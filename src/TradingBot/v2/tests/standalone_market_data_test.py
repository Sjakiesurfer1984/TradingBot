from __future__ import annotations

import os
import time
from pathlib import Path

import requests
from dotenv import load_dotenv


def _load_env() -> None:
    """
    Load .env from a few sensible locations so the test behaves like main_v2.

    Why
    - Your main entrypoint loads .env, but this standalone test did not.
    - When you run from different working directories, a relative .env lookup can fail.
    """
    # Current working directory first (most common).
    load_dotenv(dotenv_path=Path.cwd() / ".env", override=False)

    # Also try repo root patterns, based on this file location:
    # src/TradingBot/v2/test.py -> repo root is 4 parents up.
    # Path(__file__) is src/TradingBot/v2/test.py, and resolve() makes it absolute.
    here = Path(__file__).resolve()
    for candidate in [
        # here.parents[0] / ".env",  # v2/.env (unlikely but cheap to try)
        here.parents[0] / ".env",  # v2/.env (unlikely but cheap to try)
        here.parents[1] / ".env",  # TradingBot/.env
        here.parents[2] / ".env",  # src/.env
        here.parents[3] / ".env",  # project root/.env (likely)
        here.parents[4] / ".env",  # one more up, just in case
    ]:
        if candidate.exists():
            load_dotenv(dotenv_path=candidate, override=False)
            break


def main() -> None:
    _load_env()

    api_key = os.getenv("ALPACA_PAPER_API_KEY")
    api_secret = os.getenv("ALPACA_PAPER_API_SECRET")

    if not api_key or not api_secret:
        print("ERROR: Missing ALPACA_PAPER_API_KEY or ALPACA_PAPER_API_SECRET")
        print(f"PWD: {Path.cwd()}")
        print(f"Has key: {bool(api_key)}")
        print(f"Has secret: {bool(api_secret)}")
        return

    url = "https://data.alpaca.markets/v2/stocks/SPY/quotes/latest"
    headers = {
        "APCA-API-KEY-ID": api_key,
        "APCA-API-SECRET-KEY": api_secret,
    }
    params = {"feed": "iex"}  # try "sip" later only if needed
    timeout = (3.0, 10.0)

    print("=== Alpaca Market Data Test ===")
    print(f"PWD: {Path.cwd()}\n")
    print(f"URL: {url}\n")
    print(f"Params: {params}\n")
    print(f"Timeout: {timeout}\n")
    print("Sending request...\n")

    t0 = time.monotonic()
    try:
        r = requests.get(url, headers=headers, params=params, timeout=timeout)
        elapsed = time.monotonic() - t0

        print(f"HTTP status: {r.status_code}")
        print(f"Elapsed: {elapsed:.3f}s")
        print("Body:")
        try:
            print(r.json())
        except Exception:
            print(r.text)

    except requests.exceptions.Timeout:
        elapsed = time.monotonic() - t0
        print(f"TIMEOUT after {elapsed:.3f}s")

    except requests.exceptions.RequestException as exc:
        elapsed = time.monotonic() - t0
        print(f"REQUEST ERROR after {elapsed:.3f}s")
        print(exc)


if __name__ == "__main__":
    main()
