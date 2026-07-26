"""
Sursa de date — strat comun pentru scanner.py și sfp.py
════════════════════════════════════════════════════════
Extras din scanner.py ca să existe UN singur loc care știe de unde vin
lumânările. Motivul concret: sfp.py avea propriul fetch pe fapi.binance.com,
care răspunde 451 de pe runnerele GitHub — deci SFP-ul, singura strategie cu
edge demonstrat în backtest, nu a alertat niciodată din cloud (vezi logurile:
"Eroare SFP DOGEUSDT: 451"). Cu stratul ăsta comun, o reparație de sursă se
face o dată și e valabilă peste tot.

Ordinea de rezolvare per simbol: mirror spot Binance → spot MEXC → futures MEXC.
Doar mirror-ul Binance dă taker-buy volume, deci doar acolo CVD-ul e real.
"""

import logging
import os
import time

import requests

log = logging.getLogger("datasrc")

# fapi.binance.com ȘI api.binance.com răspund 451 (Unavailable For Legal
# Reasons) de pe runnerele GitHub Actions. data-api.binance.vision e mirror-ul
# public de date spot, accesibil de acolo, cu aceeași formă de răspuns —
# inclusiv taker-buy volume la index 9, deci CVD-ul rămâne real.
BINANCE_BASE = os.environ.get("BINANCE_BASE", "https://data-api.binance.vision/api/v3")
# Fallback pentru monedele care nu există pe spot Binance (perp-only: HYPE,
# ASTER, 1000BONK, MOCA, POPCAT etc.).
MEXC_BASE = "https://contract.mexc.com/api/v1/contract"
MEXC_SPOT_BASE = "https://api.mexc.com/api/v3"

_src = {}  # symbol -> "binance" | "mexc_spot" | "mexc_fut" (rezolvat o singură dată)

_MEXC_IV = {
    "1m": "Min1", "5m": "Min5", "15m": "Min15", "30m": "Min30",
    "1h": "Min60", "4h": "Hour4", "8h": "Hour8", "1d": "Day1",
}
_IV_SEC = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800,
           "1h": 3600, "4h": 14400, "8h": 28800, "1d": 86400}
# Spot MEXC folosește "60m" în loc de "1h" (restul intervalelor coincid).
_MEXC_SPOT_IV = {"1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m",
                 "1h": "60m", "4h": "4h", "1d": "1d"}


def _get(url, params=None, timeout=15, tries=3):
    """GET cu retry + backoff pe 429/418/5xx. Ridică ultima eroare dacă nu reușește."""
    last = None
    for i in range(tries):
        try:
            r = requests.get(url, params=params, timeout=timeout)
            if r.status_code in (418, 429) or r.status_code >= 500:
                wait = 3 * (2 ** i)
                log.warning(f"HTTP {r.status_code} de la {url} — reîncerc în {wait}s")
                last = requests.HTTPError(f"{r.status_code} de la {url}")
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            last = e
            status = getattr(getattr(e, "response", None), "status_code", None)
            # Simbol inexistent / blocat geografic — n-are rost să reîncerc.
            if status in (400, 404, 451):
                raise
            time.sleep(2 ** i)
    raise last


def _mexc_sym(symbol):
    s = symbol.upper()
    return f"{s[:-4]}_USDT" if s.endswith("USDT") else s


def _mexc_data(path, params=None, tries=3):
    """GET pe MEXC + validare payload ({'success':true,'data':...}).
    MEXC returnează ocazional success=false / data goală sub rafală — reîncearcă."""
    last = None
    for i in range(tries):
        j = _get(f"{MEXC_BASE}/{path}", params).json() or {}
        data = j.get("data")
        if j.get("success") and data:
            return data
        last = j.get("message") or j.get("code")
        time.sleep(1 + i)
    raise ValueError(f"MEXC fără date pentru {path} ({last})")


