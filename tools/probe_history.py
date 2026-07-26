"""
Probe #2 — adâncimea istoricului.
═════════════════════════════════
Probe-ul #1 a arătat CE răspunde. Ăsta arată CÂT DE ÎNAPOI, ceea ce decide
dacă un filtru poate fi backtestat sau rămâne doar cosmetic în alertă.

Regula pe care o aplicăm: dacă o serie n-are istoric suficient, nu intră în
scor. Poate intra ca linie informativă, atât.
"""

import sys
import time
from datetime import datetime, timezone

import requests

DAY = 86_400_000
NOW = int(time.time() * 1000)


def ts(ms):
    return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def show(label, oldest_ms, newest_ms, n, note=""):
    span = (int(newest_ms) - int(oldest_ms)) / DAY
    print(f"[{label}]\n    puncte: {n} · interval: {ts(oldest_ms)} → {ts(newest_ms)}"
          f" · adâncime: {span:.1f} zile {note}\n")


def probe_okx_ratio():
    """OKX rubik long/short account ratio — poziționarea retail, granularitate 5m."""
    for period in ("5m", "1H"):
        try:
            r = requests.get(
                "https://www.okx.com/api/v5/rubik/stat/contracts/long-short-account-ratio",
                params={"ccy": "DOGE", "period": period,
                        "begin": str(NOW - 365 * DAY), "end": str(NOW)},
                timeout=20)
            d = r.json().get("data") or []
            if not d:
                print(f"[okx long/short {period}] gol: {r.text[:160]}\n")
                continue
            tss = sorted(int(x[0]) for x in d)
            show(f"okx long/short ratio {period}", tss[0], tss[-1], len(d),
                 f"· val. recentă {d[0][1]}")
        except Exception as e:
            print(f"[okx long/short {period}] EXCEPȚIE {type(e).__name__}: {e}\n")


def probe_okx_oi():
    """OKX rubik open interest + volume istoric."""
    try:
        r = requests.get(
            "https://www.okx.com/api/v5/rubik/stat/contracts/open-interest-volume",
            params={"ccy": "DOGE", "period": "5m",
                    "begin": str(NOW - 365 * DAY), "end": str(NOW)},
            timeout=20)
        d = r.json().get("data") or []
        if not d:
            print(f"[okx OI istoric] gol: {r.text[:160]}\n")
            return
        tss = sorted(int(x[0]) for x in d)
        show("okx OI+volum istoric 5m", tss[0], tss[-1], len(d))
    except Exception as e:
        print(f"[okx OI istoric] EXCEPȚIE {type(e).__name__}: {e}\n")


def probe_mexc_funding():
    """MEXC funding — ultima pagină spune cât de departe merge."""
    try:
        r = requests.get("https://contract.mexc.com/api/v1/contract/funding_rate/history",
                         params={"symbol": "DOGE_USDT", "page_num": 1, "page_size": 100},
                         timeout=20)
        d = r.json()["data"]
        total_page = d["totalPage"]
        first = d["resultList"]
        r2 = requests.get("https://contract.mexc.com/api/v1/contract/funding_rate/history",
                          params={"symbol": "DOGE_USDT", "page_num": total_page,
                                  "page_size": 100}, timeout=20)
        last = r2.json()["data"]["resultList"]
        show("mexc funding istoric", last[-1]["settleTime"], first[0]["settleTime"],
             d["totalCount"], f"· {total_page} pagini")
    except Exception as e:
        print(f"[mexc funding istoric] EXCEPȚIE {type(e).__name__}: {e}\n")


def probe_binance_klines():
    """Mirror-ul Binance: cât de departe merg M5 cu startTime (baza backtestului)."""
    for months in (3, 6, 12):
        start = NOW - months * 30 * DAY
        try:
            r = requests.get("https://data-api.binance.vision/api/v3/klines",
                             params={"symbol": "DOGEUSDT", "interval": "5m",
                                     "startTime": start, "limit": 1000}, timeout=20)
            d = r.json()
            if not d:
                print(f"[binance mirror M5 -{months} luni] GOL\n")
                continue
            print(f"[binance mirror M5 -{months} luni] prima lumânare: {ts(d[0][0])}"
                  f" · cerut de la {ts(start)} · {len(d)} bare · taker-buy={d[0][9]}\n")
        except Exception as e:
            print(f"[binance mirror M5 -{months} luni] EXCEPȚIE {type(e).__name__}: {e}\n")


def main():
    print("═" * 70)
    print("ADÂNCIMEA ISTORICULUI — ce se poate backtesta")
    print("═" * 70 + "\n")
    probe_binance_klines()
    probe_okx_ratio()
    probe_okx_oi()
    probe_mexc_funding()
    return 0


if __name__ == "__main__":
    sys.exit(main())
