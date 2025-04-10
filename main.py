import json
import random
import time
import asyncio
import telebot
from websockets.legacy.client import connect
from websockets.exceptions import ConnectionClosed

# =======================================
# CONFIG TELEGRAM
# =======================================
TELEGRAM_TOKEN = '8128728008:AAHqEYHrT5Wt8L_qJ_QeSDRvlFjl0llxtoM'
CHAT_ID = '-1002642605413'
bot = telebot.TeleBot(TELEGRAM_TOKEN)

# =======================================
# LISTA DE SINAIS
# =======================================
SINAIS_ANALISTA = [
    "ENTRE 3 RODADAS NO ⚪\n\nDEPOIS DO {num}{emoji}",
]

# =======================================
# VARIÁVEIS DE ESTADO
# =======================================
STATE = {
    "phase": "IDLE",            # IDLE, WAITING_3, WAITING_2
    "white_count": 0,
    "rounds_left": 0,
    "next_signal_time": 0.0,
    "signal_round": None,
    "did_flush": False,
    "in_whites_loop": False,
    "sinal2_message_id": None,
    "whites_loop_start": None,  # p/ rastrear início da sequência de brancos
}

WHITE_MULTIPLIERS = {
    1: 14,
    2: 28,
    3: 42,
    4: 56,
    5: 70,
    6: 84,
    7: 98,
    8: 112,
    9: 126,
    10: 140
}

LOSS_OPTIONS = [
    "Dessa vez não deu ✖️",
    "Não foi agora ✖️",
    "Não veio agora ✖️",
    "Não encaixou ✖️",
    "Não rolou ✖️",
    "loss✖️"
]

async def send_telegram_message(text):
    return await asyncio.to_thread(bot.send_message, CHAT_ID, text)

async def delete_signal_message():
    if STATE.get("sinal2_message_id") is not None:
        try:
            await asyncio.to_thread(bot.delete_message, CHAT_ID, STATE["sinal2_message_id"])
            print(f"[delete_signal_message] Mensagem de SINAL 2.0 apagada (ID: {STATE['sinal2_message_id']})")
        except Exception as e:
            print(f"[delete_signal_message] Erro ao apagar msg: {e}")
        STATE["sinal2_message_id"] = None

def get_color_emoji(num):
    if num == 0:
        return "⚪"
    elif 1 <= num <= 7:
        return "🔴"
    elif 8 <= num <= 14:
        return "⚫️"
    return "❓"

def schedule_next_signal(min_s=30, max_s=60):
    wait_seconds = random.randint(min_s, max_s)
    STATE["next_signal_time"] = time.time() + wait_seconds
    print(f"[schedule_next_signal] Próximo sinal em {wait_seconds} segundos.")

async def flush_old_rounds(ws):
    if STATE["phase"] != "IDLE":
        print("[flush_old_rounds] Já em um sinal ativo; não descartando.")
        STATE["did_flush"] = True
        return

    print("[flush_old_rounds] Descartando rodadas antigas...")
    last_round_id = None
    while True:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=30)
        except asyncio.TimeoutError:
            print("[flush_old_rounds] Timeout. Fim do flush.")
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
                    if current_round_id != last_round_id:
                        print("[flush_old_rounds] Rodadas antigas descartadas. Iniciando leitura real.")
                        STATE["did_flush"] = True
                        return
                    last_round_id = current_round_id
            except Exception as e:
                print("[flush_old_rounds] Erro no flush:", e)

async def get_next_round(ws, last_round_id_set):
    while True:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=30)
        except asyncio.TimeoutError:
            print("[get_next_round] Timeout, tentando de novo...")
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
    STATE["in_whites_loop"] = True
    STATE["whites_loop_start"] = time.time()
    consecutive = 1
    print("[handle_consecutive_whites] Entrando no loop de brancos...")

    while True:
        multiplier = WHITE_MULTIPLIERS.get(consecutive, 14 * consecutive)
        win_msg = f"{multiplier}x do analista!⚪✅"
        await send_telegram_message(win_msg)
        print(f"[handle_consecutive_whites] {consecutive}º branco => {multiplier}x")

        # Tempo limite de 30s para não ficar preso
        if time.time() - STATE["whites_loop_start"] > 30:
            print("[handle_consecutive_whites] Timeout nos brancos. Encerrando loop.")
            break
        if consecutive >= 10:
            print("[handle_consecutive_whites] 10 brancos! Encerrando loop.")
            break

        roll = await get_next_round(ws, last_round_id_set)
        emoji = get_color_emoji(roll)
        print(f"[handle_consecutive_whites] Nova rodada: {roll}{emoji}")

        if roll == 0:
            consecutive += 1
        else:
            break

    # Termina loop, reseta estado
    STATE["phase"] = "IDLE"
    STATE["in_whites_loop"] = False
    STATE["whites_loop_start"] = None
    STATE["white_count"] = 0
    STATE["rounds_left"] = 0
    schedule_next_signal()

