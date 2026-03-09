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
from solana.rpc.types import TokenAccountOpts, TxOpts
from solana.rpc.commitment import Confirmed

# ────────────────────────────────────────────────
# CONFIG
# ────────────────────────────────────────────────

BIRDEYE_API_KEY = os.getenv("BIRDEYE_API_KEY")
JUPITER_API_KEY = os.getenv("JUPITER_API_KEY")
HELIUS_API_KEY  = os.getenv("HELIUS_API_KEY")

if not BIRDEYE_API_KEY:
    print("ERROR: BIRDEYE_API_KEY not set!")
    exit(1)
if not JUPITER_API_KEY:
    print("ERROR: JUPITER_API_KEY not set!")
    exit(1)
if not HELIUS_API_KEY:
    print("ERROR: HELIUS_API_KEY not set! Get free at dashboard.helius.xyz")
    exit(1)

TOKEN_ADDRESS = Pubkey.from_string("Dz9mQ9NzkBcCsuGPFJ3r1bS4wgqKMHBPiVuniW8Mbonk")
USDC_MINT     = Pubkey.from_string("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v")

CHECK_INTERVAL_MIN = 1  # Keep at 1min; sufficient to catch 5min candle updates
RSI_PERIOD         = 10  # Shortened for faster signals
YELLOW_MA_PERIOD   = 5   # Shortened for less lag
EMA_PERIOD         = 50  # For trend filter
STOP_LOSS_PCT      = 0.05          # initial hard stop (safety net)
TRAIL_PCT          = 0.12          # Tightened for quicker profit lock in pumps (was 0.15)
POSITION_SIZE_PCT  = 0.5
MIN_USDC_FOR_TRADE = 2.0

STATE_FILE = "useless_agent_state.json"

private_key_str = os.getenv("SOLANA_PRIVATE_KEY")
if not private_key_str:
    print("ERROR: SOLANA_PRIVATE_KEY not set!")
    exit(1)

keypair = Keypair.from_base58_string(private_key_str)
rpc_client = Client(f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}")

BIRDEYE_HEADERS = {"accept": "application/json", "x-chain": "solana", "X-API-KEY": BIRDEYE_API_KEY}
JUPITER_HEADERS = {"x-api-key": JUPITER_API_KEY, "Content-Type": "application/json"}
JUP_BASE = "https://api.jup.ag/swap/v1"

# ────────────────────────────────────────────────
# STATE MANAGEMENT
# ────────────────────────────────────────────────

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, 'r') as f:
            state = json.load(f)
            # Ensure new keys exist for backward compatibility
            state.setdefault("max_price", None)
            return state
    return {"position": "USDC", "entry_price": None, "max_price": None}

def save_state(state):
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f)

# ────────────────────────────────────────────────
# DATA FETCHING
# ────────────────────────────────────────────────

def get_historical_prices():
    now_unix = int(time.time())
    from_unix = now_unix - (3600 * 24 * 7)  # 7 days back for more 5min data (~2016 candles)
    url = f"https://public-api.birdeye.so/defi/history_price?address={str(TOKEN_ADDRESS)}&address_type=token&type=5m&time_from={from_unix}&time_to={now_unix}&ui_amount_mode=raw"
    resp = requests.get(url, headers=BIRDEYE_HEADERS)
    resp.raise_for_status()
    data = resp.json()
    if not data.get("success"):
        raise ValueError("Birdeye history error")
    return [item["value"] for item in data["data"]["items"]][::-1]

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
        rsi.append(100 - (100 / (1 + avg_gain / avg_loss)))
    for i in range(RSI_PERIOD, len(gains)):
        avg_gain = (avg_gain * (RSI_PERIOD - 1) + gains[i]) / RSI_PERIOD
        avg_loss = (avg_loss * (RSI_PERIOD - 1) + losses[i]) / RSI_PERIOD
        if avg_loss == 0:
            rsi.append(100.0)
        else:
            rsi.append(100 - (100 / (1 + avg_gain / avg_loss)))
    current_rsi = rsi[-1]
    prev_rsi = rsi[-2] if len(rsi) > 1 else current_rsi
    yellow_ma = sum(rsi[-YELLOW_MA_PERIOD:]) / YELLOW_MA_PERIOD if len(rsi) >= YELLOW_MA_PERIOD else None
    return current_rsi, prev_rsi, yellow_ma

