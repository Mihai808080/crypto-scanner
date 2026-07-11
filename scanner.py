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

import sfp  # Playbook 1: Sweep & Reclaim — alerte SETUP (execuție discreționară)

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s")
log = logging.getLogger("confluence")

# ─────────────────────────────────────────────
# CONFIG — citit din variabile de mediu (Railway Settings → Variables)
# ─────────────────────────────────────────────
TG_TOKEN = os.environ.get("TG_TOKEN", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")
MIN_SCORE = int(os.environ.get("MIN_SCORE", "65"))
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

BINANCE_BASE = "https://fapi.binance.com/fapi/v1"

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
def get_klines(symbol, interval, limit):
    url = f"{BINANCE_BASE}/klines"
    r = requests.get(url, params={"symbol": symbol, "interval": interval, "limit": limit}, timeout=10)
    r.raise_for_status()
    raw = r.json()
    # k[9] = taker buy base volume — permite delta de agresiune REALĂ, nu estimată
    return [
        {
            "t": int(k[0]), "o": float(k[1]), "h": float(k[2]), "l": float(k[3]),
            "c": float(k[4]), "v": float(k[5]), "tb": float(k[9]),
        }
        for k in raw
    ]


def get_ticker(symbol):
    url = f"{BINANCE_BASE}/ticker/24hr"
    r = requests.get(url, params={"symbol": symbol}, timeout=10)
    r.raise_for_status()
    d = r.json()
    return float(d["lastPrice"]), float(d["priceChangePercent"])


def get_price(symbol):
    """Doar ultimul preț — pentru alertele de preț ale userului."""
    url = f"{BINANCE_BASE}/ticker/price"
    r = requests.get(url, params={"symbol": symbol}, timeout=10)
    r.raise_for_status()
    return float(r.json()["price"])


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


def compute_markov(closes, win=12):
    if len(closes) < win * 5:
        return None
    BU, BE = 0.008, -0.008
    labels = []
    for i, c in enumerate(closes):
        if i < win:
            labels.append(0)
            continue
        r = (c - closes[i - win]) / closes[i - win]
        labels.append(1 if r >= BU else 2 if r <= BE else 0)
    mat = [[0, 0, 0] for _ in range(3)]
    n_trans = 0
    for i in range(0, len(labels) - win, win):
        mat[labels[i]][labels[i + win]] += 1
        n_trans += 1
    cur = labels[-1]
    row_n = sum(mat[cur])
    # Cu prea puține observații matricea e zgomot pur — nu emitem semnal.
    # Pragurile: minim 30 tranziții totale și minim 8 din starea curentă.
    if n_trans < 30 or row_n < 8:
        return None
    prob = []
    for row in mat:
        s = sum(row)
        prob.append([v / s for v in row] if s else [1 / 3] * 3)
    sig = prob[cur][1] - prob[cur][2]
    return {"prob": prob, "cur": cur, "sig": sig, "stk": prob[cur][cur], "n": n_trans}


def is_good_session_at(ts_ms):
    # Londra + New York (8-22 UTC) — scris ca un singur interval
    h = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).hour
    return 8 <= h < 22


# ─────────────────────────────────────────────
# CONFLUENCE SCORE — port 1:1 din dashboard
# ─────────────────────────────────────────────
def compute_confluence_score(price, cls15, kl15, kl1h, kl4h, bar_ts=None):
    factors = []
    total = 0

    # 1) Markov M15 — max 8 (redus de la 20: cu ~16-30 tranziții observabile,
    # semnalul e prea zgomotos statistic ca să fie factorul dominant)
    mkv = compute_markov(cls15, 12)
    markov_pts, markov_dir = 0, 0
    if mkv:
        a = abs(mkv["sig"])
        if a >= 0.30:
            markov_pts, markov_dir = 8, (1 if mkv["sig"] > 0 else -1)
        elif a >= 0.20:
            markov_pts, markov_dir = 5, (1 if mkv["sig"] > 0 else -1)
        elif a >= 0.10:
            markov_pts, markov_dir = 3, (1 if mkv["sig"] > 0 else -1)
    total += markov_pts
    factors.append(("Markov M15", markov_pts, 8, markov_dir))

    # 2) MTF EMA Trend — max 20
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
            if bull1h:
                ema_pts += 6
            if bear1h:
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
    achievable = sum(mx for _, _, mx, _ in factors)  # 8+20+10+10+10 = 58
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


