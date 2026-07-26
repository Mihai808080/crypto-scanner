"""
Backtest — merită Consensus Radar-ul să intre în semnal?
════════════════════════════════════════════════════════
Două întrebări, în ordinea asta:

  1. BASELINE: edge-ul SFP (+0.29R/trade pe DOGE, măsurat pe date FUTURES)
     supraviețuiește mutării pe mirror-ul SPOT Binance? Am schimbat sursa ca
     să repar 451-ul; dacă edge-ul dispare odată cu ea, restul e irelevant.

  2. FILTRU: dacă păstrăm doar semnalele unde consensul de indicatori e
     ÎMPOTRIVA tradeului (fadeăm turma), se îmbunătățește expectancy-ul?

Simularea respectă playbook-ul din alertă:
  - intrare LIMIT la nivelul recuperat, valabilă 6 bare M5 (~30 min);
  - SL structural = extrema wick-ului ± BUFFER;
  - TP1 = 1R pe 50% din poziție, apoi SL la breakeven;
  - TP2 = pool-ul opus (min. 1.5R, altfel 2R);
  - time-stop: 3 bare fără +0.5R → ieșire la close.

Ipoteze conservatoare, declarate explicit:
  - dacă o bară atinge și SL și TP, presupunem SL-ul primul (nu știm ordinea
    intrabară din M5, iar varianta optimistă e cea care minte);
  - costuri: 0.02% maker la intrare + 0.05% taker la fiecare ieșire,
    convertite în R prin raportare la distanța de stop;
  - fără slippage pe limit (e maker), dar ieșirile sunt taker.

Rulează pe GitHub Actions (Actions → backtest). Local nu merge decât dacă ai
acces la data-api.binance.vision.
"""

import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import consensus  # noqa: E402
import datasrc  # noqa: E402
import sfp  # noqa: E402

DAY_MS = 86_400_000
SLICE = 900          # bare pasate detectorului — IDENTIC cu producția
ENTRY_VALID = 6      # bare M5 în care limitul poate fi prins (~30 min)
TIME_STOP = 3        # bare fără +0.5R → ieșim
FEE_MAKER = 0.0002
FEE_TAKER = 0.0005

MONTHS = int(os.environ.get("BT_MONTHS", "12"))
SYMBOLS = [s.strip().upper() for s in
           os.environ.get("BT_SYMBOLS", "DOGEUSDT,SOLUSDT").split(",") if s.strip()]


def log(msg):
    print(msg, flush=True)


# ─────────────────────────────────────────────
# SIMULAREA UNUI TRADE
# ─────────────────────────────────────────────
def simulate(bars, sig_i, sig, round_step):
    """Întoarce R-ul net al tradeului, sau None dacă limitul n-a fost prins."""
    d = sig["dir"]
    limit = sig["level"]["p"]
    stop = sig["wick"] * (1 - sfp.BUFFER) if d == 1 else sig["wick"] * (1 + sfp.BUFFER)
    risk = abs(limit - stop)
    if risk <= 0:
        return None
    if risk / limit < sfp.MIN_STOP_PCT:
        return None  # aceeași regulă ca în producție: stop prea mic vs. fees

    tp2 = sfp.opposite_pool(bars, sig_i, d, limit, round_step)
    min_tp2 = limit + 1.5 * risk if d == 1 else limit - 1.5 * risk
    if tp2 is None or (tp2 < min_tp2 if d == 1 else tp2 > min_tp2):
        tp2 = limit + 2 * risk if d == 1 else limit - 2 * risk
    tp1 = limit + risk if d == 1 else limit - risk

    # 1) prinderea limitului
    fill = None
    for j in range(sig_i + 1, min(sig_i + 1 + ENTRY_VALID, len(bars))):
        b = bars[j]
        if (b["l"] <= limit) if d == 1 else (b["h"] >= limit):
            fill = j
            break
    if fill is None:
        return None

    # 2) derularea poziției
    half_done = False
    sl = stop
    r_acc = 0.0
    bars_in = 0
    for j in range(fill, len(bars)):
        b = bars[j]
        bars_in += 1
        hit_sl = (b["l"] <= sl) if d == 1 else (b["h"] >= sl)
        hit_tp1 = (b["h"] >= tp1) if d == 1 else (b["l"] <= tp1)
        hit_tp2 = (b["h"] >= tp2) if d == 1 else (b["l"] <= tp2)

        # Conservator: SL-ul are prioritate în aceeași bară.
        if hit_sl:
            frac = 0.5 if half_done else 1.0
            r_sl = 0.0 if half_done else -1.0  # după TP1, SL-ul e la BE
            r_acc += frac * r_sl
            r_acc -= frac * (FEE_TAKER * limit / risk)
            return r_acc - (FEE_MAKER * limit / risk)

        if not half_done and hit_tp1:
            r_acc += 0.5 * 1.0
            r_acc -= 0.5 * (FEE_TAKER * limit / risk)
            half_done = True
            sl = limit  # breakeven
            bars_in = 0

        if half_done and hit_tp2:
            r_mult = abs(tp2 - limit) / risk
            r_acc += 0.5 * r_mult
            r_acc -= 0.5 * (FEE_TAKER * limit / risk)
            return r_acc - (FEE_MAKER * limit / risk)

        # time-stop: doar înainte de TP1
        if not half_done and bars_in >= TIME_STOP:
            move = (b["c"] - limit) / risk if d == 1 else (limit - b["c"]) / risk
            if move < 0.5:
                r_acc += move
                r_acc -= FEE_TAKER * limit / risk
                return r_acc - (FEE_MAKER * limit / risk)

    return None  # semnal prea aproape de capătul datelor


