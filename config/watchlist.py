import os

from dotenv import load_dotenv

load_dotenv()

_DEFAULT = "BTC/USDT,ETH/USDT,SOL/USDT"

WATCHLIST: list[str] = [
    s.strip()
    for s in os.getenv("WATCHLIST", _DEFAULT).split(",")
    if s.strip()
]
