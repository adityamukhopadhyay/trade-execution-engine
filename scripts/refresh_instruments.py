"""Rebuild data/instruments.json from the public Angel One and Upstox instrument masters (manual, needs network)."""
from __future__ import annotations

import argparse
import gzip
import json
import sys
import urllib.request
from pathlib import Path

ANGEL_URL = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
UPSTOX_URL = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"
OUT = Path(__file__).resolve().parent.parent / "data" / "instruments.json"
FIELDS = {"symbol", "exchange", "isin", "name", "angel_token"}
DEFAULT_SYMBOLS = (
    "ADANIENT ADANIPORTS APOLLOHOSP ASIANPAINT AXISBANK BAJAJ-AUTO BAJFINANCE BAJAJFINSV BEL BHARTIARTL "
    "CIPLA COALINDIA DRREDDY EICHERMOT ETERNAL GRASIM HCLTECH HDFCBANK HDFCLIFE HEROMOTOCO HINDALCO "
    "HINDUNILVR ICICIBANK INDUSINDBK INFY ITC JIOFIN JSWSTEEL KOTAKBANK LT M&M MARUTI NESTLEIND NTPC ONGC "
    "POWERGRID RELIANCE SBILIFE SBIN SHRIRAMFIN SUNPHARMA TATACONSUM TATAMOTORS TATASTEEL TCS TECHM TITAN "
    "TRENT ULTRACEMCO WIPRO DMART LTIM PIDILITIND HAVELLS DABUR BANKBARODA PNB VEDL GAIL IOC BPCL SIEMENS "
    "DLF BRITANNIA HAL").split()
REJECTME = {"symbol": "REJECTME", "exchange": "NSE", "isin": "INE000000000",
            "name": "Fake row: the paper broker rejects it after acceptance", "angel_token": "0"}


def fetch(url: str, cache: Path | None) -> bytes:
    """Download `url`, reusing the copy in `cache` when one is there."""
    local = cache / url.rsplit("/", 1)[1] if cache else None
    if local and local.exists():
        return local.read_bytes()
    with urllib.request.urlopen(url, timeout=180) as resp:  # noqa: S310  fixed https URLs
        data = resp.read()
    if local:
        local.write_bytes(data)
    return data


def build(symbols: list[str], cache: Path | None) -> list[dict]:
    angel = {r["symbol"][:-3]: r["token"] for r in json.loads(fetch(ANGEL_URL, cache))
             if r.get("exch_seg") == "NSE" and r.get("symbol", "").endswith("-EQ")}
    upstox = {r["trading_symbol"]: r for r in json.loads(gzip.decompress(fetch(UPSTOX_URL, cache)))
              if r.get("segment") == "NSE_EQ" and r.get("instrument_type") == "EQ"}
    missing = [s for s in symbols if s not in angel or s not in upstox]
    if missing:
        print(f"skipped (not in both masters): {', '.join(missing)}", file=sys.stderr)
    return [{"symbol": s, "exchange": "NSE", "isin": upstox[s]["isin"], "name": upstox[s]["name"],
             "angel_token": angel[s]} for s in symbols if s not in missing] + [REJECTME]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbols", help="comma-separated NSE symbols (default: the NIFTY universe above)")
    ap.add_argument("--cache", type=Path, help="directory holding/receiving the downloaded masters")
    ap.add_argument("--check", action="store_true", help="validate the current file offline and exit")
    args = ap.parse_args()
    if args.check:
        rows = json.loads(OUT.read_text())
        bad = [r for r in rows if set(r) != FIELDS or not r["symbol"] or len(r["isin"]) != 12]
        print(f"{OUT}: {len(rows)} rows, {len(bad)} malformed")
        return 1 if bad else 0
    symbols = [s.strip().upper() for s in (args.symbols.split(",") if args.symbols else DEFAULT_SYMBOLS)]
    rows = build(symbols, args.cache)
    OUT.write_text(json.dumps(rows, indent=1) + "\n")
    print(f"wrote {len(rows)} rows to {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