def build_alert_message(symbol, cs, price):
    is_long = cs["dir"] == 1
    atr_pct = cs.get("atr_pct") or 0.0035
    # Podea de 0.4%: sub asta, taxele round-trip (~0.1%) + slippage devin
    # o fracțiune prea mare din distanța de stop și edge-ul dispare.
    sl_pct = max(0.004, atr_pct * 1.2)
    tp1_pct, tp2_pct = sl_pct * 1.5, sl_pct * 3
    sl = price * (1 - sl_pct) if is_long else price * (1 + sl_pct)
    tp1 = price * (1 + tp1_pct) if is_long else price * (1 - tp1_pct)
    tp2 = price * (1 + tp2_pct) if is_long else price * (1 - tp2_pct)
    # Leverage temperat: 20× cu SL de câteva zecimi de procent înseamnă că
    # taxele+slippage-ul mănâncă o parte mare din edge; 5-10× e sustenabil.
    lev = 10 if cs["score"] >= 80 else 7 if cs["score"] >= 65 else 5
    factor_lines = "\n".join(
        f"{'✅' if pts >= mx * 0.6 else '⚠️'} {name}: {pts}/{mx}"
        for name, pts, mx, d in cs["factors"] if mx > 0
    )
    adx_line = f"📈 ADX: {cs.get('adx', 0):.0f} (×{cs.get('adx_mult', 1):.2f})\n"
    sym_disp = symbol.replace("USDT", "/USDT")
    return (
        f"📊 <b>CONTEXT — CONFLUENCE ENGINE</b>\n"
        f"⚡ <b>{sym_disp}</b> · 15m\n"
        f"ℹ️ <i>Informativ (backtest: fără edge de intrare mecanică) — nu e semnal de execuție.</i>\n\n"
        f"{'▲' if is_long else '▼'} <b>{'LONG' if is_long else 'SHORT'} SIGNAL</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"💰 Entry:    <b>${price:,.4f}</b>\n"
        f"🛑 SL (ATR): ${sl:,.4f}  ({sl_pct*100:.2f}%)\n"
        f"🎯 TP1:      ${tp1:,.4f}\n"
        f"🚀 TP2:      ${tp2:,.4f}\n"
        f"⚡ Leverage: <b>{lev}×</b>\n"
        f"📊 CS Score: <b>{cs['score']}/100</b>\n"
        f"{adx_line}"
        f"🕐 Ora:      {datetime.now().strftime('%H:%M:%S')}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"<b>Factori:</b>\n{factor_lines}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"⚠️ Nu e sfat financiar. Verifică manual."
    )


# ─────────────────────────────────────────────
# SCAN LOOP
# ─────────────────────────────────────────────
def scan_symbol(symbol):
    try:
        kl15 = get_klines(symbol, "15m", 200)
        kl1h = get_klines(symbol, "1h", 60)
        kl4h = get_klines(symbol, "4h", 30)
        price, chg = get_ticker(symbol)
        cls15 = [k["c"] for k in kl15]
        cs = compute_confluence_score(price, cls15, kl15, kl1h, kl4h, bar_ts=int(time.time() * 1000))

        log.info(f"{symbol:12s} price=${price:,.4f}  CS={cs['score']}/100  dir={cs['dir']}")

        st = state.setdefault(symbol, {"prev_dir": 0, "last_alert_ts": 0})
        now = time.time()
        if (
            cs["score"] >= MIN_SCORE
            and cs["dir"] != 0
            and cs["dir"] != st["prev_dir"]
            and now - st["last_alert_ts"] > 5 * 60
        ):
            st["prev_dir"] = cs["dir"]
            st["last_alert_ts"] = now
            msg = build_alert_message(symbol, cs, price)
            if send_telegram(msg):
                log.info(f"  → Alertă trimisă pentru {symbol}")
        elif cs["dir"] == 0:
            st["prev_dir"] = 0
    except Exception as e:
        log.error(f"Eroare la scanarea {symbol}: {e}")


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
        if dom <= 12 and pm != 0 and pm == -direction:
            total += 15

    return {"score": min(100, total), "dir": direction}


def build_sweep_message(symbol, sig, price):
    is_long = sig["dir"] == 1
    disp = symbol.replace("USDT", "/USDT")
    return (
        f"🎯 <b>LIQUIDITY SWEEP</b>\n"
        f"⚡ <b>{disp}</b> · 15m\n\n"
        f"{'▲' if is_long else '▼'} <b>{'LONG' if is_long else 'SHORT'}</b> · sweep + reclaim confirmat\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"💰 Preț: <b>${price:,.4f}</b>\n"
        f"📊 Scor sweep: <b>{sig['score']}/100</b>\n"
        f"🕐 {datetime.now().strftime('%H:%M:%S')}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"⚠️ Nu e sfat financiar. Verifică manual."
    )


def scan_symbol_sweep(symbol):
    """Rulează Liquidity Sweep pe un simbol și alertează la sweep confirmat nou."""
    try:
        kl15 = get_klines(symbol, "15m", 200)
        kl4h = get_klines(symbol, "4h", 30)
        sig = sweep_signal(kl15, kl4h, int(time.time() * 1000))
        st = sweep_state.setdefault(symbol, {"prev_dir": 0, "last_alert_ts": 0})
        if not sig or sig["dir"] == 0:
            st["prev_dir"] = 0
            return
        price = kl15[-1]["c"]
        log.info(f"{symbol:12s} SWEEP score={sig['score']}/100  dir={sig['dir']}")
        now = time.time()
        if sig["dir"] != st["prev_dir"] and now - st.get("last_alert_ts", 0) > 5 * 60:
            st["prev_dir"] = sig["dir"]
            st["last_alert_ts"] = now
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
                f"<b>{disp}</b> {verb} <b>{target:,.4f}</b>\n"
                f"💰 Preț curent: <b>{p:,.4f}</b>\n"
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
    log.info(f"Scanez {len(wl)} simboluri: {', '.join(w['sym'] for w in wl)}")
    for w in wl:
        sym, strat = w["sym"], w.get("strat", "conf")
        if strat in ("conf", "both"):
            scan_symbol(sym)  # Confluence Score
        if strat in ("sweep", "both"):
            scan_symbol_sweep(sym)  # Liquidity Sweep
        time.sleep(1)  # mic delay între simboluri, să nu lovim rate-limit Binance
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

    # Server always-on / local: buclă continuă (starea trăiește în memorie).
    while True:
        scan_pass()
        log.info(f"Scan complet. Următorul scan în {SCAN_INTERVAL_SEC}s.\n")
        time.sleep(SCAN_INTERVAL_SEC)


if __name__ == "__main__":
    main()
