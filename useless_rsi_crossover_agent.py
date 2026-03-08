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

CHECK_INTERVAL_MIN = 1
RSI_PERIOD         = 14
YELLOW_MA_PERIOD   = 9
STOP_LOSS_PCT      = 0.05          # initial hard stop (safety net)
TRAIL_PCT          = 0.15          # trailing % from high (tune: 0.12 tight, 0.18–0.20 loose)
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
    from_unix = now_unix - (3600 * 24 * 2)
    url = f"https://public-api.birdeye.so/defi/history_price?address={str(TOKEN_ADDRESS)}&address_type=token&type=15m&time_from={from_unix}&time_to={now_unix}&ui_amount_mode=raw"
    resp = requests.get(url, headers=BIRDEYE_HEADERS)
    resp.raise_for_status()
    data = resp.json()
    if not data.get
