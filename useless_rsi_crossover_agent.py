import requests
import time
import json
import os
from datetime import datetime
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solana.rpc.api import Client
from solana.transaction import Transaction
from jupiter_python_sdk.jupiter import Jupiter  # We'll add this library

# CONFIG
BIRDEYE_API_KEY = os.getenv("BIRDEYE_API_KEY")
if not BIRDEYE_API_KEY:
    print("ERROR: No Birdeye key!")
    exit(1)

TOKEN_ADDRESS = Pubkey.from_string("Dz9mQ9NzkBcCsuGPFJ3r1bS4wgqKMHBPiVuniW8Mbonk")
USDC_MINT = Pubkey.from_string("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v")
CHECK_INTERVAL_MIN = 5  # Start slower to be safe
RSI_PERIOD = 14
YELLOW_MA_PERIOD = 9
STOP_LOSS_PCT = 0.05
POSITION_SIZE_PCT = 0.30
STATE_FILE = "useless_agent_state.json"

# Load private key from env (base58 string)
private_key_str = os.getenv("SOLANA_PRIVATE_KEY")
if private_key_str:
    keypair = Keypair.from_base58_string(private_key_str)
else:
    # If using seed phrase instead
    seed_phrase = os.getenv("SOLANA_SEED_PHRASE")
    if seed_phrase:
        from bip_utils import Bip39SeedGenerator, Bip44, Bip44Coins, Bip44Changes
        seed_bytes = Bip39SeedGenerator(seed_phrase).Generate()
        bip44_mst_ctx = Bip44.FromSeed(seed_bytes, Bip44Coins.SOLANA)
        bip44_acc_ctx = bip44_mst_ctx.Purpose().Coin().Account(0).Change(Bip44Changes.CHAIN_EXT).AddressIndex(0)
        keypair = Keypair.from_seed(bip44_acc_ctx.PrivateKey().Raw().ToBytes())
    else:
        print("ERROR: No SOLANA_PRIVATE_KEY or SOLANA_SEED_PHRASE in Variables!")
        exit(1)

# Solana connection
rpc_client = Client("https://api.mainnet-beta.solana.com")

# Jupiter swap client
jupiter = Jupiter(rpc_client)

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
    # Your original function - keep it
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
    # Your original RSI calc - keep it
    # ... (paste your calculate_rsi_and_ma function here from previous code)
    pass  # replace with your full function

def get_current_price():
    url = f"https://public-api.birdeye.so/defi/price?address={str(TOKEN_ADDRESS)}"
    response = requests.get(url, headers=HEADERS)
    response.raise_for_status()
    data = response.json()
    if not data.get("success"):
        raise ValueError("Birdeye price error")
    return float(data["data"]["value"])

def execute_swap(from_mint, to_mint, amount):
    print(f"Swapping {amount} of {from_mint} to {to_mint}")
    try:
        quote = jupiter.quote(
            input_mint=from_mint,
            output_mint=to_mint,
            amount=int(amount * 1_000_000),  # USDC has 6 decimals
            slippage_bps=50  # 0.5%
        )
        swap_tx = jupiter.swap_transaction(
            quote_response=quote,
            user_public_key=keypair.pubkey()
        )
        tx = Transaction.deserialize(swap_tx.swapTransaction)
        tx.sign([keypair])
        signature = rpc_client.send_transaction(tx)
        print(f"Swap SUCCESS! Signature: {signature.value}")
        return True
    except Exception as e:
        print(f"Swap failed: {e}")
        return False

# MAIN LOOP
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
                print(f"STOP-LOSS TRIGGERED at {current_price}")
                if execute_swap(TOKEN_ADDRESS, USDC_MINT, 1.0):  # sell all
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
