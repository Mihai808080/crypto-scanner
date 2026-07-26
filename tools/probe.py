"""
Probe temporar de endpoint-uri — rulat DOAR manual, din GitHub Actions.
═══════════════════════════════════════════════════════════════════════
Runnerele GitHub sunt geo-blocate pe Binance (451), de aceea nu putem
presupune nimic despre disponibilitatea unei burse: trebuie măsurat de
acolo, nu de pe laptop.

Ce ne interesează:
  - o sursă de M5 CU taker-buy volume (CVD-ul din SFP depinde de el);
  - funding rate, curent + istoric (poziționarea aglomerată);
  - open interest, curent + istoric (aglomerare care crește vs. se închide).

Rulează: Actions → probe-endpoints → Run workflow. Nu e importat de scanner.
"""

import json
import sys
import time

import requests

SYM = "DOGEUSDT"
MEXC_SYM = "DOGE_USDT"
BYBIT_SYM = "DOGEUSDT"
OKX_SYM = "DOGE-USDT-SWAP"

# (etichetă, url, params, ce căutăm în răspuns)
PROBES = [
    # ── surse de lumânări ───────────────────────────────────────────
    ("binance-mirror spot klines (baseline cunoscut bun)",
     "https://data-api.binance.vision/api/v3/klines",
     {"symbol": SYM, "interval": "5m", "limit": 3}, "taker-buy la index 9"),
    ("binance FUTURES klines (așteptat 451)",
     "https://fapi.binance.com/fapi/v1/klines",
     {"symbol": SYM, "interval": "5m", "limit": 3}, "taker-buy la index 9"),
    ("mexc contract klines",
     "https://contract.mexc.com/api/v1/contract/kline/" + MEXC_SYM,
     {"interval": "Min5", "start": int(time.time()) - 3600}, "fără taker-buy"),
    ("bybit linear klines",
     "https://api.bybit.com/v5/market/kline",
     {"category": "linear", "symbol": BYBIT_SYM, "interval": "5", "limit": 3}, "fără taker-buy"),
    ("okx swap candles",
     "https://www.okx.com/api/v5/market/candles",
     {"instId": OKX_SYM, "bar": "5m", "limit": 3}, "fără taker-buy"),

    # ── funding ─────────────────────────────────────────────────────
    ("mexc funding curent",
     "https://contract.mexc.com/api/v1/contract/funding_rate/" + MEXC_SYM, {}, "fundingRate"),
    ("mexc funding ISTORIC",
     "https://contract.mexc.com/api/v1/contract/funding_rate/history",
     {"symbol": MEXC_SYM, "page_num": 1, "page_size": 100}, "listă de rate"),
    ("bybit funding istoric",
     "https://api.bybit.com/v5/market/funding/history",
     {"category": "linear", "symbol": BYBIT_SYM, "limit": 100}, "listă de rate"),
    ("okx funding istoric",
     "https://www.okx.com/api/v5/public/funding-rate-history",
     {"instId": OKX_SYM, "limit": 100}, "listă de rate"),

    # ── open interest ───────────────────────────────────────────────
    ("mexc ticker (holdVol = OI curent)",
     "https://contract.mexc.com/api/v1/contract/ticker", {"symbol": MEXC_SYM}, "holdVol"),
    ("bybit OI ISTORIC",
     "https://api.bybit.com/v5/market/open-interest",
     {"category": "linear", "symbol": BYBIT_SYM, "intervalTime": "5min", "limit": 50}, "serie OI"),
    ("okx OI curent",
     "https://www.okx.com/api/v5/public/open-interest",
     {"instType": "SWAP", "instId": OKX_SYM}, "oi"),

    # ── poziționare retail (asta e „counter-telemetry"-ul real) ─────
    ("binance long/short account ratio (așteptat 451)",
     "https://fapi.binance.com/futures/data/globalLongShortAccountRatio",
     {"symbol": SYM, "period": "15m", "limit": 5}, "longAccount/shortAccount"),
    ("bybit long/short account ratio",
     "https://api.bybit.com/v5/market/account-ratio",
     {"category": "linear", "symbol": BYBIT_SYM, "period": "15min", "limit": 5}, "buyRatio/sellRatio"),
    ("okx long/short account ratio",
     "https://www.okx.com/api/v5/rubik/stat/contracts/long-short-account-ratio",
     {"ccy": "DOGE", "period": "5m"}, "raport long/short"),
]


def main():
    ok, bad = [], []
    for label, url, params, want in PROBES:
        try:
            r = requests.get(url, params=params, timeout=20)
            body = r.text[:220].replace("\n", " ")
            if r.status_code == 200:
                # 200 nu înseamnă automat date: MEXC/OKX/Bybit împachetează
                # erorile în corp cu success=false / code != "0".
                try:
                    j = r.json()
                    empty = (
                        (isinstance(j, dict) and j.get("success") is False)
                        or (isinstance(j, dict) and str(j.get("code", "0")) not in ("0", "00000", "None"))
                        or (isinstance(j, list) and not j)
                    )
                except json.JSONDecodeError:
                    empty = True
                (bad if empty else ok).append(label)
                flag = "PAYLOAD GOL/EROARE" if empty else "OK"
                print(f"[{flag}] {label}\n    {r.status_code} · caut: {want}\n    {body}\n")
            else:
                bad.append(label)
                print(f"[HTTP {r.status_code}] {label}\n    {body}\n")
        except Exception as e:
            bad.append(label)
            print(f"[EXCEPȚIE] {label}\n    {type(e).__name__}: {e}\n")

    print("═" * 70)
    print(f"MERG ({len(ok)}):")
    for s in ok:
        print(f"  ✓ {s}")
    print(f"NU MERG ({len(bad)}):")
    for s in bad:
        print(f"  ✗ {s}")
    # Probe-ul e informativ: nu pică jobul dacă o bursă e blocată, asta e chiar
    # rezultatul pe care îl măsurăm.
    return 0


if __name__ == "__main__":
    sys.exit(main())