def calculate_ema(closes, period):
    if len(closes) < period:
        return None
    alpha = 2 / (period + 1)
    ema = sum(closes[:period]) / period  # Initial SMA
    for price in closes[period:]:
        ema = price * alpha + ema * (1 - alpha)
    return ema

def get_current_price():
    url = f"https://public-api.birdeye.so/defi/price?address={str(TOKEN_ADDRESS)}"
    resp = requests.get(url, headers=BIRDEYE_HEADERS)
    resp.raise_for_status()
    data = resp.json()
    if not data.get("success"):
        raise ValueError("Birdeye price error")
    return float(data["data"]["value"])

def get_token_balance(token_mint: Pubkey) -> int:
    opts = TokenAccountOpts(mint=token_mint)
    resp = rpc_client.get_token_accounts_by_owner(keypair.pubkey(), opts)
    if not resp.value:
        return 0
    ata = resp.value[0].pubkey
    bal = rpc_client.get_token_account_balance(ata)
    return int(bal.value.amount) if bal.value else 0

# ────────────────────────────────────────────────
# SWAP EXECUTION
# ────────────────────────────────────────────────

def execute_swap(from_mint: Pubkey, to_mint: Pubkey):
    print(f"Trying swap: {from_mint} → {to_mint}")
    is_buy = from_mint == USDC_MINT
    slippage_bps = 50

    if is_buy:
        usdc_raw = get_token_balance(USDC_MINT)
        if usdc_raw < MIN_USDC_FOR_TRADE * 1_000_000:
            print(f"Not enough USDC: {usdc_raw / 1_000_000:.4f} < {MIN_USDC_FOR_TRADE}")
            return False
        amount_ui = (usdc_raw / 1_000_000) * POSITION_SIZE_PCT
        amount_lamports = int(amount_ui * 1_000_000)
        print(f"Buying with {amount_ui:.4f} USDC ({POSITION_SIZE_PCT*100}%)")
    else:
        raw = get_token_balance(from_mint)
        if raw == 0:
            print("Nothing to sell")
            return False
        amount_lamports = raw
        print(f"Selling full {raw} raw units")

    for attempt in range(1, 5):
        print(f"Attempt {attempt}/4 (slippage {slippage_bps} bps)...")
        try:
            params = {
                "inputMint": str(from_mint),
                "outputMint": str(to_mint),
                "amount": amount_lamports,
                "slippageBps": slippage_bps,
            }

            q = requests.get(f"{JUP_BASE}/quote", params=params, headers=JUPITER_HEADERS, timeout=15)
            q.raise_for_status()
            quote = q.json()

            out_amt = int(quote.get('outAmount', 0))
            if out_amt <= 0:
                print("Bad quote: zero output")
                continue

            print(f"Quote good: expected out ~{out_amt / 10**6:.4f} tokens")

            payload = {
                "quoteResponse": quote,
                "userPublicKey": str(keypair.pubkey()),
                "wrapAndUnwrapSol": True,
                "computeUnitPriceMicroLamports": 250000,
            }

            s = requests.post(f"{JUP_BASE}/swap", json=payload, headers=JUPITER_HEADERS, timeout=15)
            s.raise_for_status()
            data = s.json()

            tx_bytes = base64.b64decode(data["swapTransaction"])
            if len(tx_bytes) < 200:
                print(f"Tx too short: {len(tx_bytes)} bytes")
                continue

            unsigned_tx = VersionedTransaction.from_bytes(tx_bytes)
            tx = VersionedTransaction(unsigned_tx.message, [keypair])

            sim = rpc_client.simulate_transaction(tx)
            if sim.value.err:
                err_str = str(sim.value.err).lower()
                print(f"Simulation failed: {sim.value.err}")
                if "slippage" in err_str:
                    slippage_bps = min(1000, slippage_bps + 100)
                continue

            print("Simulation passed → sending...")

            sig_resp = rpc_client.send_raw_transaction(
                bytes(tx),
                opts=TxOpts(
                    skip_preflight=True,
                    preflight_commitment=Confirmed,
                    max_retries=3
                )
            )
            sig = sig_resp.value

            print(f"Transaction sent: {sig}")

            conf = rpc_client.confirm_transaction(sig, commitment=Confirmed)
            if conf.value:
                print(f"✅ SUCCESS! Signature: {sig}")
                return True
            else:
                print(f"Confirmation failed for {sig}")

        except requests.exceptions.HTTPError as http_err:
            print(f"HTTP error: {http_err}")
            if 'q' in locals():
                print("Jupiter error message:", q.text[:500])
            continue
        except Exception as e:
            print(f"Attempt {attempt} error: {type(e).__name__}: {e}")
            if attempt < 4:
                time.sleep(3)

    print("All attempts failed - check balance, liquidity, or API key")
    return False

