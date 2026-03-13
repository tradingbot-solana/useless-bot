import sys
import os
import time
import requests
import pandas as pd
import base64
from datetime import datetime
from solders.pubkey import Pubkey
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction
from solana.rpc.api import Client
from solana.rpc.types import TxOpts
from spl.token.client import Token
from spl.token.constants import TOKEN_PROGRAM_ID

# ────────────────────────────────────────────────
# Immediate debug prints – first thing in the script
# ────────────────────────────────────────────────
print("DEBUG: Script file has started execution", flush=True)
print(f"DEBUG: Python version: {sys.version}", flush=True)
print(f"DEBUG: Current working directory: {os.getcwd()}", flush=True)
print(f"DEBUG: os.environ keys present: {list(os.environ.keys())}", flush=True)
print("DEBUG: Attempting to load environment variables...", flush=True)

from dotenv import load_dotenv
load_dotenv()

print("DEBUG: dotenv loaded (if .env file was present)", flush=True)

BIRDEYE_API_KEY = os.getenv("BIRDEYE_API_KEY")
HELIUS_API_KEY = os.getenv("HELIUS_API_KEY")
SOLANA_PRIVATE_KEY = os.getenv("SOLANA_PRIVATE_KEY")
SOLANA_PUBLIC_ADDRESS = os.getenv("SOLANA_PUBLIC_ADDRESS")

print(f"DEBUG: BIRDEYE_API_KEY is set: {bool(BIRDEYE_API_KEY)}", flush=True)
print(f"DEBUG: HELIUS_API_KEY is set: {bool(HELIUS_API_KEY)}", flush=True)
print(f"DEBUG: SOLANA_PRIVATE_KEY is set: {bool(SOLANA_PRIVATE_KEY)}", flush=True)
print(f"DEBUG: SOLANA_PUBLIC_ADDRESS is set: {bool(SOLANA_PUBLIC_ADDRESS)}", flush=True)

# Configurable params
TOKEN_MINT = "Dz9mQ9NzkBcCsuGPFJ3r1bS4wgqKMHBPiVuniW8Mbonk"
TRADE_SIZE_SOL = 0.5
SLIPPAGE_BPS = 50
TIMEFRAME = "1m"
HISTORY_BARS = 200
RSI_PERIOD = 14
SMA_PERIOD = 50
BB_PERIOD = 20
BB_STD = 2.0
RSI_BUY_THRESH = 30
RSI_SELL_THRESH = 70
SMA_FLAT_THRESH = 0.01
POLL_INTERVAL_SEC = 60
PRICE_CHECK_SEC = 5
TP_PCT = 0.01
SL_PCT = -0.01

# Constants
SOL_MINT = "So11111111111111111111111111111111111111112"
BIRDEYE_BASE_URL = "https://public-api.birdeye.so"
JUPITER_QUOTE_URL = "https://quote-api.jup.ag/v6/quote"
JUPITER_SWAP_URL = "https://quote-api.jup.ag/v6/swap"
HELIUS_RPC_URL = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
LAMPORTS_PER_SOL = 1_000_000_000

print("DEBUG: Initializing Solana client and keypair...", flush=True)
try:
    rpc_client = Client(HELIUS_RPC_URL)
    keypair = Keypair.from_base58_string(SOLANA_PRIVATE_KEY)
    wallet_pubkey = Pubkey.from_string(SOLANA_PUBLIC_ADDRESS)
    print("DEBUG: Solana client and keypair initialized successfully", flush=True)
except Exception as e:
    print(f"DEBUG: CRITICAL - Failed to initialize keypair or client: {str(e)}", flush=True)
    raise

token_mint_pubkey = Pubkey.from_string(TOKEN_MINT)
token_client = Token(rpc_client, token_mint_pubkey, TOKEN_PROGRAM_ID, keypair)

def get_unix_time():
    return int(time.time())