def _mexc_klines(symbol, interval, limit):
    iv = _MEXC_IV.get(interval)
    sec = _IV_SEC.get(interval, 900)
    if not iv:
        raise ValueError(f"interval nesuportat pe MEXC: {interval}")
    start = int(time.time()) - (limit + 2) * sec
    d = _mexc_data(f"kline/{_mexc_sym(symbol)}", {"interval": iv, "start": start})
    ts = d.get("time") or []
    out = []
    for i in range(len(ts)):
        v = float(d["vol"][i])
        out.append({
            "t": int(ts[i]) * 1000, "o": float(d["open"][i]), "h": float(d["high"][i]),
            "l": float(d["low"][i]), "c": float(d["close"][i]), "v": v,
            # MEXC nu expune taker-buy volume → delta neutră (CVD nu inventează direcție).
            "tb": v / 2,
        })
    if not out:
        raise ValueError(f"MEXC n-a returnat lumânări pentru {symbol} {interval}")
    return out[-limit:]


def _mexc_spot_klines(symbol, interval, limit):
    """Klines de pe spot MEXC (format compatibil Binance, dar fără taker-buy)."""
    raw = _get(f"{MEXC_SPOT_BASE}/klines",
               {"symbol": symbol.upper(), "interval": _MEXC_SPOT_IV.get(interval, interval),
                "limit": limit}).json()
    if not raw:
        raise ValueError(f"MEXC spot fără lumânări pentru {symbol}")
    return [
        {
            "t": int(k[0]), "o": float(k[1]), "h": float(k[2]), "l": float(k[3]),
            "c": float(k[4]), "v": float(k[5]), "tb": float(k[5]) / 2,
        }
        for k in raw
    ]


def _resolve_src(symbol):
    """Alege sursa pentru un simbol, o dată, și o memorează.

    ATENȚIE la coliziunile de ticker: contractul futures MEXC poate fi cu totul
    alt activ decât tokenul cu același ticker (ex. EWT_USDT futures = 98.98 vs
    EWT spot = 0.2775). De aceea spot-ul MEXC are prioritate față de futures,
    iar futures se acceptă doar dacă spot-ul nu listează deloc simbolul.
    """
    if symbol in _src:
        return _src[symbol]
    try:
        _get(f"{BINANCE_BASE}/klines", {"symbol": symbol, "interval": "15m", "limit": 1})
        _src[symbol] = "binance"
    except Exception as e:
        try:
            _get(f"{MEXC_SPOT_BASE}/klines",
                 {"symbol": symbol.upper(), "interval": "15m", "limit": 1})
            _src[symbol] = "mexc_spot"
            log.info(f"{symbol}: nu e pe spot Binance ({e}) — folosesc spot MEXC")
        except Exception as e2:
            _src[symbol] = "mexc_fut"
            log.info(f"{symbol}: nici pe spot MEXC ({e2}) — folosesc futures MEXC")
    return _src[symbol]


def has_real_cvd(symbol):
    """True doar unde avem taker-buy volume real. Semnalele care depind de
    divergența CVD (SFP) n-au ce căuta pe o sursă care nu-l expune."""
    return _resolve_src(symbol) == "binance"


def get_klines(symbol, interval, limit):
    src = _resolve_src(symbol)
    if src == "binance":
        r = _get(f"{BINANCE_BASE}/klines",
                 {"symbol": symbol, "interval": interval, "limit": limit})
        # k[9] = taker buy base volume — permite delta de agresiune REALĂ, nu estimată
        return [
            {
                "t": int(k[0]), "o": float(k[1]), "h": float(k[2]), "l": float(k[3]),
                "c": float(k[4]), "v": float(k[5]), "tb": float(k[9]),
            }
            for k in r.json()
        ]
    if src == "mexc_spot":
        return _mexc_spot_klines(symbol, interval, limit)
    return _mexc_klines(symbol, interval, limit)