# ─────────────────────────────────────────────
# FUNDING (MEXC, ~540 zile)
# ─────────────────────────────────────────────
def load_funding_all(symbol):
    """Toate paginile de funding, cronologic. [] dacă sursa tace."""
    out = []
    try:
        for page in range(1, 30):
            d = datasrc._mexc_data("funding_rate/history",
                                   {"symbol": datasrc._mexc_sym(symbol),
                                    "page_num": page, "page_size": 100})
            rows = d.get("resultList") or []
            out.extend({"t": int(r["settleTime"]), "r": float(r["fundingRate"])}
                       for r in rows)
            if page >= d.get("totalPage", 1):
                break
            time.sleep(0.2)
    except Exception as e:
        log(f"  ! funding indisponibil pentru {symbol}: {e}")
        return []
    out.sort(key=lambda x: x["t"])
    return out


def funding_at(funding, ts):
    """Starea funding-ului AȘA CUM ERA la momentul ts (fără lookahead)."""
    past = [f for f in funding if f["t"] <= ts]
    if len(past) < 20:
        return None
    return past


# ─────────────────────────────────────────────
# STATISTICI
# ─────────────────────────────────────────────
def stats(rs):
    n = len(rs)
    if n == 0:
        return None
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    gross_w = sum(wins)
    gross_l = abs(sum(losses))
    exp = sum(rs) / n
    # Eroare standard — fără ea, o diferență de expectancy nu înseamnă nimic.
    mean = exp
    var = sum((r - mean) ** 2 for r in rs) / (n - 1) if n > 1 else 0.0
    se = (var / n) ** 0.5 if n > 1 else 0.0
    return {
        "n": n, "exp": exp, "se": se,
        "wr": 100.0 * len(wins) / n,
        "pf": (gross_w / gross_l) if gross_l > 0 else float("inf"),
        "total": sum(rs),
    }


def show(title, rs):
    s = stats(rs)
    if not s:
        log(f"  {title:38s} — niciun trade")
        return
    pf = "∞" if s["pf"] == float("inf") else f"{s['pf']:.2f}"
    log(f"  {title:38s} n={s['n']:4d}  exp={s['exp']:+.3f}R ±{s['se']:.3f}"
        f"  WR={s['wr']:4.1f}%  PF={pf:>5s}  total={s['total']:+7.1f}R")
    return s


