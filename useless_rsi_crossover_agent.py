import requests
import time
import json
import os
import base64
from datetime import datetime
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction
from solana.rpc.api import Client
from solana.rpc.types import TokenAccountOpts
from solana.rpc.commitment import Confirmed

# ────────────────────────────────────────────────
# CONFIG
# ────────────────────────────────────────────────

BIRDEYE_API_KEY = os.getenv("BIRDEYE_API_KEY")
JUPITER_API_KEY = os.getenv("JUPITER_API_KEY")

if not BIRDEYE_API_KEY:
    print("ERROR: BIRDEYE_API_KEY not set!")
    exit(1)

if not JUPITER_API_KEY:
    print("ERROR: JUPITER_API_KEY not set! Get one at https://portal.jup.ag")
    exit(1)

TOKEN_ADDRESS = Pubkey.from_string("Dz9mQ9NzkBcCsuGPFJ3r1bS4wgqKMHBPiVuniW8Mbonk")
USDC_MINT     = Pubkey.from_string("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v")

CHECK_INTERVAL_MIN = 1
RSI_PERIOD         = 14
YELLOW_MA_PERIOD   = 9
STOP_LOSS_PCT      = 0.05
POSITION_SIZE_PCT  = 0.5
MIN_USDC_FOR_TRADE = 1.0  # skip buy if less than this much USDC available

STATE_FILE         = "useless_agent_state.json"

# Wallet & RPC (consider replacing with Helius/QuickNode later)
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

# ────────────────────────────────────────────────
# STATE MANAGEMENT
# ────────────────────────────────────────────────

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, 'r') as f:
            return json.load(f)
    return {"position": "USDC", "entry_price": None}

def save_state(state):
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f)

# ────────────────────────────────────────────────
# DATA FETCHING
# ────────────────────────────────────────────────

def get_historical_prices():
    now_unix = int(time.time())
    from_unix = now_unix - (3600 * 24 * 2)
    url = f"https://public-api.birdeye.so/defi/history_price?address={str(TOKEN_ADDRESS)}&address_type=token&type=15m&time_from={from_unix}&time_to={now_unix}&ui_amount_mode=raw"
    resp = requests.get(url, headers=BIRDEYE_HEADERS)
    resp.raise_for_status()
    data = resp.json()
    if not data.get("success"):
        raise ValueError(f"Birdeye history failed: {data.get('message')}")
    items = data["data"]["items"]
    closes = [item["value"] for item in items]
    return closes[::-1]

def calculate_rsi_and_ma(closes):
    if len(closes) < RSI_PERIOD + YELLOW_MA_PERIOD + 5:
        return None, None, None

    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains = [max(d, 0) for d in deltas]
    losses = [max(-d, 0) for d in deltas]

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

    yellow_ma = sum(rsi[-YELLOW_MA_PERIOD:]) / YELLOW_MA_PERIOD if len(rsi) >= YELLOW_MA_PERIOD else None

    return current_rsi, prev_rsi, yellow_ma

def get_current_price():
    url = f"https://public-api.birdeye.so/defi/price?address={str(TOKEN_ADDRESS)}"
    resp = requests.get(url, headers=BIRDEYE_HEADERS)
    resp.raise_for_status()
    data = resp.json()
    if not data.get("success"):
        raise ValueError(f"Birdeye price failed: {data.get('message')}")
    return float(data["data"]["value"])

def get_token_balance(token_mint: Pubkey) -> int:
    opts = TokenAccountOpts(mint=token_mint)
    resp = rpc_client.get_token_accounts_by_owner(keypair.pubkey(), opts)
    if not resp.value:
        return 0
    ata = resp.value[0].pubkey
    bal_resp = rpc_client.get_token_account_balance(ata)
    return int(bal_resp.value.amount) if bal_resp.value else 0

# ────────────────────────────────────────────────
# SWAP EXECUTION
# ────────────────────────────────────────────────

