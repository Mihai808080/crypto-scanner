"""
Confluence Engine — Background Scanner
═══════════════════════════════════════
Rulează non-stop, scanează watchlist.txt la fiecare 5 minute,
calculează Confluence Score (aceeași logică ca dashboard-ul HTML)
și trimite alerte pe Telegram pentru monedele cu CS >= prag.

Nu necesită telefonul sau dashboard-ul deschis — rulează pe server.
"""

import os
import time
import json
import logging
from datetime import datetime, timezone
import requests

from fmt import fmt_price  # zecimale adaptate la mărimea prețului (vezi fmt.py)
import sfp  # Playbook 1: Sweep & Reclaim — alerte SETUP (execuție discreționară)

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s")
log = logging.getLogger("confluence")

# ─────────────────────────────────────────────
# CONFIG — citit din variabile de mediu (Railway Settings → Variables)
# ─────────────────────────────────────────────
TG_TOKEN = os.environ.get("TG_TOKEN", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")
MIN_SCORE = int(os.environ.get("MIN_SCORE", "80"))  # prag pe scara nouă post-Markov (7a)
# Timeframe de EXECUȚIE. Implicit 4h: costul per trade e 0.03 R vs 0.16 R pe 15m,
# singura schimbare cu efect măsurat asupra rentabilității. 15m rămâne opțiune
# (EXEC_TF=15m), dar NU e testat-profitabil. Contextul (EMA) rămâne 1h + 4h.
EXEC_TF = os.environ.get("EXEC_TF", "4h")
# Costuri de execuție, pentru ajustarea scorului la costul real al monedei (7c).
FEE_PER_SIDE = float(os.environ.get("FEE_PER_SIDE", "0.0005"))
SCAN_INTERVAL_SEC = int(os.environ.get("SCAN_INTERVAL_SEC", "300"))  # 5 min
WATCHLIST_FILE = os.environ.get("WATCHLIST_FILE", "watchlist.txt")
# SFP doar pe perechile cu edge demonstrat în backtest (DOGE clar, SOL marginal).
# NU adăuga BTC — backtestul a arătat expectancy negativ acolo.
SFP_SYMBOLS = [
    s.strip().upper()
    for s in os.environ.get("SFP_SYMBOLS", "DOGEUSDT,SOLUSDT").split(",")
    if s.strip()
]
# RUN_ONCE=1 → o singură trecere apoi ieși (pentru cron / GitHub Actions).
# Gol/0 → bucla clasică while-True (pentru rulare pe server always-on / local).
RUN_ONCE = os.environ.get("RUN_ONCE", "") == "1"
# Alerte de preț definite de user din UI, stocate în Netlify Blobs.
# ALERTS_URL = https://<site>.netlify.app/api/alerts ; ALERTS_KEY = CI_ALERT_KEY.
# Goale → funcția e sărită (nimic nu se strică dacă nu sunt setate).
ALERTS_URL = os.environ.get("ALERTS_URL", "")
ALERTS_KEY = os.environ.get("ALERTS_KEY", "")
# Watchlist din cloud (Netlify Blobs) — listă de {sym, cat, strat} scrisă din UI.
# Gol → fallback la watchlist.txt (toate pe strategia 'conf').
WATCHLIST_URL = os.environ.get("WATCHLIST_URL", "")
# Fișier de stare persistat între rulări (anti-spam). Pe GitHub Actions e
# restaurat/salvat prin actions/cache; local e doar un fișier lângă scanner.
STATE_FILE = os.environ.get("STATE_FILE", "scan_state.json")

# Sursa de date. ATENȚIE: fapi.binance.com ȘI api.binance.com răspund cu
# 451 (Unavailable For Legal Reasons) de pe runnerele GitHub Actions — de aceea
# scannerul din cloud nu trimitea nimic. data-api.binance.vision e mirror-ul
# public de date Binance (spot) și e accesibil de acolo; are aceeași formă de
# răspuns (inclusiv taker-buy volume la index 9, deci CVD-ul rămâne real).
BINANCE_BASE = os.environ.get("BINANCE_BASE", "https://data-api.binance.vision/api/v3")
# Fallback pentru monedele care nu există pe spot Binance (perp-only: HYPE,
# ASTER, 1000BONK, MOCA, POPCAT etc.) — futures MEXC, accesibil din Actions.
MEXC_BASE = "https://contract.mexc.com/api/v1/contract"
MEXC_SPOT_BASE = "https://api.mexc.com/api/v3"
_src = {}  # symbol -> "binance" | "mexc_spot" | "mexc_fut" (rezolvat o singură dată)
_MEXC_IV = {
    "1m": "Min1", "5m": "Min5", "15m": "Min15", "30m": "Min30",
    "1h": "Min60", "4h": "Hour4", "8h": "Hour8", "1d": "Day1",
}
_IV_SEC = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400, "8h": 28800, "1d": 86400}
# Oprire curată înainte de limita de 6h a unui job GitHub Actions (0 = fără limită).
MAX_RUNTIME_SEC = int(os.environ.get("MAX_RUNTIME_SEC", "0"))

# Anti-spam: ultima direcție/alertă trimisă per simbol
state = {}  # { "BTCUSDT": {"prev_dir": 0, "last_alert_ts": 0} }
sweep_state = {}  # anti-spam separat pentru alertele de Liquidity Sweep


# ─────────────────────────────────────────────
# STARE PERSISTATĂ (pentru modul RUN_ONCE pe cron)
# ─────────────────────────────────────────────
def load_state():
    """Încarcă starea anti-spam din STATE_FILE (confluence + SFP)."""
    if not os.path.exists(STATE_FILE):
        return
    try:
        with open(STATE_FILE) as f:
            data = json.load(f)
        state.update(data.get("confluence", {}))
        sweep_state.update(data.get("sweep", {}))
        sfp._alerted.update(data.get("sfp", {}))
        log.info(f"Stare încărcată din {STATE_FILE}")
    except Exception as e:
        log.warning(f"Nu am putut încărca starea ({e}) — pornesc curat")


def save_state():
    """Salvează starea anti-spam în STATE_FILE."""
    try:
        with open(STATE_FILE, "w") as f:
            json.dump({"confluence": state, "sweep": sweep_state, "sfp": sfp._alerted}, f)
    except Exception as e:
        log.error(f"Nu am putut salva starea: {e}")


# ─────────────────────────────────────────────
# WATCHLIST
# ─────────────────────────────────────────────
def load_watchlist():
    """Citește watchlist.txt — un simbol pe linie, ex: BTCUSDT"""
    if not os.path.exists(WATCHLIST_FILE):
        default = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
        with open(WATCHLIST_FILE, "w") as f:
            f.write("\n".join(default) + "\n")
        return default
    with open(WATCHLIST_FILE) as f:
        syms = [l.strip().upper() for l in f if l.strip() and not l.strip().startswith("#")]
    return syms


# ─────────────────────────────────────────────
# BINANCE DATA
# ─────────────────────────────────────────────
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