def get_ohlcv(address, timeframe, bars):
    print("DEBUG: Fetching OHLCV data...", flush=True)
    time_to = get_unix_time()
    time_from = time_to - (bars * 60)
    url = f"{BIRDEYE_BASE_URL}/defi/ohlcv"
    params = {
        "address": address,
        "type": timeframe,
        "time_from": time_from,
        "time_to": time_to,
        "currency": "usd",
        "ui_amount_mode": "raw"
    }
    headers = {"x-api-key": BIRDEYE_API_KEY, "x-chain": "solana"}
    response = requests.get(url, params=params, headers=headers)
    response.raise_for_status()

    data = response.json()
    if not data.get("success", False):
        raise Exception(f"BirdEye OHLCV failed: {data.get('message', data)}")

    items = data.get("data", {}).get("items", [])
    if not items:
        raise Exception("BirdEye returned empty OHLCV items list")

    df = pd.DataFrame(items)
    df = df.rename(columns={"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume", "unixTime": "unixTime"})

    required_cols = ["unixTime", "open", "high", "low", "close", "volume"]
    missing = [col for col in required_cols if col not in df.columns]
    if missing:
        raise KeyError(f"Missing columns: {missing}. Got: {list(df.columns)}")

    df = df[required_cols]
    df["unixTime"] = pd.to_datetime(df["unixTime"], unit="s")
    print("DEBUG: OHLCV data fetched and processed", flush=True)
    return df

def get_current_price(address):
    print("DEBUG: Fetching current price...", flush=True)
    url = f"{BIRDEYE_BASE_URL}/defi/price"
    params = {"address": address}
    headers = {"x-api-key": BIRDEYE_API_KEY, "x-chain": "solana"}
    response = requests.get(url, params=params, headers=headers)
    response.raise_for_status()
    data = response.json()
    if not data.get("success"):
        raise Exception(f"BirdEye price failed: {data}")
    price = data["data"]["value"]
    print(f"DEBUG: Current price: {price}", flush=True)
    return price

def compute_indicators(df):
    closes = df["close"]
    df["sma"] = closes.rolling(window=SMA_PERIOD).mean()
    delta = closes.diff()
    gain = delta.where(delta > 0, 0).rolling(window=RSI_PERIOD).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=RSI_PERIOD).mean()
    rs = gain / loss
    df["rsi"] = 100 - (100 / (1 + rs))
    df["bb_middle"] = closes.rolling(window=BB_PERIOD).mean()
    std = closes.rolling(window=BB_PERIOD).std()
    df["bb_upper"] = df["bb_middle"] + (BB_STD * std)
    df["bb_lower"] = df["bb_middle"] - (BB_STD * std)
    return df

def is_sma_flat(df):
    recent_sma = df["sma"].iloc[-10:]
    pct_change = (recent_sma.max() - recent_sma.min()) / recent_sma.min()
    return pct_change < SMA_FLAT_THRESH

def get_token_balance():
    try:
        mint_info = token_client.get_mint(token_mint_pubkey)
        decimals = mint_info.decimals
        balance_resp = token_client.get_balance(wallet_pubkey)
        bal = balance_resp.value.ui_amount or 0.0
        print(f"DEBUG: Token balance: {bal}", flush=True)
        return bal
    except Exception:
        print("DEBUG: No token account or balance fetch failed", flush=True)
        return 0.0

def execute_swap(is_buy, amount_lamports):
    print(f"DEBUG: Executing swap (buy={is_buy})...", flush=True)
    input_mint = SOL_MINT if is_buy else TOKEN_MINT
    output_mint = TOKEN_MINT if is_buy else SOL_MINT
    quote_params = {
        "inputMint": input_mint,
        "outputMint": output_mint,
        "amount": amount_lamports,
        "slippageBps": SLIPPAGE_BPS
    }
    quote_resp = requests.get(JUPITER_QUOTE_URL, params=quote_params).json()

    swap_body = {
        "quoteResponse": quote_resp,
        "userPublicKey": str(wallet_pubkey),
        "wrapAndUnwrapSol": True,
        "computeUnitPriceMicroLamports": "auto",
        "prioritizationFeeLamports": "auto"
    }
    swap_resp = requests.post(JUPITER_SWAP_URL, json=swap_body).json()
    swap_tx_b64 = swap_resp["swapTransaction"]

    tx_bytes = base64.b64decode(swap_tx_b64)
    tx = VersionedTransaction.from_bytes(tx_bytes)
    tx.sign([keypair])
    sig = rpc_client.send_transaction(tx, opts=TxOpts(skip_preflight=True)).value
    print(f"DEBUG: Swap transaction sent: {sig}", flush=True)
    return sig