def get_klines_range(symbol, interval, start_ms, end_ms):
    """Istoric paginat, pentru backtest. Doar mirror-ul Binance suportă
    startTime/endTime în forma asta; pe restul surselor ridicăm, ca backtestul
    să nu ruleze în tăcere pe date incomplete."""
    if _resolve_src(symbol) != "binance":
        raise ValueError(f"{symbol}: istoric paginat disponibil doar pe mirror-ul Binance")
    out, cur = [], start_ms
    while cur < end_ms:
        r = _get(f"{BINANCE_BASE}/klines",
                 {"symbol": symbol, "interval": interval,
                  "startTime": cur, "endTime": end_ms, "limit": 1000})
        batch = r.json()
        if not batch:
            break
        out.extend(
            {
                "t": int(k[0]), "o": float(k[1]), "h": float(k[2]), "l": float(k[3]),
                "c": float(k[4]), "v": float(k[5]), "tb": float(k[9]),
            }
            for k in batch
        )
        nxt = int(batch[-1][0]) + 1
        if nxt <= cur:
            break
        cur = nxt
        time.sleep(0.25)  # politicos cu rate-limit-ul
    return out


def get_ticker(symbol):
    src = _resolve_src(symbol)
    if src == "binance":
        d = _get(f"{BINANCE_BASE}/ticker/24hr", {"symbol": symbol}).json()
        return float(d["lastPrice"]), float(d["priceChangePercent"])
    if src == "mexc_spot":
        d = _get(f"{MEXC_SPOT_BASE}/ticker/24hr", {"symbol": symbol.upper()}).json()
        return float(d["lastPrice"]), float(d.get("priceChangePercent", 0))
    d = _mexc_data("ticker", {"symbol": _mexc_sym(symbol)})
    return float(d["lastPrice"]), float(d.get("riseFallRate", 0)) * 100


def get_price(symbol):
    """Doar ultimul preț — pentru alertele de preț ale userului."""
    src = _resolve_src(symbol)
    if src == "binance":
        return float(_get(f"{BINANCE_BASE}/ticker/price", {"symbol": symbol}).json()["price"])
    if src == "mexc_spot":
        return float(_get(f"{MEXC_SPOT_BASE}/ticker/price",
                          {"symbol": symbol.upper()}).json()["price"])
    d = _mexc_data("ticker", {"symbol": _mexc_sym(symbol)})
    return float(d["lastPrice"])


# ─────────────────────────────────────────────
# FUNDING — de pe MEXC contract (Binance e 451 din Actions)
# ─────────────────────────────────────────────
def get_funding(symbol, limit=100):
    """Istoric de funding, cel mai recent ULTIMUL (ca la Binance, ca să nu
    trebuiască schimbat consumatorul). Listă goală dacă sursa nu răspunde —
    apelantul degradează elegant, nu crapă."""
    try:
        d = _mexc_data("funding_rate/history",
                       {"symbol": _mexc_sym(symbol), "page_num": 1, "page_size": limit})
        rows = d.get("resultList") if isinstance(d, dict) else d
        out = [
            {"t": int(f["settleTime"]), "r": float(f["fundingRate"])}
            for f in (rows or []) if f.get("settleTime") is not None
        ]
        out.sort(key=lambda x: x["t"])
        return out
    except Exception as e:
        log.warning(f"Funding indisponibil pentru {symbol}: {e}")
        return []


def get_funding_current(symbol):
    """Rata de funding curentă, sau None."""
    try:
        d = _mexc_data(f"funding_rate/{_mexc_sym(symbol)}")
        return float(d["fundingRate"])
    except Exception as e:
        log.warning(f"Funding curent indisponibil pentru {symbol}: {e}")
        return None


def get_open_interest(symbol):
    """Open interest curent (holdVol pe MEXC), sau None."""
    try:
        d = _mexc_data("ticker", {"symbol": _mexc_sym(symbol)})
        oi = d.get("holdVol")
        return float(oi) if oi is not None else None
    except Exception as e:
        log.warning(f"OI indisponibil pentru {symbol}: {e}")
        return None