# Spot MEXC folosește "60m" în loc de "1h" (restul intervalelor coincid cu Binance).
_MEXC_SPOT_IV = {"1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m",
                 "1h": "60m", "4h": "4h", "1d": "1d"}


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
    """Alege sursa pentru un simbol, o dată, și o memorează:
    binance (mirror spot) → mexc spot → mexc futures.

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
            _get(f"{MEXC_SPOT_BASE}/klines", {"symbol": symbol.upper(), "interval": "15m", "limit": 1})
            _src[symbol] = "mexc_spot"
            log.info(f"{symbol}: nu e pe spot Binance ({e}) — folosesc spot MEXC")
        except Exception as e2:
            _src[symbol] = "mexc_fut"
            log.info(f"{symbol}: nici pe spot MEXC ({e2}) — folosesc futures MEXC")
    return _src[symbol]


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


def closed_bars(kl, interval):
    """Taie lumânarea în formare — port 1:1 din closedBars() (confluence.js).

    Exchange-ul întoarce ca ULTIMĂ lumânare pe cea neterminată. Calculând
    scorul pe ea, un semnal apărea la minutul 3 dintr-o lumânare de 15 și
    putea dispărea până la închidere — alerta pleca pentru un setup care nu
    s-a confirmat niciodată. În plus, backtestul lucra pe bare închise, deci
    măsura cu totul altceva decât făcea scanner-ul.
    """
    if not kl or len(kl) < 2:
        return kl or []
    step = _IV_SEC.get(interval, 0) * 1000 or (kl[-1]["t"] - kl[-2]["t"])
    if not step:
        return kl
    now_ms = time.time() * 1000
    return kl[:-1] if kl[-1]["t"] + step > now_ms else kl


def get_closed_klines(symbol, interval, limit):
    """Lumânări ÎNCHISE, garantat `limit` bucăți (cerem una în plus)."""
    kl = closed_bars(get_klines(symbol, interval, limit + 1), interval)
    return kl[-limit:]


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


# Funding + Open Interest: futures. fapi.binance.com dă 451 de pe GitHub
# Actions, așa că le luăm de la Bybit (public, accesibil de acolo). Best-effort:
# dacă simbolul nu există pe Bybit linear sau API-ul pică, întoarcem None și
# mesajul pur și simplu omite liniile — nimic nu se strică.
BYBIT_BASE = "https://api.bybit.com/v5/market"


def bybit_funding_oi(symbol):
    """{'funding': fracție, 'oi_delta': fracție pe ~6h} sau None."""
    try:
        t = _get(f"{BYBIT_BASE}/tickers", {"category": "linear", "symbol": symbol.upper()}).json()
        lst = (t.get("result") or {}).get("list") or []
        if not lst:
            return None
        funding = float(lst[0].get("fundingRate")) if lst[0].get("fundingRate") not in (None, "") else None
        oi_delta = None
        try:
            o = _get(f"{BYBIT_BASE}/open-interest",
                     {"category": "linear", "symbol": symbol.upper(),
                      "intervalTime": "1h", "limit": 7}).json()
            pts = (o.get("result") or {}).get("list") or []
            # Bybit întoarce cel mai recent primul; delta = (nou - vechi)/vechi.
            if len(pts) >= 2:
                new = float(pts[0]["openInterest"])
                old = float(pts[-1]["openInterest"])
                if old > 0:
                    oi_delta = (new - old) / old
        except Exception:
            pass
        if funding is None and oi_delta is None:
            return None
        return {"funding": funding, "oi_delta": oi_delta}
    except Exception as e:
        log.info(f"{symbol}: funding/OI Bybit indisponibil ({e})")
        return None


_slip_cache = {}  # symbol -> (ts, slip) — spread-ul nu se schimbă de la minut la minut


def estimate_slippage(symbol, ttl=900):
    """Slippage estimat pe un leg, ca fracțiune — jumătate din spread-ul real
    bid/ask. Costul de 0.03 R a fost măsurat pe BTC/ETH/SOL/XRP; pe alt-urile
    mici e semnificativ mai mare, deci trebuie măsurat per monedă, nu presupus.
    Fallback conservator 0.0010 (0.1%) dacă orderbook-ul nu e disponibil.

    Rezultatul se memorează `ttl` secunde: la 200 de monede scanate la 5 minute,
    un apel în plus per monedă per trecere ar însemna ~57k cereri/zi degeaba.
    """
    hit = _slip_cache.get(symbol)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    src = _resolve_src(symbol)
    try:
        if src == "binance":
            d = _get(f"{BINANCE_BASE}/ticker/bookTicker", {"symbol": symbol}).json()
            bid, ask = float(d["bidPrice"]), float(d["askPrice"])
        elif src == "mexc_spot":
            d = _get(f"{MEXC_SPOT_BASE}/ticker/bookTicker", {"symbol": symbol.upper()}).json()
            bid, ask = float(d["bidPrice"]), float(d["askPrice"])
        else:
            d = _mexc_data("ticker", {"symbol": _mexc_sym(symbol)})
            bid, ask = float(d["bid1"]), float(d["ask1"])
        mid = (bid + ask) / 2
        if mid > 0 and ask >= bid:
            slip = (ask - bid) / mid / 2
            _slip_cache[symbol] = (time.time(), slip)
            return slip
    except Exception as e:
        log.info(f"{symbol}: spread indisponibil ({e}) — folosesc 0.10% estimat")
    _slip_cache[symbol] = (time.time(), 0.0010)
    return 0.0010


def cost_adjust(score, sl_pct, slip_est):
    """Ajustează scorul la costul real de execuție al monedei (7c).

    O monedă cu scor 85 și cost 0.4 R e mai proastă decât una cu scor 72 și cost
    0.05 R — de aceea clasarea și alertarea se fac pe score_adj, nu pe score.
    """
    if not sl_pct:
        return score, 0.0
    cost_r = (FEE_PER_SIDE * 2 + slip_est) / sl_pct
    return score * (1 - min(cost_r, 0.9)), cost_r


_btc_trend_cache = {"ts": 0, "dir": 0}


def btc_trend_dir():
    """Direcția trendului BTC pe 4h (EMA21), memorată 10 min — pentru
    'BTC Shield Alignment'. Refolosește sursa spot (nu e blocată)."""
    now = time.time()
    if now - _btc_trend_cache["ts"] < 600:
        return _btc_trend_cache["dir"]
    try:
        kl4h = get_closed_klines("BTCUSDT", "4h", 30)
        d = htf_trend_dir(kl4h)
    except Exception:
        d = 0
    _btc_trend_cache.update({"ts": now, "dir": d})
    return d


def get_price(symbol):
    """Doar ultimul preț — pentru alertele de preț ale userului."""
    src = _resolve_src(symbol)
    if src == "binance":
        return float(_get(f"{BINANCE_BASE}/ticker/price", {"symbol": symbol}).json()["price"])
    if src == "mexc_spot":
        return float(_get(f"{MEXC_SPOT_BASE}/ticker/price", {"symbol": symbol.upper()}).json()["price"])
    d = _mexc_data("ticker", {"symbol": _mexc_sym(symbol)})
    return float(d["lastPrice"])


# ─────────────────────────────────────────────
# INDICATORS (port 1:1 din dashboard JS)
# ─────────────────────────────────────────────
def ema(values, span):
    k = 2 / (span + 1)
    out = []
    e = values[0]
    for v in values:
        e = v * k + e * (1 - k)
        out.append(e)
    return out


def rsi(closes, period=14):
    deltas = [0] + [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    avg_gain = sum(d for d in deltas[1:period + 1] if d > 0) / period
    avg_loss = sum(-d for d in deltas[1:period + 1] if d < 0) / period
    out = [50] * (period + 1)
    for i in range(period + 1, len(closes)):
        d = deltas[i]
        avg_gain = (avg_gain * (period - 1) + max(d, 0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-d, 0)) / period
        out.append(100 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss))
    return out


def calc_cvd(klines):
    """Delta reală de agresiune: taker buy - taker sell = 2*takerBuy - volum.
    Binance o dă gratis în klines (câmpul 9); estimarea din poziția close-ului
    în range rămâne doar ca fallback."""
    cum = 0
    out = []
    for k in klines:
        if "tb" in k and k["v"] > 0:
            delta = 2 * k["tb"] - k["v"]
        else:
            rng = (k["h"] - k["l"]) or 1
            delta = ((k["c"] - k["l"]) / rng - (k["h"] - k["c"]) / rng) * k["v"]
        cum += delta
        out.append(cum)
    return out


def calc_fib(klines, lookback=100):
    recent = klines[-lookback:]
    high = max(k["h"] for k in recent)
    low = min(k["l"] for k in recent)
    diff = high - low
    return {
        "high": high, "low": low,
        "f236": high - diff * 0.236, "f382": high - diff * 0.382,
        "f500": high - diff * 0.500, "f618": high - diff * 0.618,
        "f786": high - diff * 0.786, "f886": high - diff * 0.886,
    }


def calc_atr(klines, period=14):
    """True Range mediat — bază pentru SL/TP dinamice."""
    if len(klines) < 2:
        return [0] * len(klines)
    tr = []
    for i, k in enumerate(klines):
        if i == 0:
            tr.append(k["h"] - k["l"])
        else:
            pc = klines[i - 1]["c"]
            tr.append(max(k["h"] - k["l"], abs(k["h"] - pc), abs(k["l"] - pc)))
    out = [tr[0]]
    for i in range(1, len(tr)):
        if i < period:
            out.append((out[i - 1] * i + tr[i]) / (i + 1))
        else:
            out.append((out[i - 1] * (period - 1) + tr[i]) / period)
    return out


def calc_adx(klines, period=14):
    """Putere de trend (0-100). >25 = trend valid, <20 = choppy — filtru anti-fals-semnal."""
    n = len(klines)
    if n < period * 2:
        return [0] * n
    plus_dm, minus_dm, tr = [0], [0], [klines[0]["h"] - klines[0]["l"]]
    for i in range(1, n):
        up_move = klines[i]["h"] - klines[i - 1]["h"]
        down_move = klines[i - 1]["l"] - klines[i]["l"]
        plus_dm.append(up_move if (up_move > down_move and up_move > 0) else 0)
        minus_dm.append(down_move if (down_move > up_move and down_move > 0) else 0)
        pc = klines[i - 1]["c"]
        tr.append(max(klines[i]["h"] - klines[i]["l"], abs(klines[i]["h"] - pc), abs(klines[i]["l"] - pc)))

    def smooth(arr):
        out = [sum(arr[:period])]
        for i in range(period, len(arr)):
            out.append(out[-1] - out[-1] / period + arr[i])
        return out

    sm_tr, sm_plus, sm_minus = smooth(tr), smooth(plus_dm), smooth(minus_dm)
    di_plus = [100 * p / t if t else 0 for t, p in zip(sm_tr, sm_plus)]
    di_minus = [100 * m / t if t else 0 for t, m in zip(sm_tr, sm_minus)]
    dx = [(100 * abs(p - m) / (p + m)) if (p + m) else 0 for p, m in zip(di_plus, di_minus)]
    if len(dx) < period:
        return [0] * n
    adx = [sum(dx[:period]) / period]
    for i in range(period, len(dx)):
        adx.append((adx[-1] * (period - 1) + dx[i]) / period)
    pad = n - len(adx)
    return ([adx[0]] * max(0, pad)) + adx


def is_good_session_at(ts_ms):
    # Londra + New York (8-22 UTC) — scris ca un singur interval
    h = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).hour
    return 8 <= h < 22


# ─────────────────────────────────────────────
# DETALII STIL "APEX" — toate din klines-urile pe care le avem deja (gratis)
# Nu ating scorul: sunt straturi informative pentru mesajul de alertă.
# ─────────────────────────────────────────────
def rvol(kl, win=30):
    """Volum relativ: ultima bară față de media ultimelor `win` (fără ea)."""
    if len(kl) < win + 2:
        return None
    vols = [k["v"] for k in kl[-(win + 1):-1]]
    avg = sum(vols) / len(vols) if vols else 0
    return kl[-1]["v"] / avg if avg else None


def volume_zscore(kl, win=30):
    """Cât de statistic-anormal e volumul ultimei bare (în deviații standard)."""
    if len(kl) < win + 2:
        return None
    vols = [k["v"] for k in kl[-(win + 1):-1]]
    n = len(vols)
    mean = sum(vols) / n
    var = sum((v - mean) ** 2 for v in vols) / n
    sd = var ** 0.5
    return (kl[-1]["v"] - mean) / sd if sd else None


def volume_poc(kl, lookback=100, bins=40):
    """Point of Control: prețul cu cel mai mult volum tranzacționat (aprox.
    din klines — distribuie volumul fiecărei bare la prețul ei tipic)."""
    recent = kl[-lookback:]
    if len(recent) < 5:
        return None
    hi = max(k["h"] for k in recent)
    lo = min(k["l"] for k in recent)
    if hi <= lo:
        return None
    step = (hi - lo) / bins
    buckets = [0.0] * bins
    for k in recent:
        tp = (k["h"] + k["l"] + k["c"]) / 3
        b = min(bins - 1, max(0, int((tp - lo) / step)))
        buckets[b] += k["v"]
    top = max(range(bins), key=lambda b: buckets[b])
    return lo + (top + 0.5) * step


def buy_vol_pct(kl, bars=1):
    """Procent de volum de cumpărare (taker buy) pe ultimele `bars` bare.
    Doar unde avem taker-buy real (Binance); pe MEXC 'tb' e neutru → ~50%."""
    seg = kl[-bars:]
    tot = sum(k["v"] for k in seg)
    buy = sum(k.get("tb", k["v"] / 2) for k in seg)
    return 100 * buy / tot if tot else None


def cvd_word(kl, win=20):
    """Direcția presiunii de agresiune pe ultima fereastră, în cuvinte."""
    cvd = calc_cvd(kl[-win:])
    if len(cvd) < 5:
        return None, 0
    d = cvd[-1] - cvd[-5]
    if d > 0:
        return "acumulare 🟢", 1
    if d < 0:
        return "distribuție 🔴", -1
    return "neutru", 0


def setup_label(direction, rsi_val, rvol_val, near_fib):
    """Nume de setup ales din stare (echivalentul etichetei Apex)."""
    long = direction == 1
    if rsi_val is not None and ((long and rsi_val < 40) or (not long and rsi_val > 60)):
        return "Oversold Rebound" if long else "Overbought Rejection"
    if rvol_val is not None and rvol_val >= 2:
        return "Momentum Breakout" if long else "Momentum Breakdown"
    if near_fib:
        return "Fib Support Bounce" if long else "Fib Resistance Reject"
    return "Bullish Confluence" if long else "Bearish Confluence"


def dca_ladder(price, sl_pct, direction, fracs=(0.3, 0.6, 0.9)):
    """Nivele de intrare scalată (DEFENDER-ele Apex), plasate ca fracțiuni din
    distanța până la SL — mereu ordonate între entry și stop, indiferent de ATR."""
    out = []
    for f in fracs:
        off = f * sl_pct
        lvl = price * (1 - direction * off)
        out.append((lvl, -off * 100))
    return out


def liq_prices(price, direction, levs=(2, 5), mmr=0.005):
    """Preț de lichidare REAL per levier (isolated). NU inventăm ca Apex un liq
    la -4.6% pentru 2× — un 2× real se lichidează pe la -50%."""
    out = []
    for L in levs:
        if direction == 1:
            liq = price * (1 - 1 / L + mmr)
        else:
            liq = price * (1 + 1 / L - mmr)
        out.append((L, liq, (liq - price) / price * 100))
    return out


# ─────────────────────────────────────────────
# CONFLUENCE SCORE — port 1:1 din dashboard
# ─────────────────────────────────────────────
def compute_confluence_score(price, cls15, kl15, kl1h, kl4h, bar_ts=None):
    factors = []
    total = 0

    # (Markov ELIMINAT: 0 puncte în 100% din cazuri pe date reale, corelație cu
    #  randamentul următoarelor 12 bare +0.015 (t=0.81) — factor mort. achievable
    #  scade de la 58 la 50, deci scorurile cresc ~16%; pragurile recalibrate.)

    # 1) MTF EMA Trend — max 20
    ema_pts, ema_dir = 0, 0
    cls1h = [k["c"] for k in kl1h]
    cls4h = [k["c"] for k in kl4h]
    if len(cls15) >= 50:
        e9, e21, e50 = ema(cls15, 9), ema(cls15, 21), ema(cls15, 50)
        l9, l21, l50 = e9[-1], e21[-1], e50[-1]
        bull15 = l9 > l21 > l50 and price > l50
        bear15 = l9 < l21 < l50 and price < l50
        if bull15:
            ema_pts += 8; ema_dir += 1
        if bear15:
            ema_pts += 8; ema_dir -= 1
        if len(cls1h) >= 21:
            e21_1h, e50_1h = ema(cls1h, 21), ema(cls1h, 50)
            bull1h = price > e21_1h[-1] > e50_1h[-1]
            bear1h = price < e21_1h[-1] < e50_1h[-1]
            # aliniere 1h — puncte DOAR dacă întărește direcția 15m (ema_dir)
            if bull1h and ema_dir > 0:
                ema_pts += 6
            if bear1h and ema_dir < 0:
                ema_pts += 6
        if len(cls4h) >= 21:
            e21_4h = ema(cls4h, 21)
            bull4h = price > e21_4h[-1]
            if bull4h and ema_dir > 0:
                ema_pts += 6
            elif not bull4h and ema_dir < 0:
                ema_pts += 6
        ema_pts = min(20, ema_pts)
    total += ema_pts
    factors.append(("MTF EMA Trend", ema_pts, 20, 1 if ema_dir > 0 else -1 if ema_dir < 0 else 0))

    # 3) RSI Momentum — max 10
    rsi_pts, rsi_dir = 0, 0
    if len(cls15) >= 20:
        r = rsi(cls15, 14)
        lr, pr = r[-1], r[-2] if len(r) > 1 else r[-1]
        if lr > 55 and pr < 55:
            rsi_pts, rsi_dir = 10, 1
        elif lr < 45 and pr > 45:
            rsi_pts, rsi_dir = 10, -1
        elif lr > 55:
            rsi_pts, rsi_dir = 5, 1
        elif lr < 45:
            rsi_pts, rsi_dir = 5, -1
    total += rsi_pts
    factors.append(("RSI Momentum", rsi_pts, 10, rsi_dir))

    # 4) Volume + CVD — max 10
    vol_pts, vol_dir = 0, 0
    if len(kl15) >= 20:
        vols = [k["v"] for k in kl15]
        avg_v = sum(vols[-20:-1]) / 19
        last_v = vols[-1]
        ratio = last_v / avg_v if avg_v else 1
        if ratio >= 2:
            vol_pts += 5
        elif ratio >= 1.3:
            vol_pts += 3
        cvd = calc_cvd(kl15[-20:])
        if cvd[-1] > cvd[-5 if len(cvd) >= 5 else 0]:
            vol_pts += 5; vol_dir = 1
        elif cvd[-1] < cvd[-5 if len(cvd) >= 5 else 0]:
            vol_pts += 5; vol_dir = -1
        vol_pts = min(10, vol_pts)
    total += vol_pts
    factors.append(("Volume + CVD", vol_pts, 10, vol_dir))

    # 5) Fibonacci — max 10
    fib_pts = 0
    if len(kl15) >= 50:
        fib = calc_fib(kl15, 100)
        tol = price * 0.002
        if abs(price - fib["f618"]) < tol * 2 or abs(price - fib["f786"]) < tol * 2 or abs(price - fib["f886"]) < tol * 2:
            fib_pts = 10
        elif abs(price - fib["f382"]) < tol * 2:
            fib_pts = 7
    total += fib_pts
    factors.append(("Fibonacci", fib_pts, 10, 0))

    # 6) OI + Funding — omis în server (necesită endpoint suplimentar; opțional de extins)
    # 7) Liquidity Sweep — omis în server (necesită istoric de swing-uri; opțional de extins)
    # Factorii omiși NU mai apar cu max>0: scorul se normalizează la ce e
    # implementat efectiv, altfel pragul MIN_SCORE devine imposibil de atins.
    factors.append(("OI + Funding", 0, 0, 0))
    factors.append(("Liq Sweep", 0, 0, 0))

    # Normalizare: 0-100 raportat la punctajul maxim REALIZABIL aici
    achievable = sum(mx for _, _, mx, _ in factors)  # 20+10+10+10 = 50 (era 58 cu Markov)
    total = round(100 * total / achievable) if achievable else 0

    dir_scores = {-1: 0, 0: 0, 1: 0}
    for name, pts, mx, d in factors:
        if d != 0:
            dir_scores[d] += pts
    final_dir = 1 if dir_scores[1] > dir_scores[-1] else -1 if dir_scores[-1] > dir_scores[1] else 0

    # 8) ADX — filtru de putere de trend (multiplicator, nu doar factor aditiv).
    # Cele mai multe semnale false vin din piață choppy; ADX<20 taie scorul drastic.
    adx_val, adx_mult = 0, 1.0
    if len(kl15) >= 30:
        adx_arr = calc_adx(kl15, 14)
        adx_val = adx_arr[-1] if adx_arr else 0
        if adx_val >= 35:
            adx_mult = 1.10
        elif adx_val >= 25:
            adx_mult = 1.0
        elif adx_val >= 20:
            adx_mult = 0.80
        else:
            adx_mult = 0.55
    factors.append(("ADX Trend Filter", 0, 0, 0))  # informativ, nu contribuie la dir_scores

    sess_ok = is_good_session_at(bar_ts) if bar_ts else True
    if not sess_ok:
        total = round(total * 0.7)
    total = round(total * adx_mult)
    total = max(0, min(100, total))

    # ATR pentru SL/TP dinamice
    atr14, atr_pct = None, None
    if len(kl15) >= 20:
        atr_arr = calc_atr(kl15, 14)
        atr14 = atr_arr[-1]
        atr_pct = atr14 / price if price else None

    return {
        "score": total, "dir": final_dir, "factors": factors, "sess_ok": sess_ok,
        "adx": adx_val, "adx_mult": adx_mult, "atr": atr14, "atr_pct": atr_pct,
    }


# ─────────────────────────────────────────────
# TELEGRAM
# ─────────────────────────────────────────────
def send_telegram(text):
    if not TG_TOKEN or not TG_CHAT_ID:
        log.warning("Telegram nu este configurat (TG_TOKEN / TG_CHAT_ID lipsesc)")
        return False
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML"}, timeout=10)
        ok = r.json().get("ok", False)
        if not ok:
            log.error(f"Telegram error: {r.text}")
        return ok
    except Exception as e:
        log.error(f"Telegram send failed: {e}")
        return False


def build_alert_message(symbol, cs, price, kl15=None, live_price=None, extras=None):
    """Mesaj în stil «Apex» — straturi de detaliu peste scorul de confluență.
    Toate nivelurile în PROCENTE (fără sume $, la cererea userului). Câmpurile
    de futures (funding/OI/BTC) vin prin `extras`; lipsa lor omite doar liniile."""
    is_long = cs["dir"] == 1
    extras = extras or {}
    atr_pct = cs.get("atr_pct") or 0.0035
    # Podea de 0.4%: sub asta, taxele round-trip (~0.1%) + slippage devin
    # o fracțiune prea mare din distanța de stop și edge-ul dispare.
    sl_pct = max(0.004, atr_pct * 1.2)
    tp1_pct, tp2_pct, tp3_pct = sl_pct * 1.5, sl_pct * 3, sl_pct * 5
    sl = price * (1 - sl_pct) if is_long else price * (1 + sl_pct)

    def tgt(pct):
        return price * (1 + pct) if is_long else price * (1 - pct)

    tp1, tp2, tp3 = tgt(tp1_pct), tgt(tp2_pct), tgt(tp3_pct)
    # Leverage temperat: 20× cu SL de câteva zecimi de procent înseamnă că
    # taxele+slippage-ul mănâncă o parte mare din edge; 5-10× e sustenabil.
    lev = 10 if cs["score"] >= 93 else 7 if cs["score"] >= 75 else 5  # tiere recalibrate post-Markov

    # --- DEFENDER / DCA (fracțiuni din distanța până la SL) ---
    dca = dca_ladder(price, sl_pct, cs["dir"])
    dca_lines = "".join(
        f"🛡 DCA {i+1} ({pct:+.1f}%):  ${fmt_price(lvl)}\n"
        for i, (lvl, pct) in enumerate(dca)
    )

    # --- Liq real (isolated) pentru levierul sugerat + referință conservatoare 2× ---
    liqs = liq_prices(price, cs["dir"], levs=(2, lev) if lev != 2 else (2,))
    liq_line = "⚙️ Liq real: " + " · ".join(
        f"{L}× ${fmt_price(lp)} ({lpct:+.1f}%)" for L, lp, lpct in liqs
    ) + "\n"

    # --- Detalii de order-flow / volum (din klines) ---
    detail_lines = ""
    setup = ""
    if kl15:
        rv = rvol(kl15)
        vz = volume_zscore(kl15)
        poc = volume_poc(kl15)
        bp = buy_vol_pct(kl15, bars=1)
        cvd_txt, _ = cvd_word(kl15)
        r_series = rsi([k["c"] for k in kl15], 14)
        rsi_val = r_series[-1] if r_series else None
        near_fib = any(n == "Fibonacci" and p > 0 for n, p, m, d in cs["factors"])
        setup = setup_label(cs["dir"], rsi_val, rv, near_fib)
        vol_bits = []
        if rv is not None:
            vol_bits.append(f"RVOL {rv:.1f}×")
        if vz is not None:
            vol_bits.append(f"Vol Z {vz:+.1f}σ")
        if vol_bits:
            detail_lines += "📊 " + " · ".join(vol_bits) + "\n"
        flow_bits = []
        if cvd_txt:
            flow_bits.append(f"CVD {cvd_txt}")
        if bp is not None:
            flow_bits.append(f"buy {bp:.0f}%")
        if flow_bits:
            detail_lines += "🌊 " + " · ".join(flow_bits) + "\n"
        if poc:
            detail_lines += f"🧲 POC: ${fmt_price(poc)}\n"

    # --- Funding / OI (Bybit) ---
    fund_bits = []
    if extras.get("funding") is not None:
        fund_bits.append(f"funding {extras['funding']*100:+.3f}%")
    if extras.get("oi_delta") is not None:
        arrow = "🟢" if extras["oi_delta"] >= 0 else "🔴"
        fund_bits.append(f"OI ~6h {extras['oi_delta']*100:+.1f}% {arrow}")
    fund_line = ("💸 " + " · ".join(fund_bits) + "\n") if fund_bits else ""

    # --- BTC Shield Alignment ---
    btc_line = ""
    bd = extras.get("btc_dir")
    if bd is not None:
        if bd == 0:
            btc_line = "🛡 BTC: neutru\n"
        else:
            aligned = bd == cs["dir"]
            word = "urcă" if bd == 1 else "coboară"
            btc_line = f"🛡 BTC: {'aliniat 🟢' if aligned else 'contra ⚠️'} ({word})\n"

    # --- Clasare în univers (7b): un scor singur nu spune nimic ---
    uni_line = ""
    if extras.get("uni_total"):
        uni_line = (f"🏁 Rang: <b>{extras['uni_rank']}/{extras['uni_total']}</b>"
                    f" · percentila {extras['uni_pct']:.0f}"
                    f" · mediana univers {extras['uni_median']:.0f}\n")
    # --- Cost real de execuție (7c) ---
    cost_line = ""
    if extras.get("cost_r") is not None:
        cost_line = (f"🧾 Cost: <b>{extras['cost_r']:.2f} R</b>"
                     f" · scor ajustat <b>{extras.get('score_adj', 0):.0f}</b>\n")

    factor_lines = "\n".join(
        f"{'✅' if pts >= mx * 0.6 else '⚠️'} {name}: {pts}/{mx}"
        for name, pts, mx, d in cs["factors"] if mx > 0
    )
    adx_line = f"📈 ADX: {cs.get('adx', 0):.0f} (×{cs.get('adx_mult', 1):.2f})\n"
    # Cât s-a mișcat prețul de la închiderea barei de semnal până acum — asta e
    # slippage-ul pe care îl plătești dacă intri în clipa asta.
    slip_line = ""
    if live_price and price:
        d = (live_price - price) / price * 100
        if abs(d) >= 0.05:
            slip_line = f"📍 Preț acum:  ${fmt_price(live_price)}  ({d:+.2f}% față de semnal)\n"
    sym_disp = symbol.replace("USDT", "/USDT")
    setup_txt = f"  ·  Setup: {setup}" if setup else ""
    return (
        f"📊 <b>{sym_disp} · {'LONG ▲' if is_long else 'SHORT ▼'} · {EXEC_TF}</b>\n"
        f"🧠 Score: <b>{cs['score']}/100</b>{setup_txt}\n"
        f"{uni_line}"
        f"{cost_line}"
        f"ℹ️ <i>Informativ (backtest: fără edge de intrare mecanică) — nu e semnal de execuție.</i>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"💰 Entry:    <b>${fmt_price(price)}</b>  <i>(close bară închisă)</i>\n"
        f"{slip_line}"
        f"{dca_lines}"
        f"🛑 SL (ATR {sl_pct*100:.1f}%): ${fmt_price(sl)}\n"
        f"🎯 TP1 (+{tp1_pct*100:.1f}%): ${fmt_price(tp1)} — vinde 25%\n"
        f"🎯 TP2 (+{tp2_pct*100:.1f}%): ${fmt_price(tp2)} — vinde 25%\n"
        f"🎯 TP3 (+{tp3_pct*100:.1f}%): ${fmt_price(tp3)} — vinde 50%\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"⚡ Leverage sugerat: <b>{lev}×</b>\n"
        f"{liq_line}"
        f"{fund_line}"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"{detail_lines}"
        f"{btc_line}"
        f"{adx_line}"
        f"🕐 {datetime.now().strftime('%H:%M:%S')}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"<b>Factori:</b>\n{factor_lines}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"⚠️ Nu e sfat financiar. Verifică manual."
    )


# ─────────────────────────────────────────────
# SCAN LOOP
# ─────────────────────────────────────────────
def evaluate_symbol(symbol):
    """Faza 1: calculează scorul, FĂRĂ să alerteze. Întoarce dict sau None.

    Alertarea e separată fiindcă un scor n-are sens singur: într-un scan de 200
    de monede, maximul e maximul a 200 de trageri dintr-o distribuție fără edge
    (în medie 3.3 SE peste zero). Trebuie clasat față de univers — vezi scan_pass.
    """
    try:
        # DOAR lumânări închise — semnalul trebuie să fie confirmat, nu în curs.
        # kl_exec = timeframe-ul de EXECUȚIE (implicit 4h); contextul rămâne 1h+4h.
        kl_exec = get_closed_klines(symbol, EXEC_TF, 200)
        kl1h = get_closed_klines(symbol, "1h", 60)
        kl4h = get_closed_klines(symbol, "4h", 30)
        if not kl_exec:
            log.warning(f"{symbol}: nicio lumânare închisă — sar peste")
            return None
        bar_ts = kl_exec[-1]["t"]
        # Prețul pentru scor = close-ul ultimei bare ÎNCHISE (ca în backtest).
        # Prețul live se ia separat, doar ca să arătăm cât s-a mișcat între timp.
        price = kl_exec[-1]["c"]
        try:
            live_price, _chg = get_ticker(symbol)
        except Exception:
            live_price = price
        cls = [k["c"] for k in kl_exec]
        cs = compute_confluence_score(price, cls, kl_exec, kl1h, kl4h, bar_ts=bar_ts)

        sl_pct = max(0.004, (cs.get("atr_pct") or 0.0035) * 1.2)
        slip_est = estimate_slippage(symbol)
        score_adj, cost_r = cost_adjust(cs["score"], sl_pct, slip_est)

        log.info(f"{symbol:12s} price=${fmt_price(price)}  CS={cs['score']}/100  "
                 f"adj={score_adj:.0f}  cost={cost_r:.2f}R  dir={cs['dir']}")
        return {"symbol": symbol, "cs": cs, "price": price, "live_price": live_price,
                "kl": kl_exec, "bar_ts": bar_ts, "score_adj": score_adj,
                "cost_r": cost_r, "slip_est": slip_est}
    except Exception as e:
        log.error(f"Eroare la scanarea {symbol}: {e}")
        return None


def alert_symbol(ev, uni):
    """Faza 2: alertează pentru o evaluare, dacă trece pragul și anti-spam-ul.
    `uni` = statisticile universului din bara curentă (rang, percentilă, mediană)."""
    symbol, cs, bar_ts = ev["symbol"], ev["cs"], ev["bar_ts"]
    st = state.setdefault(symbol, {"prev_dir": 0, "last_alert_ts": 0, "last_bar_ts": 0})
    now = time.time()
    if cs["dir"] == 0:
        st["prev_dir"] = 0
        return
    # Pragul se aplică pe scorul AJUSTAT LA COST, nu pe cel brut (7c).
    if not (
        ev["score_adj"] >= MIN_SCORE
        and cs["dir"] != st["prev_dir"]
        # O bară închisă e evaluată de mai multe ori (scanăm mai des decât
        # durează bara). Fără asta, aceeași lumânare putea alerta de 3 ori.
        and bar_ts != st.get("last_bar_ts")
        and now - st["last_alert_ts"] > 5 * 60
    ):
        return
    st["prev_dir"] = cs["dir"]
    st["last_alert_ts"] = now
    st["last_bar_ts"] = bar_ts
    # Datele de futures (Bybit) + BTC se cer DOAR acum, la alertă — nu
    # la fiecare scanare a fiecărui simbol (rare = puține cereri în plus).
    extras = {"btc_dir": btc_trend_dir()}
    fo = bybit_funding_oi(symbol)
    if fo:
        extras.update(fo)
    extras.update(uni)
    extras["score_adj"] = ev["score_adj"]
    extras["cost_r"] = ev["cost_r"]
    msg = build_alert_message(symbol, cs, ev["price"], kl15=ev["kl"],
                              live_price=ev["live_price"], extras=extras)
    if send_telegram(msg):
        log.info(f"  → Alertă trimisă pentru {symbol}")


# ─────────────────────────────────────────────
# LIQUIDITY SWEEP (SFP) — port 1:1 din src/lib/confluence.js (sweepSignal)
# Aceeași logică ca în browser (Watchlist), ca semnalul să fie identic.
# ─────────────────────────────────────────────
def htf_trend_dir(kl4h):
    if not kl4h or len(kl4h) < 21:
        return 0
    closes = [k["c"] for k in kl4h]
    e21 = ema(closes, 21)[-1]
    price = closes[-1]
    return 1 if price > e21 else -1 if price < e21 else 0


def find_pivots(kl, k=3):
    """Pivoturi confirmate: extremă strictă pe j±k."""
    lows, highs = [], []
    for j in range(k, len(kl) - k):
        is_low = is_high = True
        for m in range(j - k, j + k + 1):
            if m == j:
                continue
            if kl[m]["l"] <= kl[j]["l"]:
                is_low = False
            if kl[m]["h"] >= kl[j]["h"]:
                is_high = False
            if not is_low and not is_high:
                break
        if is_low:
            lows.append({"idx": j, "level": kl[j]["l"]})
        if is_high:
            highs.append({"idx": j, "level": kl[j]["h"]})
    return lows, highs


def detect_sweep(kl, pivot_k=3, lookback=80, confirm_window=8, recent_bars=0):
    """Sweep confirmat fără lookahead (vezi comentariile din confluence.js)."""
    n = len(kl)
    i = n - 1
    if n < lookback + pivot_k * 2 + 5:
        return None
    frm = max(0, i - lookback - recent_bars)
    lows, highs = find_pivots(kl[frm:i + 1], pivot_k)
    for p in lows:
        p["idx"] += frm
    for p in highs:
        p["idx"] += frm

    def scan_at(direction, f):
        pivots = lows if direction == 1 else highs
        s = f - 1
        while s >= f - confirm_window and s > frm:
            for p in range(len(pivots) - 1, -1, -1):
                j, level = pivots[p]["idx"], pivots[p]["level"]
                if j + pivot_k >= s:
                    continue
                if f - j > lookback:
                    break
                bs = kl[s]
                swept = (bs["l"] < level and bs["c"] > level) if direction == 1 \
                    else (bs["h"] > level and bs["c"] < level)
                if not swept:
                    continue
                spent = False
                for m in range(j + 1, s):
                    if (kl[m]["c"] < level) if direction == 1 else (kl[m]["c"] > level):
                        spent = True
                        break
                if spent:
                    continue
                holds, first = True, True
                for m in range(s + 1, f + 1):
                    if (kl[m]["l"] < bs["l"]) if direction == 1 else (kl[m]["h"] > bs["h"]):
                        holds = False
                        break
                    if m < f and ((kl[m]["c"] > bs["h"]) if direction == 1 else (kl[m]["c"] < bs["l"])):
                        first = False
                        break
                if not holds or not first:
                    continue
                confirmed = (kl[f]["c"] > bs["h"]) if direction == 1 else (kl[f]["c"] < bs["l"])
                if not confirmed:
                    continue
                return {
                    "dir": direction, "level": level, "sweepIdx": s,
                    "sweepExtreme": bs["l"] if direction == 1 else bs["h"],
                }
            s -= 1
        return None

    f = i
    while f >= i - recent_bars and f > 0:
        sw = scan_at(1, f) or scan_at(-1, f)
        if sw:
            alive = True
            for m in range(f + 1, i + 1):
                if sw["dir"] == 1:
                    ok = kl[m]["l"] >= sw["sweepExtreme"] and kl[m]["c"] > sw["level"]
                else:
                    ok = kl[m]["h"] <= sw["sweepExtreme"] and kl[m]["c"] < sw["level"]
                if not ok:
                    alive = False
                    break
            if alive:
                return sw
        f -= 1
    return None


def prev_month_dir(kl4h, ts):
    if not kl4h:
        return 0
    d = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
    m_start = datetime(d.year, d.month, 1, tzinfo=timezone.utc).timestamp() * 1000
    py, pm = (d.year - 1, 12) if d.month == 1 else (d.year, d.month - 1)
    p_start = datetime(py, pm, 1, tzinfo=timezone.utc).timestamp() * 1000
    prev = [k for k in kl4h if p_start <= k["t"] < m_start]
    if len(prev) < 100:
        return 0
    o, c = prev[0]["o"], prev[-1]["c"]
    return 1 if c > o else -1 if c < o else 0


def sweep_signal(kl, kl4h, bar_ts=None):
    """Scor 0-100 pentru un sweep confirmat la ultima bară, sau None."""
    sw = detect_sweep(kl)
    if not sw:
        return None
    direction, level = sw["dir"], sw["level"]
    sweep_idx, sweep_extreme = sw["sweepIdx"], sw["sweepExtreme"]
    price = kl[-1]["c"]
    total = 40  # evenimentul în sine

    atr_arr = calc_atr(kl, 14)
    atr_abs = (atr_arr[sweep_idx] if sweep_idx < len(atr_arr) else 0) or (atr_arr[-1] if atr_arr else 0)
    depth = (level - sweep_extreme) if direction == 1 else (sweep_extreme - level)
    if atr_abs > 0 and depth >= 0.5 * atr_abs:
        total += 10

    if sweep_idx >= 21:
        vols = [k["v"] for k in kl[sweep_idx - 20:sweep_idx]]
        avg_v = sum(vols) / len(vols) if vols else 0
        if avg_v and kl[sweep_idx]["v"] >= 1.3 * avg_v:
            total += 10

    r = rsi([k["c"] for k in kl[:sweep_idx + 1]], 14)[-1]
    if (direction == 1 and r < 35) or (direction == -1 and r > 65):
        total += 10

    if htf_trend_dir(kl4h) == direction:
        total += 15

    if bar_ts:
        dom = datetime.fromtimestamp(bar_ts / 1000, tz=timezone.utc).day
        pm = prev_month_dir(kl4h, bar_ts)
        # redus de la 15 la 5: pm == -direction etichetează toate sweep-urile unei
        # luni la fel → ~30 observații independente în 2,5 ani, iar 52% vs 39% e
        # la ~1.6 SE. Boost mic, nu factor dominant.
        if dom <= 12 and pm != 0 and pm == -direction:
            total += 5

    return {"score": min(100, total), "dir": direction}


def build_sweep_message(symbol, sig, price):
    is_long = sig["dir"] == 1
    disp = symbol.replace("USDT", "/USDT")
    return (
        f"🎯 <b>LIQUIDITY SWEEP</b>\n"
        f"⚡ <b>{disp}</b> · {EXEC_TF}\n\n"
        f"{'▲' if is_long else '▼'} <b>{'LONG' if is_long else 'SHORT'}</b> · sweep + reclaim confirmat\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"💰 Preț: <b>${fmt_price(price)}</b>\n"
        f"📊 Scor sweep: <b>{sig['score']}/100</b>\n"
        f"🕐 {datetime.now().strftime('%H:%M:%S')}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"⚠️ Nu e sfat financiar. Verifică manual."
    )


def scan_symbol_sweep(symbol):
    """Rulează Liquidity Sweep pe un simbol și alertează la sweep confirmat nou."""
    try:
        # Și sweep-ul doar pe bare închise: un „reclaim" la mijlocul barei se
        # poate anula până la închidere. Timeframe-ul de execuție = EXEC_TF (4h),
        # ca măsurătoarea (Sweep pooled pe 4h) și scanner-ul să vadă același lucru.
        kl15 = get_closed_klines(symbol, EXEC_TF, 200)
        kl4h = get_closed_klines(symbol, "4h", 30)
        if not kl15:
            return
        bar_ts = kl15[-1]["t"]
        sig = sweep_signal(kl15, kl4h, bar_ts)
        st = sweep_state.setdefault(symbol, {"prev_dir": 0, "last_alert_ts": 0, "last_bar_ts": 0})
        if not sig or sig["dir"] == 0:
            st["prev_dir"] = 0
            return
        price = kl15[-1]["c"]
        log.info(f"{symbol:12s} SWEEP score={sig['score']}/100  dir={sig['dir']}")
        now = time.time()
        if (
            sig["dir"] != st["prev_dir"]
            and bar_ts != st.get("last_bar_ts")
            and now - st.get("last_alert_ts", 0) > 5 * 60
        ):
            st["prev_dir"] = sig["dir"]
            st["last_alert_ts"] = now
            st["last_bar_ts"] = bar_ts
            if send_telegram(build_sweep_message(symbol, sig, price)):
                log.info(f"  → Alertă SWEEP trimisă pentru {symbol}")
    except Exception as e:
        log.error(f"Eroare la sweep {symbol}: {e}")


# ─────────────────────────────────────────────
# ALERTE DE PREȚ (user, din UI → Netlify Blobs)
# ─────────────────────────────────────────────
def check_price_alerts():
    """Citește alertele de preț ale userului din cloud, verifică prețul curent
    și trimite Telegram la declanșare — 24/7, fără browser deschis.
    Marchează alerta ca inactivă și scrie lista înapoi în cloud (anti-repeat)."""
    if not ALERTS_URL or not ALERTS_KEY:
        return
    try:
        r = requests.get(ALERTS_URL, headers={"x-ci-key": ALERTS_KEY}, timeout=10)
        r.raise_for_status()
        alerts = r.json()
    except Exception as e:
        log.warning(f"Nu am putut citi alertele de preț din cloud: {e}")
        return
    if not isinstance(alerts, list) or not alerts:
        return

    # Un singur fetch de preț per simbol, doar pentru alertele active.
    syms = sorted({a["sym"] for a in alerts if a.get("active") and a.get("sym")})
    prices = {}
    for s in syms:
        try:
            prices[s] = get_price(s)
        except Exception as e:
            log.warning(f"Preț indisponibil pentru {s}: {e}")

    changed = False
    for a in alerts:
        if not a.get("active"):
            continue
        p = prices.get(a.get("sym"))
        if p is None:
            continue
        target = float(a["price"])
        hit = p >= target if a.get("cond") == "above" else p <= target
        if hit:
            a["active"] = False
            a["triggeredAt"] = int(time.time() * 1000)
            a["triggeredPrice"] = p
            changed = True
            disp = a["sym"].replace("USDT", "/USDT")
            verb = "a trecut PESTE" if a.get("cond") == "above" else "a coborât SUB"
            send_telegram(
                f"🔔 <b>ALERTĂ DE PREȚ</b>\n"
                f"<b>{disp}</b> {verb} <b>{fmt_price(target)}</b>\n"
                f"💰 Preț curent: <b>{fmt_price(p)}</b>\n"
                f"🕐 {datetime.now().strftime('%H:%M:%S')}"
            )
            log.info(f"  → Alertă de preț declanșată: {a['sym']} @ {p}")

    if changed:
        try:
            requests.post(
                ALERTS_URL,
                headers={"x-ci-key": ALERTS_KEY, "Content-Type": "application/json"},
                json=alerts, timeout=10,
            )
        except Exception as e:
            log.error(f"Nu am putut actualiza alertele în cloud: {e}")


def load_watchlist_cloud():
    """Watchlist din cloud (Netlify Blobs) — listă de {sym, strat}.
    Fallback la watchlist.txt (toate pe 'conf') dacă nu e configurat/gol."""
    if WATCHLIST_URL and ALERTS_KEY:
        try:
            r = requests.get(WATCHLIST_URL, headers={"x-ci-key": ALERTS_KEY}, timeout=10)
            r.raise_for_status()
            data = r.json()
            if isinstance(data, list) and data:
                return [
                    {"sym": w["sym"].upper(), "strat": w.get("strat", "conf")}
                    for w in data if w.get("sym")
                ]
        except Exception as e:
            log.warning(f"Watchlist cloud indisponibil ({e}) — folosesc watchlist.txt")
    return [{"sym": s, "strat": "conf"} for s in load_watchlist()]


def scan_pass():
    """O singură trecere: alerte de preț + watchlist (conf/sweep per monedă) + SFP."""
    # Alertele de preț ale userului (rapid, un fetch de preț per simbol).
    check_price_alerts()
    wl = load_watchlist_cloud()
    log.info(f"Scanez {len(wl)} simboluri ({EXEC_TF}): {', '.join(w['sym'] for w in wl)}")

    # ── Faza 1: evaluăm TOT universul, fără să alertăm ──
    evals = []
    for w in wl:
        sym, strat = w["sym"], w.get("strat", "conf")
        if strat in ("conf", "both"):
            ev = evaluate_symbol(sym)
            if ev:
                evals.append(ev)
        if strat in ("sweep", "both"):
            scan_symbol_sweep(sym)  # Liquidity Sweep (independent de clasare)
        time.sleep(1)  # mic delay între simboluri, să nu lovim rate-limit Binance

    # ── Faza 2: clasare pe univers, apoi alertare de la cel mai bun în jos ──
    # Un scor de 82 nu înseamnă nimic singur: trebuie raportat la ce a scos tot
    # universul în aceeași bară (7b). Ordonăm pe score_adj, nu pe score brut (7c).
    if evals:
        evals.sort(key=lambda e: e["score_adj"], reverse=True)
        adj = sorted(e["score_adj"] for e in evals)
        n = len(adj)
        median = adj[n // 2] if n % 2 else (adj[n // 2 - 1] + adj[n // 2]) / 2
        for rank, ev in enumerate(evals, start=1):
            # percentila = % din univers pe care îl depășește scorul ăsta
            below = sum(1 for a in adj if a < ev["score_adj"])
            uni = {"uni_rank": rank, "uni_total": n,
                   "uni_pct": 100.0 * below / n, "uni_median": median}
            alert_symbol(ev, uni)
    # SFP dedicat (Playbook 1) — rulează doar în ferestrele Londra/NY open;
    # în afara lor, scan_sfp iese imediat, fără apeluri API.
    for sym in SFP_SYMBOLS:
        sfp.scan_sfp(sym, send_telegram)
        time.sleep(1)


def main():
    log.info("═" * 50)
    log.info(f"Confluence + SFP Scanner — pornit ({'RUN_ONCE' if RUN_ONCE else 'loop'})")
    log.info(f"Min CS Score: {MIN_SCORE} | SFP: {', '.join(SFP_SYMBOLS)}")
    log.info("═" * 50)

    if not TG_TOKEN or not TG_CHAT_ID:
        log.warning("⚠ TG_TOKEN sau TG_CHAT_ID nu sunt setate — alertele nu vor fi trimise!")

    # TEST_PING=1 → trimite un mesaj de test și ieși (verificare livrare Telegram).
    if os.environ.get("TEST_PING", "").lower() in ("1", "true"):
        ok = send_telegram(
            "✅ <b>Test crypto-scanner</b>\n"
            "Scannerul e conectat și rulează pe GitHub Actions.\n"
            "Vei primi: 📊 alerte CONTEXT (confluence) și 🎯 alerte SETUP (SFP)."
        )
        log.info(f"Mesaj de test trimis: {ok}")
        return

    if RUN_ONCE:
        # Cron / GitHub Actions: încarcă starea, o trecere, salvează, ieși.
        load_state()
        scan_pass()
        save_state()
        log.info("Trecere unică completă (RUN_ONCE).")
        return

    # Buclă continuă: server always-on, local SAU job lung pe GitHub Actions.
    # Cu MAX_RUNTIME_SEC setat, iese curat înainte de limita de 6h a jobului;
    # starea anti-spam e salvată după fiecare trecere (o preia cache-ul Actions).
    started = time.time()
    load_state()
    while True:
        scan_pass()
        save_state()
        if MAX_RUNTIME_SEC and time.time() - started + SCAN_INTERVAL_SEC > MAX_RUNTIME_SEC:
            log.info(f"Am atins MAX_RUNTIME_SEC ({MAX_RUNTIME_SEC}s) — ies curat.")
            return
        log.info(f"Scan complet. Următorul scan în {SCAN_INTERVAL_SEC}s.\n")
        time.sleep(SCAN_INTERVAL_SEC)


if __name__ == "__main__":
    main()