def confirm_tx(sig):
    print(f"DEBUG: Confirming tx {sig}...", flush=True)
    for _ in range(30):
        status = rpc_client.get_signature_statuses([sig]).value[0]
        if status and status.confirmation_status in ("processed", "confirmed", "finalized"):
            print("DEBUG: Transaction confirmed", flush=True)
            return True
        time.sleep(1)
    print("DEBUG: Transaction confirmation timeout", flush=True)
    return False

# ────────────────────────────────────────────────
# Main bot loop
# ────────────────────────────────────────────────

print("DEBUG: Entering main loop", flush=True)

position = False
entry_price = None

while True:
    print(f"[{datetime.now()}] DEBUG: Main loop iteration start", flush=True)
    try:
        df = get_ohlcv(TOKEN_MINT, TIMEFRAME, HISTORY_BARS)
        df = compute_indicators(df)
        current_price = get_current_price(TOKEN_MINT)
        last_close = df["close"].iloc[-1]
        last_rsi = df["rsi"].iloc[-1]
        last_bb_lower = df["bb_lower"].iloc[-1]
        last_bb_upper = df["bb_upper"].iloc[-1]

        if not is_sma_flat(df):
            print(f"[{datetime.now()}] Market not ranging → skip", flush=True)
            time.sleep(POLL_INTERVAL_SEC)
            continue

        token_bal = get_token_balance()
        position = token_bal > 0.000001

        if not position:
            if last_rsi < RSI_BUY_THRESH and last_close < last_bb_lower:
                print(f"[{datetime.now()}] BUY signal @ {current_price:.8f}", flush=True)
                amount_lamports = int(TRADE_SIZE_SOL * LAMPORTS_PER_SOL)
                sig = execute_swap(True, amount_lamports)
                if confirm_tx(sig):
                    entry_price = current_price
                    position = True
                    print(f"[{datetime.now()}] Buy executed ≈ {entry_price:.8f}", flush=True)
        else:
            pct_change = (current_price - entry_price) / entry_price
            if (last_rsi > RSI_SELL_THRESH or
                last_close > last_bb_upper or
                pct_change >= TP_PCT or
                pct_change <= SL_PCT):
                print(f"[{datetime.now()}] SELL signal @ {current_price:.8f} (pct: {pct_change:+.2%})", flush=True)
                mint_info = token_client.get_mint(token_mint_pubkey)
                decimals = mint_info.decimals
                amount_to_sell = int(token_bal * (10 ** decimals))
                sig = execute_swap(False, amount_to_sell)
                if confirm_tx(sig):
                    entry_price = None
                    position = False
                    print(f"[{datetime.now()}] Sold ≈ {current_price:.8f}", flush=True)

        # Tight monitoring for TP/SL
        start = time.time()
        while time.time() - start < POLL_INTERVAL_SEC:
            if position:
                current_price = get_current_price(TOKEN_MINT)
                pct_change = (current_price - entry_price) / entry_price
                if pct_change >= TP_PCT or pct_change <= SL_PCT:
                    print(f"[{datetime.now()}] TP/SL hit @ {current_price:.8f} (pct: {pct_change:+.2%})", flush=True)
                    mint_info = token_client.get_mint(token_mint_pubkey)
                    decimals = mint_info.decimals
                    amount_to_sell = int(token_bal * (10 ** decimals))
                    sig = execute_swap(False, amount_to_sell)
                    if confirm_tx(sig):
                        entry_price = None
                        position = False
                    break
            time.sleep(PRICE_CHECK_SEC)

    except Exception as e:
        print(f"[{datetime.now()}] DEBUG: Top-level exception caught: {str(e)}", flush=True)
        import traceback
        traceback.print_exc(file=sys.stdout)
        print("DEBUG: Sleeping 60s before retry...", flush=True)
        time.sleep(60)
