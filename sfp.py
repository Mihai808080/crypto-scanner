"""
SFP Detector — Playbook 1: Sweep & Reclaim
═══════════════════════════════════════════
Port 1:1 al detectorului validat prin backtest (iul. 2026, 12 luni M5):
edge măsurat DOAR pe meme pairs (DOGE +0.29R/trade, PF 1.81) și DOAR în
primele 90 min ale sesiunilor Londra/NY. BTC: fără edge — nu-l adăuga.

Logica: nivel de lichiditate (PDH/PDL, high/low-ul zilei, equal highs/lows,
numere rotunde, range-ul Asiei) e înțepat cu wick, dar bara M5 ÎNCHIDE
înapoi — capcană pentru cei prinși — cu divergență CVD la extremă.
Alerta propune limit la nivelul recuperat (maker), SL structural, TP1 la 1R,
TP2 la pool-ul opus. Execuția e discreționară — scannerul doar semnalează.

Rezultatele complete ale backtestului: vezi memoria sesiunii Claude
(sfp-playbook-research) sau scratchpad pb1/.
"""

import os
import time
import logging
from datetime import datetime, timezone

import requests

log = logging.getLogger("sfp")

BINANCE_BASE = "https://fapi.binance.com/fapi/v1"

# ── Parametri (identici cu backtestul validat — NU-i modifica fără re-test) ──
BUFFER = 0.0005          # stop = extrema wick-ului ± 0.05%
TOL = 0.0008             # toleranță nivel / clustere equal highs-lows
PIVOT_K = 2
EQ_LOOKBACK = 150        # bare M5 pentru equal highs/lows
CVD_WIN = 12             # fereastră divergență CVD
MIN_STOP_PCT = 0.0015    # sub 0.15% stopul e mâncat de fees → skip
FUNDING_PCTILE = 0.80    # top 20% |funding| = "extrem" → grad A

# Ferestrele validate: primele 90 min Londra + NY (UTC).
# Backtest: extinderea la 07-11 diluează edge-ul de la +0.29R la +0.05R.
WINDOWS = [(7 * 60, 8 * 60 + 30), (13 * 60, 14 * 60 + 30)]

# Pas numere rotunde per simbol (aliniat cu backtestul)
ROUND_STEP = {"BTCUSDT": 1000.0, "ETHUSDT": 100.0, "SOLUSDT": 5.0, "DOGEUSDT": 0.01}

# Anti-spam: (symbol) -> timestamp-ul barei M5 pentru care s-a alertat deja
_alerted = {}


# ─────────────────────────────────────────────
# DATE
# ─────────────────────────────────────────────
def get_klines_5m(symbol, limit=900):
    """~3 zile de M5 — suficient pt. PDH/PDL (ziua UTC anterioară completă)."""
    r = requests.get(
        f"{BINANCE_BASE}/klines",
        params={"symbol": symbol, "interval": "5m", "limit": limit},
        timeout=10,
    )
    r.raise_for_status()
    return [
        {"t": int(k[0]), "o": float(k[1]), "h": float(k[2]), "l": float(k[3]),
         "c": float(k[4]), "v": float(k[5]), "tb": float(k[9])}
        for k in r.json()
    ]


def get_funding(symbol, limit=100):
    r = requests.get(
        f"{BINANCE_BASE}/fundingRate",
        params={"symbol": symbol, "limit": limit},
        timeout=10,
    )
    r.raise_for_status()
    return [{"t": int(f["fundingTime"]), "r": float(f["fundingRate"])} for f in r.json()]


# ─────────────────────────────────────────────
# UTILITARE (paritate cu playbook1.mjs)
# ─────────────────────────────────────────────
def _utc(ts_ms):
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)


def day_key(ts_ms):
    return _utc(ts_ms).strftime("%Y-%m-%d")


def in_window(ts_ms):
    d = _utc(ts_ms)
    mins = d.hour * 60 + d.minute
    return any(a <= mins < b for a, b in WINDOWS)


def cvd_series(bars):
    cum, out = 0.0, []
    for k in bars:
        if k["v"] > 0:
            delta = 2 * k["tb"] - k["v"]
        else:
            rng = (k["h"] - k["l"]) or 1
            delta = ((k["c"] - k["l"]) / rng - (k["h"] - k["c"]) / rng) * k["v"]
        cum += delta
        out.append(cum)
    return out


def _is_pivot_high(bars, i, k):
    return all(bars[j]["h"] < bars[i]["h"] for j in range(i - k, i + k + 1) if j != i)


def _is_pivot_low(bars, i, k):
    return all(bars[j]["l"] > bars[i]["l"] for j in range(i - k, i + k + 1) if j != i)


