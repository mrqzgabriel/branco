import json
import random
import time
import os             # Adicionado para usar os._exit
import asyncio
import threading      # Adicionado para agendar o restart via thread
import telebot
from websockets.legacy.client import connect
from websockets.exceptions import ConnectionClosed

# =======================================
# CONFIGURAÇÕES DO TELEGRAM
# =======================================
TELEGRAM_TOKEN = '8128728008:AAHqEYHrT5Wt8L_qJ_QeSDRvlFjl0llxtoM'
CHAT_ID = '-1002642605413'
bot = telebot.TeleBot(TELEGRAM_TOKEN)

# =======================================
# LISTA DE SINAIS – Mensagens com 2 linhas (tempo passado)
# =======================================
SINAIS_ANALISTA = [
    "ENTRE 3 RODADAS NO ⚪\n\nDEPOIS DO {num}{emoji}",
]

# =======================================
# VARIÁVEIS DE ESTADO
# =======================================
STATE = {
    # Fases:
    #   "IDLE"       -> aguardando envio de novo sinal
    #   "WAITING_3"  -> aguardando 3 rodadas após enviar o sinal principal
    #   "WAITING_2"  -> aguardando 2 rodadas do SINAL 2.0
    "phase": "IDLE",
    "white_count": 0,
    "rounds_left": 0,
    "next_signal_time": 0.0,
    "signal_round": None,
    "did_flush": False,
    "in_whites_loop": False,
    "sinal2_message_id": None
}

# Mapeamento para multiplicadores (WIN) – a quantidade de brancos define o multiplicador
WHITE_MULTIPLIERS = {
    1: 14,
    2: 28,
    3: 42,
    4: 56,
    5: 70,
    6: 84,
    8: 98,
    9: 112,
    10: 126
}

# Lista de mensagens alternativas para LOSS (30% das vezes)
LOSS_OPTIONS = [
    "Dessa vez não deu ✖️",
    "Não rolou agora ✖️",
    "Não veio agora ✖️",
    "Não caiu agora ✖️",
    "Não veio ✖️",
    "Não bateu ✖️",
    "Não encaixou ✖️",
    "Não rolou ✖️",
    "Não foi agora ✖️",
    "Branco não caiu ✖️",
    "Não pegamos ✖️",
]

async def send_telegram_message(text):
    """Envia uma mensagem ao Telegram de forma assíncrona e retorna o objeto da mensagem."""
    return await asyncio.to_thread(bot.send_message, CHAT_ID, text)

async def delete_signal_message():
    """
    Deleta a mensagem de SINAL 2.0, se existir,
    usada apenas quando realmente acontece um loss (sem brancos).
    """
    if STATE.get("sinal2_message_id") is not None:
        try:
            await asyncio.to_thread(bot.delete_message, CHAT_ID, STATE["sinal2_message_id"])
            print(f"[delete_signal_message] Mensagem de SINAL 2.0 apagada (ID: {STATE['sinal2_message_id']})")
        except Exception as e:
            print(f"[delete_signal_message] Erro ao apagar mensagem de SINAL 2.0: {e}")
        finally:
            STATE["sinal2_message_id"] = None

def get_color_emoji(num):
    """Retorna o emoji correspondente ao número recebido."""
    if num == 0:
        return "⚪"
    elif 1 <= num <= 7:
        return "🔴"
    elif 8 <= num <= 14:
        return "⚫️"
    return "❓"

def schedule_next_signal():
    """Agenda o próximo sinal para um intervalo aleatório entre 3 e 10 minutos."""
    wait_seconds = random.randint(180, 600)  # espera entre 180s (3 min) e 600s (10 min)
    STATE["next_signal_time"] = time.time() + wait_seconds
    print(f"[schedule_next_signal] Próximo sinal em {wait_seconds} segundos.")

