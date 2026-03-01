import requests
import time
import json
import os
import base64
from datetime import datetime
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solana.rpc.api import Client
from solana.transaction import Transaction

# CONFIG
BIRDEYE_API_KEY = os.getenv("BIRDEYE_API_KEY")
if not BIRDEYE_API_KEY:
    print("ERROR: No Birdeye key!")
    exit(1)

TOKEN_ADDRESS = Pubkey.from_string("Dz9mQ9NzkBcCsuGPFJ3r1bS4wgqKMHBPiVuniW8Mbonk")
USDC_MINT = Pubkey.from_string("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v")
CHECK_INTERVAL_MIN = 5
RSI_PERIOD = 14
YELLOW_MA_PERIOD = 9
STOP_LOSS_PCT = 0.05
POSITION_SIZE_PCT = 0.30
STATE_FILE = "useless_agent_state.json"

# Load wallet key
private_key_str = os.getenv("SOLANA_PRIVATE_KEY")
if private_key_str:
    keypair = Keypair.from_base58_string(private_key_str)
else:
    print("ERROR: SOLANA_PRIVATE_KEY not set in Variables!")
    exit(1)

rpc_client = Client("https://api.mainnet-beta.solana.com")

HEADERS = {"accept": "application/json", "x-chain": "solana", "X-API-KEY": BIRDEYE_API_KEY}

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, 'r') as f:
            return json.load(f)
    return {"position": "USDC", "entry_price": None}

def save_state(state):
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f)

def get_historical_prices():
    now_unix = int(time.time())
    from_unix = now_unix - (3600 * 24 * 2)
    url = f"https://public-api.birdeye.so/defi/history_price?address={str(TOKEN_ADDRESS)}&address_type=token&type=15m&time_from={from_unix}&time_to={now_unix}&ui_amount_mode=raw"
    response = requests.get(url, headers=HEADERS)
    response.raise_for_status()
    data = response.json()
    if not data.get("success"):
        raise ValueError("Birdeye API error")
    items = data["data"]["items"]
    closes = [item["value"] for item in items]
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
    url = f"https://public-api.birdeye.so/defi/price?address={str(TOKEN_ADDRESS)}"
    response = requests.get(url, headers=HEADERS)
    response.raise_for_status()
    data = response.json()
    if not data.get("success"):
        raise ValueError("Birdeye price error")
    return float(data["data"]["value"])

def execute_swap(from_mint, to_mint, amount):
    print(f"Trying swap {amount} {from_mint} -> {to_mint}")

    # Get quote
    quote_url = "https://quote-api.jup.ag/v6/quote"
    params = {
        "inputMint": str(from_mint),
        "outputMint": str(to_mint),
        "amount": int(amount * 1_000_000),  # USDC 6 decimals
        "slippageBps": 50
    }
    quote_resp = requests.get(quote_url, params=params)
    if quote_resp.status_code != 200:
        print("Quote failed:", quote_resp.text)
        return False
    quote = quote_resp.json()

    # Get swap tx
    swap_url = "https://quote-api.jup.ag/v6/swap"
    payload = {
        "quoteResponse": quote,
        "userPublicKey": str(keypair.pubkey()),
        "wrapAndUnwrapSol": True
    }
    swap_resp = requests.post(swap_url, json=payload)
    if swap_resp.status_code != 200:
        print("Swap tx failed:", swap_resp.text)
        return False
    swap_data = swap_resp.json()
    tx_bytes = base64.b64decode(swap_data["swapTransaction"])

    # Sign & send
    try:
        tx = Transaction.deserialize(tx_bytes)
        tx.sign([keypair])
        sig = rpc_client.send_transaction(tx)
        print(f"Swap SUCCESS! Sig: {sig.value}")
        return True
    except Exception as e:
        print(f"Tx failed: {e}")
        return False

# MAIN
state = load_state()
print(f"[{datetime.now()}] Useless Coin Crossover Agent STARTED (Phantom Wallet Only)")

while True:
    try:
        closes = get_historical_prices()
        current_rsi, prev_rsi, yellow_ma = calculate_rsi_and_ma(closes)
        
        if current_rsi is None or yellow_ma is None:
            print(f"Not enough data yet ({len(closes)} candles)")
            time.sleep(CHECK_INTERVAL_MIN * 60)
            continue
        
        current_price = get_current_price()
        print(f"[{datetime.now()}] Price: ${current_price:.6f} | RSI: {current_rsi:.2f} (prev: {prev_rsi:.2f}) | Yellow MA: {yellow_ma:.2f}")

        crossover_up = (prev_rsi <= yellow_ma) and (current_rsi > yellow_ma)
        crossover_down = (prev_rsi >= yellow_ma) and (current_rsi < yellow_ma)

        if state["position"] == str(TOKEN_ADDRESS) and state["entry_price"] is not None:
            if current_price <= state["entry_price"] * (1 - STOP_LOSS_PCT):
                print(f"STOP-LOSS at {current_price}")
                if execute_swap(TOKEN_ADDRESS, USDC_MINT, 1.0):
                    state["position"] = "USDC"
                    state["entry_price"] = None
                    save_state(state)

        elif crossover_up and state["position"] == "USDC":
            print("LONG CROSSOVER → Buying")
            if execute_swap(USDC_MINT, TOKEN_ADDRESS, POSITION_SIZE_PCT):
                state["position"] = str(TOKEN_ADDRESS)
                state["entry_price"] = current_price
                save_state(state)

        elif crossover_down and state["position"] == str(TOKEN_ADDRESS):
            print("SHORT CROSSOVER → Selling")
            if execute_swap(TOKEN_ADDRESS, USDC_MINT, 1.0):
                state["position"] = "USDC"
                state["entry_price"] = None
                save_state(state)

    except Exception as e:
        print(f"Error: {e}")

    time.sleep(CHECK_INTERVAL_MIN * 60)