# ─────────────────────────────────────────────
# RULARE
# ─────────────────────────────────────────────
def run_symbol(symbol):
    log("=" * 78)
    log(f"{symbol} — ultimele {MONTHS} luni, M5, mirror spot Binance")
    log("=" * 78)

    end = int(time.time() * 1000)
    start = end - MONTHS * 30 * DAY_MS
    t0 = time.time()
    bars = datasrc.get_klines_range(symbol, "5m", start, end)
    log(f"  {len(bars)} bare descărcate în {time.time() - t0:.0f}s "
        f"({datetime.fromtimestamp(bars[0]['t']/1000, tz=timezone.utc):%Y-%m-%d} → "
        f"{datetime.fromtimestamp(bars[-1]['t']/1000, tz=timezone.utc):%Y-%m-%d})")

    funding = load_funding_all(symbol)
    log(f"  {len(funding)} înregistrări de funding")

    step = sfp.ROUND_STEP.get(symbol, 1.0)
    trades = []
    scanned = 0
    for i in range(SLICE, len(bars) - 60):
        b = bars[i]
        # Aceleași ferestre ca producția — restul zilei n-a avut edge.
        if not sfp.in_window(b["t"]):
            continue
        scanned += 1
        sl = bars[i - SLICE + 1:i + 1]
        li = len(sl) - 1
        sig = sfp.detect_sfp(sl, li, step)
        if not sig:
            continue
        r = simulate(bars, i, sig, step)
        if r is None:
            continue
        # Consensul se calculează pe barele DE PÂNĂ LA semnal inclusiv.
        cons = consensus.consensus(sl[-260:])
        fund = funding_at(funding, b["t"])
        grade = sfp.funding_grade(fund, sig["dir"])[0] if fund else "?"
        trades.append({
            "t": b["t"], "dir": sig["dir"], "r": r, "cons": cons,
            "kind": sig["level"]["kind"], "grade": grade,
        })

    log(f"  {scanned} bare în fereastră · {len(trades)} tradeuri simulate\n")
    if not trades:
        log("  Fără tradeuri — nimic de concluzionat.\n")
        return trades

    log("  ── BASELINE (toate semnalele SFP) ──")
    show("toate", [t["r"] for t in trades])
    show("LONG", [t["r"] for t in trades if t["dir"] == 1])
    show("SHORT", [t["r"] for t in trades if t["dir"] == -1])

    log("\n  ── FILTRU CONSENSUS (ideea luată de la trader) ──")
    with_cons = [t for t in trades if t["cons"]]
    log(f"  ({len(with_cons)}/{len(trades)} tradeuri au consens calculabil)")
    # Fade: turma e poziționată INVERS față de tradeul nostru.
    fade = [t for t in with_cons if t["cons"]["herd_dir"] == -t["dir"]]
    same = [t for t in with_cons if t["cons"]["herd_dir"] == t["dir"]]
    neutral = [t for t in with_cons if t["cons"]["herd_dir"] == 0]
    show("turma CONTRA noastră (fade)", [t["r"] for t in fade])
    show("turma CU noi (herd agreement)", [t["r"] for t in same])
    show("consens neutru", [t["r"] for t in neutral])

    log("\n  ── prag de aglomerare, variante ──")
    for thr in (60, 65, 70, 75, 80, 85):
        sel = [t for t in with_cons
               if (t["cons"]["pct"] <= 100 - thr and t["dir"] == 1)
               or (t["cons"]["pct"] >= thr and t["dir"] == -1)]
        show(f"fade la prag {thr}%", [t["r"] for t in sel])

    log("\n  ── FUNDING (filtrul care există deja: grad A vs B) ──")
    show("grad A", [t["r"] for t in trades if t["grade"] == "A"])
    show("grad B", [t["r"] for t in trades if t["grade"] == "B"])

    log("\n  ── combinat: fade + grad A ──")
    show("fade ȘI grad A", [t["r"] for t in fade if t["grade"] == "A"])
    log("")
    return trades


def main():
    log(f"Backtest consensus · simboluri={SYMBOLS} · luni={MONTHS}")
    log(f"Ipoteze: SL prioritar intrabară · maker {FEE_MAKER*100:.3f}% "
        f"+ taker {FEE_TAKER*100:.3f}% · limit valabil {ENTRY_VALID} bare\n")
    allt = []
    for s in SYMBOLS:
        try:
            allt += run_symbol(s)
        except Exception as e:
            log(f"EROARE pe {s}: {type(e).__name__}: {e}")
    if len(SYMBOLS) > 1 and allt:
        log("=" * 78)
        log("AGREGAT pe toate simbolurile")
        log("=" * 78)
        show("toate", [t["r"] for t in allt])
        wc = [t for t in allt if t["cons"]]
        show("fade (turma contra)", [t["r"] for t in wc
                                     if t["cons"]["herd_dir"] == -t["dir"]])
        show("herd agreement", [t["r"] for t in wc
                                if t["cons"]["herd_dir"] == t["dir"]])
    return 0


if __name__ == "__main__":
    sys.exit(main())
