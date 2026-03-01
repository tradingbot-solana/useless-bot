import requests
import time
import json
import subprocess
import os
from datetime import datetime

# ============== CONFIG ==============
BIRDEYE_API_KEY = os.getenv("BIRDEYE_API_KEY")
if not BIRDEYE_API_KEY:
    print("ERROR: No Birdeye key! Add it in Railway Variables.")
    exit(1)

TOKEN_ADDRESS = "Dz9mQ9NzkBcCsuGPFJ3r1bS4wgqKMHBPiVuniW8Mbonk"
MOONPAY_WALLET = "useless-trader"
BASE_TOKEN = "USDC"
CHECK_INTERVAL_MIN = 15
RSI_PERIOD = 14
YELLOW_MA_PERIOD = 9
STOP_LOSS_PCT = 0.05
POSITION_SIZE_PCT = 0.80
STATE_FILE = "useless_agent_state.json"
# ====================================

HEADERS = {
    "accept": "application/json",
    "x-chain": "solana",
    "X-API-KEY": BIRDEYE_API_KEY
}

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, 'r') as f:
            return json.load(f)
    return {"position": BASE_TOKEN, "entry_price": None}

def save_state(state):
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f)

def get_historical_prices():
    now_unix = int(time.time())
    from_unix = now_unix - (3600 * 24 * 2)  # ~2 days back for enough candles
    url = f"https://public-api.birdeye.so/defi/history_price?address={TOKEN_ADDRESS}&address_type=token&type=15m&time_from={from_unix}&time_to={now_unix}&ui_amount_mode=raw"
    response = requests.get(url, headers=HEADERS)
    response.raise_for_status()
    data = response.json()
    if not data.get("success"):
        raise ValueError("Birdeye API error: " + str(data))
    items = data["data"]["items"]
    closes = [item["value"] for item in items]  # newest first → reverse for oldest first
    return closes[::-1]

def calculate_rsi_and_ma(closes):
    if len(closes) < RSI_PERIOD + YELLOW_MA_PERIOD + 5:
        return None, None, None
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains = [d if d > 0 else 0 for d in deltas]
    losses = [-d if d < 0 else 0 for d in deltas]
    
    avg_gain = sum(gains[:RSI_PERIOD]) / RSI_PERIOD
    avg_loss = sum(losses[:RSI_PERIOD]) / RSI_PERIOD
    rsi = []
    if avg_loss == 0:
        rsi.append(100.0)
    else:
        rs = avg_gain / avg_loss
        rsi.append(100 - (100 / (1 + rs)))
    
    for i in range(RSI_PERIOD, len(gains)):
        avg_gain = (avg_gain * (RSI_PERIOD - 1) + gains[i]) / RSI_PERIOD
        avg_loss = (avg_loss * (RSI_PERIOD - 1) + losses[i]) / RSI_PERIOD
        if avg_loss == 0:
            rsi.append(100.0)
        else:
            rs = avg_gain / avg_loss
            rsi.append(100 - (100 / (1 + rs)))
    
    current_rsi = rsi[-1]
    prev_rsi = rsi[-2] if len(rsi) > 1 else current_rsi
    
    if len(rsi) < YELLOW_MA_PERIOD:
        yellow_ma = None
    else:
        yellow_ma = sum(rsi[-YELLOW_MA_PERIOD:]) / YELLOW_MA_PERIOD
    
    return current_rsi, prev_rsi, yellow_ma

def get_current_price():
    url = f"https://public-api.birdeye.so/defi/price?address={TOKEN_ADDRESS}"
    response = requests.get(url, headers=HEADERS)
    response.raise_for_status()
    data = response.json()
    if not data.get("success"):
        raise ValueError("Birdeye price error")
    return float(data["data"]["value"])

def execute_swap(from_token, to_token, amount_pct=1.0):
    cmd = [
        "mp", "token", "swap",
        "--chain", "solana",
        "--from", from_token,
        "--to", to_token,
        "--amount", str(amount_pct),
        "--wallet", MOONPAY_WALLET,
        "--confirm"
    ]
    print(f"[{datetime.now()}] Executing: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    print(result.stdout)
    if result.returncode != 0:
        print("Swap failed:", result.stderr)
        return False
    return True

# ============== MAIN LOOP ==============
state = load_state()
print(f"[{datetime.now()}] Useless Coin Crossover Agent STARTED (Birdeye + MoonPay CLI)")

while True:
    try:
        closes = get_historical_prices()
        current_rsi, prev_rsi, yellow_ma = calculate_rsi_and_ma(closes)
        
        if current_rsi is None or yellow_ma is None:
            print(f"[{datetime.now()}] Not enough data yet ({len(closes)} candles)")
            time.sleep(CHECK_INTERVAL_MIN * 60)
            continue
        
        current_price = get_current_price()
        print(f"[{datetime.now()}] Price: ${current_price:.6f} | RSI: {current_rsi:.2f} (prev: {prev_rsi:.2f}) | Yellow MA: {yellow_ma:.2f}")

        crossover_up = (prev_rsi <= yellow_ma) and (current_rsi > yellow_ma)
        crossover_down = (prev_rsi >= yellow_ma) and (current_rsi < yellow_ma)

        if state["position"] == "USELESS" and state["entry_price"] is not None:
            if current_price <= state["entry_price"] * (1 - STOP_LOSS_PCT):
                print(f"[{datetime.now()}] STOP-LOSS TRIGGERED at {current_price}")
                if execute_swap(TOKEN_ADDRESS, BASE_TOKEN):
                    state["position"] = BASE_TOKEN
                    state["entry_price"] = None
                    save_state(state)

        elif crossover_up and state["position"] == BASE_TOKEN:
            print(f"[{datetime.now()}] LONG CROSSOVER detected → Buying")
            if execute_swap(BASE_TOKEN, TOKEN_ADDRESS, POSITION_SIZE_PCT):
                state["position"] = "USELESS"
                state["entry_price"] = current_price
                save_state(state)

        elif crossover_down and state["position"] == "USELESS":
            print(f"[{datetime.now()}] SHORT CROSSOVER detected → Selling")
            if execute_swap(TOKEN_ADDRESS, BASE_TOKEN):
                state["position"] = BASE_TOKEN
                state["entry_price"] = None
                save_state(state)

    except Exception as e:
        print(f"[{datetime.now()}] Error: {e}")

    time.sleep(CHECK_INTERVAL_MIN * 60)