def execute_swap(from_mint: Pubkey, to_mint: Pubkey):
    print(f"Trying swap: {from_mint} → {to_mint}")

    is_buy = from_mint == USDC_MINT

    if is_buy:
        usdc_balance_raw = get_token_balance(USDC_MINT)
        if usdc_balance_raw <= 0:
            print("No USDC balance available!")
            return False
        usdc_amount_ui = usdc_balance_raw / 1_000_000
        if usdc_amount_ui < MIN_USDC_FOR_TRADE:
            print(f"USDC balance too low: {usdc_amount_ui:.4f} < {MIN_USDC_FOR_TRADE}")
            return False
        amount_to_use_ui = usdc_amount_ui * POSITION_SIZE_PCT
        amount_lamports = int(amount_to_use_ui * 1_000_000)
        print(f"Buying with {amount_to_use_ui:.4f} USDC ({POSITION_SIZE_PCT*100}% of {usdc_amount_ui:.4f})")
    else:
        raw_balance = get_token_balance(from_mint)
        if raw_balance <= 0:
            print("No tokens to sell!")
            return False
        amount_lamports = raw_balance
        print(f"Selling full balance: {raw_balance} raw units")

    params = {
        "inputMint": str(from_mint),
        "outputMint": str(to_mint),
        "amount": amount_lamports,
        "slippageBps": 50,
        "asLegacyTransaction": False,
        "maxAccounts": 30,
    }

    for attempt in range(1, 4):
        print(f"Attempt {attempt}/3...")
        try:
            quote_resp = requests.get(f"{JUP_BASE}/quote", params=params, headers=JUPITER_HEADERS)
            quote_resp.raise_for_status()
            quote = quote_resp.json()

            out_amount = int(quote.get('outAmount', '0'))
            if out_amount <= 0:
                print("Invalid quote: zero or negative output amount")
                continue

            print(f"Quote: expected out ≈ {out_amount / 10**6:.4f} tokens")

            payload = {
                "quoteResponse": quote,
                "userPublicKey": str(keypair.pubkey()),
                "wrapAndUnwrapSol": True,
                "computeUnitPriceMicroLamports": 100000,
            }

            swap_resp = requests.post(f"{JUP_BASE}/swap", json=payload, headers=JUPITER_HEADERS)
            swap_resp.raise_for_status()
            swap_data = swap_resp.json()

            tx_b64 = swap_data["swapTransaction"]
            tx_bytes = base64.b64decode(tx_b64)

            if len(tx_bytes) < 100:
                raise ValueError(f"Suspiciously small transaction bytes: {len(tx_bytes)}")

            print(f"Received tx bytes: {len(tx_bytes)}")

            tx = VersionedTransaction.from_bytes(tx_bytes)
            tx = tx.sign([keypair])

            sim = rpc_client.simulate_transaction(tx)
            if sim.value.err:
                print(f"Simulation failed: {sim.value.err}")
                if sim.value.logs:
                    print("Simulation logs (first few):", sim.value.logs[:6])
                continue

            print("Simulation passed → sending...")

            sig_resp = rpc_client.send_raw_transaction(tx.serialize())
            sig = sig_resp.value

            print(f"Transaction submitted: {sig}")

            # Wait for confirmation
            confirmed = rpc_client.confirm_transaction(sig, commitment=Confirmed)
            if confirmed.value:
                print(f"Transaction CONFIRMED: {sig}")
                return True
            else:
                print(f"Confirmation timeout or failed for {sig}")
                continue

        except Exception as e:
            print(f"Attempt {attempt} failed: {type(e).__name__}: {e}")
            if 'quote_resp' in locals():
                print("Quote snippet:", quote_resp.text[:250])
            if 'swap_resp' in locals():
                print("Swap snippet:", swap_resp.text[:250])
            if attempt < 3:
                time.sleep(2.5)

    print("All attempts failed.")
    return False

# ────────────────────────────────────────────────
# MAIN LOOP
# ────────────────────────────────────────────────

state = load_state()
print(f"[{datetime.now()}] Agent STARTED – {POSITION_SIZE_PCT*100}% USDC per buy")

while True:
    try:
        closes = get_historical_prices()
        current_rsi, prev_rsi, yellow_ma = calculate_rsi_and_ma(closes)

        if current_rsi is None or yellow_ma is None:
            print(f"Not enough data ({len(closes)} candles)")
            time.sleep(CHECK_INTERVAL_MIN * 60)
            continue

        current_price = get_current_price()
        print(f"[{datetime.now()}] Price: ${current_price:.6f} | RSI: {current_rsi:.2f} (prev: {prev_rsi:.2f}) | Yellow MA: {yellow_ma:.2f}")

        crossover_up   = (prev_rsi <= yellow_ma) and (current_rsi > yellow_ma)
        crossover_down = (prev_rsi >= yellow_ma) and (current_rsi < yellow_ma)

        holding_token = state["position"] == str(TOKEN_ADDRESS)

        if holding_token and state["entry_price"] is not None:
            if current_price <= state["entry_price"] * (1 - STOP_LOSS_PCT):
                print(f"STOP LOSS at {current_price:.6f}")
                if execute_swap(TOKEN_ADDRESS, USDC_MINT):
                    state["position"] = "USDC"
                    state["entry_price"] = None
                    save_state(state)

        elif crossover_up and not holding_token:
            print("↑ LONG CROSSOVER → Buying")
            if execute_swap(USDC_MINT, TOKEN_ADDRESS):
                state["position"] = str(TOKEN_ADDRESS)
                state["entry_price"] = current_price
                save_state(state)

        elif crossover_down and holding_token:
            print("↓ SHORT CROSSOVER → Selling")
            if execute_swap(TOKEN_ADDRESS, USDC_MINT):
                state["position"] = "USDC"
                state["entry_price"] = None
                save_state(state)

    except Exception as e:
        print(f"Main loop error: {e}")

    time.sleep(CHECK_INTERVAL_MIN * 60)