def build_levels(bars, i, round_step):
    """Niveluri candidate la bara i — doar din trecut (fără lookahead)."""
    b = bars[i]
    out = []
    # PDH/PDL — ziua UTC anterioară
    prev_day = day_key(b["t"] - 86_400_000)
    ph, pl = -float("inf"), float("inf")
    for k in bars[:i]:
        if day_key(k["t"]) == prev_day:
            ph = max(ph, k["h"]); pl = min(pl, k["l"])
    if ph > -float("inf"):
        out.append({"p": ph, "kind": "PDH", "side": "high"})
        out.append({"p": pl, "kind": "PDL", "side": "low"})
    # extremele zilei curente de până acum
    today = day_key(b["t"])
    dh, dl = -float("inf"), float("inf")
    j = i - 1
    while j >= 0 and day_key(bars[j]["t"]) == today:
        dh = max(dh, bars[j]["h"]); dl = min(dl, bars[j]["l"])
        j -= 1
    if dh > -float("inf"):
        out.append({"p": dh, "kind": "DAYH", "side": "high"})
        out.append({"p": dl, "kind": "DAYL", "side": "low"})
    # numere rotunde imediat sub/peste
    below = (b["c"] // round_step) * round_step
    out.append({"p": below, "kind": "RN", "side": "low"})
    out.append({"p": below + round_step, "kind": "RN", "side": "high"})
    # range-ul Asiei (00-07 UTC azi), după 07:00
    if _utc(b["t"]).hour >= 7:
        ah, al = -float("inf"), float("inf")
        j = i - 1
        while j >= 0 and day_key(bars[j]["t"]) == today:
            if _utc(bars[j]["t"]).hour < 7:
                ah = max(ah, bars[j]["h"]); al = min(al, bars[j]["l"])
            j -= 1
        if ah > -float("inf"):
            out.append({"p": ah, "kind": "ASIAH", "side": "high"})
            out.append({"p": al, "kind": "ASIAL", "side": "low"})
    # equal highs/lows — clustere de pivoturi
    lo = max(PIVOT_K, i - EQ_LOOKBACK)
    phs, pls = [], []
    for j in range(lo, i - PIVOT_K + 1):
        if _is_pivot_high(bars, j, PIVOT_K):
            phs.append(bars[j]["h"])
        if _is_pivot_low(bars, j, PIVOT_K):
            pls.append(bars[j]["l"])

    def clusters(arr, side):
        arr.sort()
        a = 0
        while a < len(arr):
            grp = [arr[a]]
            b2 = a + 1
            while b2 < len(arr) and (arr[b2] - arr[a]) / arr[a] < TOL:
                grp.append(arr[b2]); b2 += 1
            if len(grp) >= 2:
                out.append({"p": sum(grp) / len(grp), "kind": "EQ", "side": side})
                a += len(grp) - 1
            a += 1

    clusters(phs, "high")
    clusters(pls, "low")
    return out


def detect_sfp(bars, i, round_step):
    """SFP pe bara ÎNCHISĂ i: wick dincolo de nivel + close înapoi + div. CVD."""
    b = bars[i]
    cvd = cvd_series(bars[max(0, i - CVD_WIN):i + 1])
    lo = max(0, i - CVD_WIN)
    win = bars[lo:i + 1]
    for L in build_levels(bars, i, round_step):
        # sweep de LOW (→ long)
        if L["side"] == "low" and b["l"] < L["p"] and b["c"] > L["p"] and b["o"] > L["p"] * (1 - TOL):
            min_low = min(k["l"] for k in win)
            cvd_min_idx = min(range(len(cvd)), key=lambda j: cvd[j])
            if b["l"] <= min_low + 1e-12 and cvd_min_idx != len(cvd) - 1:
                return {"dir": 1, "level": L, "wick": b["l"]}
        # sweep de HIGH (→ short)
        if L["side"] == "high" and b["h"] > L["p"] and b["c"] < L["p"] and b["o"] < L["p"] * (1 + TOL):
            max_high = max(k["h"] for k in win)
            cvd_max_idx = max(range(len(cvd)), key=lambda j: cvd[j])
            if b["h"] >= max_high - 1e-12 and cvd_max_idx != len(cvd) - 1:
                return {"dir": -1, "level": L, "wick": b["h"]}
    return None


def opposite_pool(bars, i, direction, entry, round_step):
    best = None
    for L in build_levels(bars, i, round_step):
        if direction == 1 and L["side"] == "high" and L["p"] > entry:
            best = L["p"] if best is None else min(best, L["p"])
        if direction == -1 and L["side"] == "low" and L["p"] < entry:
            best = L["p"] if best is None else max(best, L["p"])
    return best


def funding_grade(funding, direction):
    """Grad A: |funding| în top 20% din ultimele ~30 zile, pe partea aglomerată."""
    if not funding:
        return "B", 0.0
    cur = funding[-1]
    absr = sorted(abs(f["r"]) for f in funding[-91:])
    thr = absr[int(len(absr) * FUNDING_PCTILE)] if absr else float("inf")
    extreme = abs(cur["r"]) >= thr
    crowded_side = (direction == 1 and cur["r"] < 0) or (direction == -1 and cur["r"] > 0)
    return ("A" if (extreme and crowded_side) else "B"), cur["r"]


# ─────────────────────────────────────────────
# MESAJ + SCAN
# ─────────────────────────────────────────────
def build_sfp_message(symbol, sig, bars, i, funding):
    d = sig["dir"]
    level = sig["level"]
    limit = level["p"]
    stop = sig["wick"] * (1 - BUFFER) if d == 1 else sig["wick"] * (1 + BUFFER)
    risk = abs(limit - stop)
    tp1 = limit + risk if d == 1 else limit - risk
    tp2 = opposite_pool(bars, i, d, limit, ROUND_STEP.get(symbol, 1.0))
    min_tp2 = limit + 1.5 * risk if d == 1 else limit - 1.5 * risk
    if tp2 is None or (tp2 < min_tp2 if d == 1 else tp2 > min_tp2):
        tp2 = limit + 2 * risk if d == 1 else limit - 2 * risk
    grade, frate = funding_grade(funding, d)
    kind_names = {
        "PDH": "PDH (high-ul zilei precedente)", "PDL": "PDL (low-ul zilei precedente)",
        "DAYH": "high-ul zilei", "DAYL": "low-ul zilei",
        "EQ": "equal highs/lows", "RN": "număr rotund",
        "ASIAH": "high-ul Asiei", "ASIAL": "low-ul Asiei",
    }
    sym_disp = symbol.replace("USDT", "/USDT")
    risk_note = "risc întreg (poveste funding)" if grade == "A" else "jumătate de risc"
    return (
        f"🎯 <b>SETUP — SFP SWEEP &amp; RECLAIM</b>\n"
        f"⚡ <b>{sym_disp}</b> · M5 · {'▲ LONG' if d == 1 else '▼ SHORT'}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📍 Nivel măturat: <b>{kind_names.get(level['kind'], level['kind'])}</b> @ ${limit:,.4f}\n"
        f"💰 Limit sugerat: <b>${limit:,.4f}</b> (maker; valabil ~30 min)\n"
        f"🛑 SL structural: ${stop:,.4f} ({risk / limit * 100:.2f}%)\n"
        f"🎯 TP1 (1R, 50%): ${tp1:,.4f} → apoi SL la BE\n"
        f"🚀 TP2 (pool):    ${tp2:,.4f}\n"
        f"🏷️ Grad: <b>{grade}</b> — {risk_note} (funding {frate * 100:+.4f}%)\n"
        f"⏱️ Time-stop: 3×M5 fără +0.5R → ieși\n"
        f"🕐 {datetime.now().strftime('%H:%M:%S')}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"👁️ CVD div. ✓ · Confirmă regimul pe chart înainte de execuție.\n"
        f"⚠️ Nu e sfat financiar."
    )


def scan_sfp(symbol, send_fn):
    """Scanează ultima bară M5 ÎNCHISĂ. Returnează True dacă a alertat."""
    now_ms = int(time.time() * 1000)
    if not in_window(now_ms):
        return False  # în afara ferestrelor validate — zero apeluri API
    try:
        bars = get_klines_5m(symbol)
        if len(bars) < EQ_LOOKBACK + 10:
            return False
        i = len(bars) - 2  # ultima bară ÎNCHISĂ (ultima e cea în desfășurare)
        bar = bars[i]
        if not in_window(bar["t"]):
            return False
        if _alerted.get(symbol) == bar["t"]:
            return False  # deja alertat pe bara asta
        sig = detect_sfp(bars, i, ROUND_STEP.get(symbol, 1.0))
        if not sig:
            return False
        limit = sig["level"]["p"]
        stop = sig["wick"] * (1 - BUFFER) if sig["dir"] == 1 else sig["wick"] * (1 + BUFFER)
        if abs(limit - stop) / limit < MIN_STOP_PCT:
            log.info(f"{symbol}: SFP detectat dar stop < {MIN_STOP_PCT*100:.2f}% — skip (fees)")
            return False
        funding = get_funding(symbol)
        msg = build_sfp_message(symbol, sig, bars, i, funding)
        _alerted[symbol] = bar["t"]
        if send_fn(msg):
            log.info(f"  → 🎯 Alertă SFP trimisă: {symbol} {'LONG' if sig['dir'] == 1 else 'SHORT'} @ {limit}")
            return True
    except Exception as e:
        log.error(f"Eroare SFP {symbol}: {e}")
    return False