async def flush_old_rounds(ws):
    """
    Descarte incondicional das rodadas antigas imediatamente após a conexão,
    se estivermos em IDLE. Se já estivermos em sinal ativo, não descarta.
    Agora com timeout para evitar bloqueios infinitos.
    """
    if STATE["phase"] != "IDLE":
        print("[flush_old_rounds] Já em um sinal ativo; não descartando rodadas.")
        STATE["did_flush"] = True
        return

    print("[flush_old_rounds] Descartando rodadas antigas...")
    last_round_id = None
    while True:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=30)
        except asyncio.TimeoutError:
            print("[flush_old_rounds] Timeout atingido. Prosseguindo com flush.")
            STATE["did_flush"] = True
            return
        if not isinstance(raw, str):
            continue
        if raw.startswith("42"):
            try:
                data = json.loads(raw[2:])[1]
                if data.get("id") != "double.tick":
                    continue
                payload = data["payload"]
                if payload["status"] == "complete":
                    current_round_id = payload.get("id")
                    # Assim que encontrarmos um round_id diferente, descartamos
                    if current_round_id != last_round_id:
                        print("[flush_old_rounds] Rodadas antigas descartadas. Iniciando leitura real.")
                        STATE["did_flush"] = True
                        return
                    last_round_id = current_round_id
            except Exception as e:
                print("[flush_old_rounds] Erro ao descartar rodadas antigas:", e)

async def get_next_round(ws, last_round_id_set):
    """
    Captura a próxima rodada completa, evitando rodadas repetidas (mesmo round_id).
    Inclui timeout para evitar espera infinita.
    """
    while True:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=30)
        except asyncio.TimeoutError:
            print("[get_next_round] Timeout na recepção do round. Continuando...")
            continue

        if not isinstance(raw, str):
            continue
        if raw.startswith("42"):
            try:
                data = json.loads(raw[2:])[1]
                if data.get("id") != "double.tick":
                    continue
                payload = data["payload"]
                if payload["status"] == "complete":
                    current_round_id = payload.get("id")
                    if current_round_id not in last_round_id_set:
                        last_round_id_set.add(current_round_id)
                        return payload["roll"]
            except Exception as e:
                print("[get_next_round] Erro ao capturar rodada:", e)

async def handle_consecutive_whites(ws, last_round_id_set):
    """
    Quando sai branco (roll==0), verifica se há brancos consecutivos,
    enviando mensagens de WIN correspondentes (14x, 28x etc.).
    Ao encerrar, volta para IDLE e mantém a mensagem de SINAL 2.0,
    não enviando loss.
    """
    STATE["in_whites_loop"] = True
    consecutive = 1
    while True:
        multiplier = WHITE_MULTIPLIERS.get(consecutive, 14 * consecutive)
        win_msg = f"{multiplier}x do analista!⚪✅"
        await send_telegram_message(win_msg)
        print(f"[handle_consecutive_whites] {consecutive}º branco => {multiplier}x")

        if consecutive == 10:
            break

        roll = await get_next_round(ws, last_round_id_set)
        emoji = get_color_emoji(roll)
        print(f"[handle_consecutive_whites] Nova rodada após branco: {roll}{emoji}")

        if roll == 0:
            consecutive += 1
        else:
            break

    # Não apagamos a mensagem de SINAL 2.0; apenas finalizamos a sequência WIN
    STATE["phase"] = "IDLE"
    STATE["white_count"] = 0
    STATE["rounds_left"] = 0
    schedule_next_signal()
    STATE["in_whites_loop"] = False

async def process_round(roll, ws, last_round_id_set):
    """
    Processa cada rodada com base na fase atual.
      - Se sair branco, chama handle_consecutive_whites (WIN) e não envia loss.
      - Se não sair branco, decrementa rounds_left. Se chegar a 0 em WAITING_2, envia LOSS.
    """
    if STATE["in_whites_loop"]:
        print("[process_round] Ignorando rodada: sequência de brancos ativa.")
        return

    if STATE["phase"] == "IDLE":
        return

    emoji = get_color_emoji(roll)
    print(f"[process_round] Rodada recebida: {roll}{emoji} (fase={STATE['phase']})")

    if roll == 0:
        if not STATE["in_whites_loop"]:
            await handle_consecutive_whites(ws, last_round_id_set)
        return

    if STATE["phase"] == "WAITING_3":
        STATE["rounds_left"] -= 1
        if STATE["phase"] != "WAITING_3":
            return

        if STATE["rounds_left"] <= 0:
            msg = "SINAL 2.0 🔥\n\nEntre 2 rodadas no ⚪"
            sent_msg = await send_telegram_message(msg)
            STATE["sinal2_message_id"] = sent_msg.message_id
            print("[process_round] SINAL 2.0 enviado:", msg)
            STATE["phase"] = "WAITING_2"
            STATE["rounds_left"] = 2
            STATE["white_count"] = 0

    elif STATE["phase"] == "WAITING_2":
        STATE["rounds_left"] -= 1
        if STATE["phase"] != "WAITING_2":
            return

        if STATE["rounds_left"] <= 0:
            # Somente se não houve branco (WIN) é que apagamos o sinal e enviamos loss
            await delete_signal_message()
            if random.random() < 0.7:
                loss_msg = "loss✖️"
            else:
                loss_msg = random.choice(LOSS_OPTIONS)
            await send_telegram_message(loss_msg)
            print("[process_round] LOSS enviado:", loss_msg)
            STATE["phase"] = "IDLE"
            STATE["white_count"] = 0
            STATE["rounds_left"] = 0
            schedule_next_signal()

