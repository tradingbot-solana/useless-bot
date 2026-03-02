import requests
import time
import json
import os
import base64
from datetime import datetime
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solana.rpc.api import Client
from solana.rpc.types import TokenAccountOpts
from solana.transaction import Transaction

# ────────────────────────────────────────────────
# CONFIG
# ────────────────────────────────────────────────

BIRDEYE_API_KEY = os.getenv("BIRDEYE_API_KEY")
JUPITER_API_KEY = os.getenv("JUPITER_API_KEY")

if not BIRDEYE_API_KEY:
    print("ERROR: BIRDEYE_API_KEY not set!")
    exit(1)

if not JUPITER_API_KEY:
    print("ERROR: JUPITER_API_KEY not set! Get one at https://dev.jup.ag/portal/setup")
    exit(1)

TOKEN_ADDRESS = Pubkey.from_string("Dz9mQ9NzkBcCsuGPFJ3r1bS4wgqKMHBPiVuniW8Mbonk")  # Your target token
USDC_MINT    = Pubkey.from_string("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v")

CHECK_INTERVAL_MIN = 1
RSI_PERIOD         = 14
YELLOW_MA_PERIOD   = 9
STOP_LOSS_PCT      = 0.05
POSITION_SIZE_PCT  = 0.5   # fraction of USDC balance to use when buying
STATE_FILE         = "useless_agent_state.json"

# Load wallet
private_key_str = os.getenv("SOLANA_PRIVATE_KEY")
if not private_key_str:
    print("ERROR: SOLANA_PRIVATE_KEY not set!")
    exit(1)

keypair = Keypair.from_base58_string(private_key_str)
rpc_client = Client("https://api.mainnet-beta.solana.com")

# Headers
BIRDEYE_HEADERS = {
    "accept": "application/json",
    "x-chain": "solana",
    "X-API-KEY": BIRDEYE_API_KEY
}
JUPITER_HEADERS = {
    "x-api-key": JUPITER_API_KEY,
    "Content-Type": "application/json"
}

JUP_BASE = "https://api.jup.ag/swap/v1"

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
    from_unix = now_unix - (3600 * 24 * 2)  # 2 days
    url = f"https://public-api.birdeye.so/defi/history_price?address={str(TOKEN_ADDRESS)}&address_type=token&type=15m&time_from={from_unix}&time_to={now_unix}&ui_amount_mode=raw"
    resp = requests.get(url, headers=BIRDEYE_HEADERS)
    resp.raise_for_status()
    data = resp.json()
    if not data.get("success"):
        raise ValueError(f"Birdeye history error: {data}")
    items = data["data"]["items"]
    closes = [item["value"] for item in items]
    return closes[::-1]  # oldest → newest

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

    yellow_ma = None
    if len(rsi) >= YELLOW_MA_PERIOD:
        yellow_ma = sum(rsi[-YELLOW_MA_PERIOD:]) / YELLOW_MA_PERIOD

    return current_rsi, prev_rsi, yellow_ma

def get_current_price():
    url = f"https://public-api.birdeye.so/defi/price?address={str(TOKEN_ADDRESS)}"
    resp = requests.get(url, headers=BIRDEYE_HEADERS)
    resp.raise_for_status()
    data = resp.json()
    if not data.get("success"):
        raise ValueError(f"Birdeye price error: {data}")
    return float(data["data"]["value"])

def get_token_balance(token_mint: Pubkey) -> int:
    """Get raw token balance (smallest units) of the associated token account"""
    opts = TokenAccountOpts(mint=token_mint)
    resp = rpc_client.get_token_accounts_by_owner(keypair.pubkey(), opts)
    if not resp.value:
        return 0
    # Assume first (usually only) ATA
    ata = resp.value[0].pubkey
    bal_resp = rpc_client.get_token_account_balance(ata)
    if bal_resp.value:
        return int(bal_resp.value.amount)
    return 0

def execute_swap(from_mint: Pubkey, to_mint: Pubkey, ui_amount: float = None):
    print(f"Executing swap: {from_mint} → {to_mint}")

    is_buying = from_mint == USDC_MINT

    if is_buying:
        if ui_amount is None:
            ui_amount = POSITION_SIZE_PCT  # fraction, but we need absolute USDC later
        # For buy: amount = USDC lamports (6 decimals)
        amount_lamports = int(ui_amount * 1_000_000)
    else:
        # For sell: use full balance
        raw_balance = get_token_balance(from_mint)
        if raw_balance <= 0:
            print("No balance to sell!")
            return False
        amount_lamports = raw_balance
        print(f"Selling full balance: {raw_balance} raw units")

    params = {
        "inputMint": str(from_mint),
        "outputMint": str(to_mint),
        "amount": amount_lamports,
        "slippageBps": 50,
        # optional: "feeBps": 0, etc.
    }

    try:
        quote_resp = requests.get(f"{JUP_BASE}/quote", params=params, headers=JUPITER_HEADERS)
        quote_resp.raise_for_status()
        quote = quote_resp.json()
    except Exception as e:
        print(f"Quote failed: {e} — {quote_resp.text if 'quote_resp' in locals() else ''}")
        return False

    payload = {
        "quoteResponse": quote,
        "userPublicKey": str(keypair.pubkey()),
        "wrapAndUnwrapSol": True,
        # optional: "feeAccount", "prioritizationFeeLamports", etc.
    }

    try:
        swap_resp = requests.post(f"{JUP_BASE}/swap", json=payload, headers=JUPITER_HEADERS)
        swap_resp.raise_for_status()
        swap_data = swap_resp.json()
        tx_bytes = base64.b64decode(swap_data["swapTransaction"])
    except Exception as e:
        print(f"Swap response failed: {e} — {swap_resp.text if 'swap_resp' in locals() else ''}")
        return False

    try:
        tx = Transaction.deserialize(tx_bytes)
        tx.sign([keypair])
        sig = rpc_client.send_transaction(tx)
        print(f"Swap SUCCESS! Signature: {sig.value}")
        return True
    except Exception as e:
        print(f"Transaction failed: {e}")
        return False

# ────────────────────────────────────────────────
# MAIN LOOP
# ────────────────────────────────────────────────

state = load_state()
print(f"[{datetime.now()}] Useless Coin Crossover Agent STARTED (Phantom-compatible)")

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

        crossover_up   = (prev_rsi <= yellow_ma) and (current_rsi > yellow_ma)
        crossover_down = (prev_rsi >= yellow_ma) and (current_rsi < yellow_ma)

        in_token = state["position"] == str(TOKEN_ADDRESS)

        if in_token and state["entry_price"] is not None:
            if current_price <= state["entry_price"] * (1 - STOP_LOSS_PCT):
                print(f"STOP-LOSS triggered at {current_price:.6f}")
                if execute_swap(TOKEN_ADDRESS, USDC_MINT):
                    state["position"] = "USDC"
                    state["entry_price"] = None
                    save_state(state)

        elif crossover_up and not in_token:
            print("LONG CROSSOVER → Buying")
            if execute_swap(USDC_MINT, TOKEN_ADDRESS):
                state["position"] = str(TOKEN_ADDRESS)
                state["entry_price"] = current_price
                save_state(state)

        elif crossover_down and in_token:
            print("SHORT CROSSOVER → Selling")
            if execute_swap(TOKEN_ADDRESS, USDC_MINT):
                state["position"] = "USDC"
                state["entry_price"] = None
                save_state(state)

    except Exception as e:
        print(f"Loop error: {e}")

    time.sleep(CHECK_INTERVAL_MIN * 60)