# ────────────────────────────────────────────────
# MAIN LOOP
# ────────────────────────────────────────────────

state = load_state()
print(f"[{datetime.now()}] Robot STARTED with Helius super speed! (Trailing stop: {TRAIL_PCT*100:.0f}% from high)")

while True:
    try:
        closes = get_historical_prices()
        current_rsi, prev_rsi, yellow_ma = calculate_rsi_and_ma(closes)

        if current_rsi is None or yellow_ma is None:
            print(f"Not enough data yet ({len(closes)} candles)")
            time.sleep(CHECK_INTERVAL_MIN * 60)
            continue

        ema50 = calculate_ema(closes, EMA_PERIOD)
        if ema50 is None:
            print(f"Not enough data for EMA50 ({len(closes)} candles)")
            time.sleep(CHECK_INTERVAL_MIN * 60)
            continue

        current_price = get_current_price()
        print(f"[{datetime.now()}] Price: ${current_price:.6f} | RSI: {current_rsi:.2f} | Yellow MA: {yellow_ma:.2f} | EMA50: {ema50:.6f}")

        crossover_up   = (prev_rsi <= yellow_ma) and (current_rsi > yellow_ma)
        crossover_down = (prev_rsi >= yellow_ma) and (current_rsi < yellow_ma)

        holding_token = state["position"] == str(TOKEN_ADDRESS)

        # ──── SELL CONDITIONS (when holding) ────
        sold = False

        if holding_token and state["entry_price"] is not None:
            # 1. Initial hard stop loss (safety net)
            if current_price <= state["entry_price"] * (1 - STOP_LOSS_PCT):
                print(f"INITIAL STOP LOSS triggered at {current_price:.6f} (entry {state['entry_price']:.6f})")
                if execute_swap(TOKEN_ADDRESS, USDC_MINT):
                    state["position"] = "USDC"
                    state["entry_price"] = None
                    state["max_price"] = None
                    save_state(state)
                    sold = True

            # 2. Trailing stop from high
            if not sold:
                state["max_price"] = max(state["max_price"] or 0, current_price)
                trailing_stop_price = state["max_price"] * (1 - TRAIL_PCT)
                print(f"  Max so far: ${state['max_price']:.6f} | Trailing stop sits at ${trailing_stop_price:.6f}")

                if current_price <= trailing_stop_price:
                    print(f"TRAILING STOP triggered at {current_price:.6f} (high {state['max_price']:.6f}, trail {TRAIL_PCT*100:.0f}%)")
                    if execute_swap(TOKEN_ADDRESS, USDC_MINT):
                        state["position"] = "USDC"
                        state["entry_price"] = None
                        state["max_price"] = None
                        save_state(state)
                        sold = True

            # 3. RSI crossover sell
            if not sold and crossover_down:
                print("↓ RSI DOWN CROSSOVER → Selling")
                if execute_swap(TOKEN_ADDRESS, USDC_MINT):
                    state["position"] = "USDC"
                    state["entry_price"] = None
                    state["max_price"] = None
                    save_state(state)
                    sold = True

        # ──── BUY CONDITION ────
        if crossover_up and not holding_token and current_price > ema50:  # Added trend filter
            print("↑ RSI UP CROSSOVER → Buying (price > EMA50)")
            if execute_swap(USDC_MINT, TOKEN_ADDRESS):
                state["position"] = str(TOKEN_ADDRESS)
                state["entry_price"] = current_price
                state["max_price"] = current_price   # init trailing high
                save_state(state)

    except Exception as e:
        print(f"Oops: {e}")

    time.sleep(CHECK_INTERVAL_MIN * 60)