async def process_round(roll, ws, last_round_id_set):
    # Se ainda estiver em loop de brancos e não estourou timeout,
    # ignora as rodadas até handle_consecutive_whites terminar.
    if STATE["in_whites_loop"]:
        # Se a rede cair no meio, handle_consecutive_whites pode não conseguir processar
        # Mas reforçamos um timeout, se quiser, aqui também.
        print("[process_round] Ignorando rodada: sequência de brancos ativa.")
        return

    if STATE["phase"] == "IDLE":
        return

    emoji = get_color_emoji(roll)
    print(f"[process_round] Rodada: {roll}{emoji} (fase={STATE['phase']})")

    if roll == 0:
        if not STATE["in_whites_loop"]:
            await handle_consecutive_whites(ws, last_round_id_set)
        return

    if STATE["phase"] == "WAITING_3":
        STATE["rounds_left"] -= 1
        if STATE["rounds_left"] <= 0:
            msg = "SINAL 2.0 🔥\n\nEntre 2 rodadas no ⚪"
            sent_msg = await send_telegram_message(msg)
            STATE["sinal2_message_id"] = sent_msg.message_id
            print("[process_round] SINAL 2.0 enviado:", msg)
            STATE["phase"] = "WAITING_2"
            STATE["rounds_left"] = 2

    elif STATE["phase"] == "WAITING_2":
        STATE["rounds_left"] -= 1
        if STATE["rounds_left"] <= 0:
            await delete_signal_message()
            loss_msg = random.choice(LOSS_OPTIONS)
            await send_telegram_message(loss_msg)
            print("[process_round] LOSS:", loss_msg)
            STATE["phase"] = "IDLE"
            schedule_next_signal()

async def maybe_send_signal(ws, last_round_id_set):
    if STATE["phase"] == "IDLE":
        now = time.time()
        if now >= STATE["next_signal_time"]:
            roll = await get_next_round(ws, last_round_id_set)
            STATE["signal_round"] = roll
            emoji_signal = get_color_emoji(roll)
            template = random.choice(SINAIS_ANALISTA)
            if roll == 0:
                sinal_msg = template.format(num="", emoji=emoji_signal)
            else:
                sinal_msg = template.format(num=roll, emoji=emoji_signal)

            await send_telegram_message(sinal_msg)
            print("[maybe_send_signal] Enviou sinal:", sinal_msg)

            STATE["phase"] = "WAITING_3"
            STATE["rounds_left"] = 3
            STATE["white_count"] = 0
            STATE["in_whites_loop"] = False
            STATE["whites_loop_start"] = None

def main_loop():
    async def bot_main():
        while True:
            try:
                await run_bot_cycle()
            except ConnectionClosed as e:
                print(f"[bot_main] WebSocket fechado: {e}. Esperando 5s e reconectando...")
                await asyncio.sleep(5)
            except Exception as e:
                print(f"[bot_main] Erro inesperado: {e}. Esperando 5s e reconectando...")
                await asyncio.sleep(5)

    asyncio.run(bot_main())

async def run_bot_cycle():
    """
    1) Conecta no WebSocket
    2) Reseta o estado do bot (para não herdar in_whites_loop etc.)
    3) flush_old_rounds (se IDLE)
    4) Loop de leitura das rodadas
    """
    # FORÇAR RESET SEMPRE QUE RECONECTAR
    STATE["phase"] = "IDLE"
    STATE["in_whites_loop"] = False
    STATE["whites_loop_start"] = None
    STATE["white_count"] = 0
    STATE["rounds_left"] = 0

    # Se quiser, reagenda também
    if STATE["next_signal_time"] == 0.0:
        schedule_next_signal()

    uri = "wss://api-gaming.blaze.bet.br/replication/?EIO=3&transport=websocket"
    # Tente ping_interval menor, p. ex. 10, se 20 estiver caindo muito
    async with connect(uri, ping_interval=10, ping_timeout=10) as ws:
        print("[run_bot_cycle] Conectado à Blaze.")
        await ws.send('420["cmd",{"id":"subscribe","payload":{"room":"double_room_1"}}]')
        print("[run_bot_cycle] Inscrito no canal double_room_1.")

        if not STATE["did_flush"]:
            await flush_old_rounds(ws)

        last_round_id_set = set()
        while True:
            await maybe_send_signal(ws, last_round_id_set)
            roll = await get_next_round(ws, last_round_id_set)
            await process_round(roll, ws, last_round_id_set)

if __name__ == "__main__":
    print("Bot rodando...")
    main_loop()