async def maybe_send_signal(ws, last_round_id_set):
    """
    Se o estado for IDLE e o tempo de enviar sinal chegou, captura a próxima rodada
    e envia a mensagem de sinal, mudando a fase para WAITING_3.
    """
    if STATE["phase"] == "IDLE":
        now = time.time()
        if now >= STATE["next_signal_time"]:
            roll = await get_next_round(ws, last_round_id_set)
            STATE["signal_round"] = roll
            emoji_signal = get_color_emoji(roll)
            sinal_template = random.choice(SINAIS_ANALISTA)
            if roll == 0:
                sinal_msg = sinal_template.format(num="", emoji=emoji_signal)
            else:
                sinal_msg = sinal_template.format(num=roll, emoji=emoji_signal)

            await send_telegram_message(sinal_msg)
            print("[maybe_send_signal] Sinal enviado:", sinal_msg)

            STATE["phase"] = "WAITING_3"
            STATE["white_count"] = 0
            STATE["rounds_left"] = 3
            STATE["in_whites_loop"] = False

def main_loop():
    """
    Loop principal de reconexão: se a conexão ao WebSocket cair ou ocorrer algum erro inesperado,
    aguarda 5 segundos e tenta reconectar, mantendo o estado atual.
    Isso garante que o bot continue funcionando 24h.
    """
    async def bot_main():
        while True:
            try:
                await run_bot_cycle()
            except ConnectionClosed as e:
                print(f"[bot_main] WebSocket fechado: {e}. Tentando reconectar em 5s...")
                await asyncio.sleep(5)
            except Exception as e:
                print(f"[bot_main] Erro inesperado: {e}. Tentando reconectar em 5s...")
                await asyncio.sleep(5)

    asyncio.run(bot_main())

async def run_bot_cycle():
    """
    Conecta ao WebSocket, executa o flush de rodadas antigas (se em IDLE)
    e entra no loop de leitura dos rounds, processando cada rodada conforme a lógica atual.
    """
    uri = "wss://api-gaming.blaze.bet.br/replication/?EIO=3&transport=websocket"
    # Configurando ping_interval e ping_timeout para manter a conexão ativa
    async with connect(uri, ping_interval=20, ping_timeout=20) as ws:
        print("[run_bot_cycle] Conectado à Blaze.")
        await ws.send('420["cmd",{"id":"subscribe","payload":{"room":"double_room_1"}}]')
        print("[run_bot_cycle] Inscrito no canal double_room_1.")

        if not STATE["did_flush"] and STATE["phase"] == "IDLE":
            await flush_old_rounds(ws)

        if STATE["phase"] == "IDLE" and STATE["next_signal_time"] == 0.0:
            schedule_next_signal()

        last_round_id_set = set()
        while True:
            await maybe_send_signal(ws, last_round_id_set)
            roll = await get_next_round(ws, last_round_id_set)
            await process_round(roll, ws, last_round_id_set)

# =======================================
# Agendamento do auto-restart a cada 1 hora
# =======================================
def schedule_auto_restart():
    """Aguarda 1 hora e finaliza o processo para que o Render reinicie o Worker."""
    time.sleep(3600)  # espera 1 hora
    os._exit(0)

if __name__ == "__main__":
    print("Bot rodando...")

    # Inicia uma thread daemon que automaticamente finaliza o processo após 1 hora
    threading.Thread(target=schedule_auto_restart, daemon=True).start()

    main_loop()
