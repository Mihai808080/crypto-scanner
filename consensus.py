"""
Consensus Radar — cât de aglomerat e retailul de aceeași parte
═══════════════════════════════════════════════════════════════
De unde vine: screenshot-ul „Apex Alpha MM 5.3.0" arăta
`Consensus Radar: RETAIL BEAR TRAP (-17/26)`. Pine n-are acces la
poziționarea reală a nimănui, deci „-17/26" nu poate fi altceva decât un
numărător de voturi: 26 de indicatori clasici, 17 bearish. Adică
„consensul retail" = ce ar vedea un om care se uită la indicatorii standard.

Reproducem exact ideea, cu numele ei adevărat, și o citim CONTRARIAN:
consens extrem = mulți sunt deja poziționați acolo = combustibil pentru
mișcarea inversă. Nu e magie, e o măsură de aglomerare.

Modulul e pur: primește lumânări, întoarce numere. Nu face rețea, nu trimite
nimic, nu importă scanner-ul (ar fi import circular) — de aceea își are
propriile funcții de indicatori, chiar dacă seamănă cu cele din scanner.py.

Interpretarea în alerte se face în altă parte; aici doar se măsoară.
"""


# ─────────────────────────────────────────────
# INDICATORI (local, ca modulul să rămână independent)
# ─────────────────────────────────────────────
def _ema(vals, span):
    k = 2 / (span + 1)
    e = vals[0]
    out = []
    for v in vals:
        e = v * k + e * (1 - k)
        out.append(e)
    return out


def _sma(vals, n):
    if len(vals) < n:
        return None
    return sum(vals[-n:]) / n


def _rsi(closes, period=14):
    if len(closes) < period + 2:
        return None
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gain = sum(d for d in deltas[:period] if d > 0) / period
    loss = sum(-d for d in deltas[:period] if d < 0) / period
    for d in deltas[period:]:
        gain = (gain * (period - 1) + max(d, 0)) / period
        loss = (loss * (period - 1) + max(-d, 0)) / period
    if loss == 0:
        return 100.0
    return 100 - 100 / (1 + gain / loss)


def _macd(closes, fast=12, slow=26, sig=9):
    if len(closes) < slow + sig:
        return None, None
    ef, es = _ema(closes, fast), _ema(closes, slow)
    line = [a - b for a, b in zip(ef, es)]
    signal = _ema(line, sig)
    return line[-1], signal[-1]


def _stoch(kl, period=14, smooth=3):
    if len(kl) < period + smooth:
        return None, None
    ks = []
    for i in range(len(kl) - smooth, len(kl)):
        win = kl[i - period + 1:i + 1]
        hh = max(b["h"] for b in win)
        ll = min(b["l"] for b in win)
        ks.append(50.0 if hh == ll else 100 * (kl[i]["c"] - ll) / (hh - ll))
    return ks[-1], sum(ks) / len(ks)


def _bollinger(closes, n=20, mult=2.0):
    if len(closes) < n:
        return None, None
    win = closes[-n:]
    mid = sum(win) / n
    var = sum((c - mid) ** 2 for c in win) / n
    sd = var ** 0.5
    return mid + mult * sd, mid - mult * sd


def _cci(kl, n=20):
    if len(kl) < n:
        return None
    tp = [(b["h"] + b["l"] + b["c"]) / 3 for b in kl[-n:]]
    ma = sum(tp) / n
    md = sum(abs(x - ma) for x in tp) / n
    return 0.0 if md == 0 else (tp[-1] - ma) / (0.015 * md)


def _williams_r(kl, n=14):
    if len(kl) < n:
        return None
    win = kl[-n:]
    hh = max(b["h"] for b in win)
    ll = min(b["l"] for b in win)
    return -50.0 if hh == ll else -100 * (hh - kl[-1]["c"]) / (hh - ll)


def _di(kl, period=14):
    """DI+ / DI- — direcția din ADX, ce vede lumea ca 'trend up/down'."""
    if len(kl) < period * 2:
        return None, None
    pdm, ndm, tr = [], [], []
    for i in range(1, len(kl)):
        up = kl[i]["h"] - kl[i - 1]["h"]
        dn = kl[i - 1]["l"] - kl[i]["l"]
        pdm.append(up if (up > dn and up > 0) else 0)
        ndm.append(dn if (dn > up and dn > 0) else 0)
        pc = kl[i - 1]["c"]
        tr.append(max(kl[i]["h"] - kl[i]["l"], abs(kl[i]["h"] - pc), abs(kl[i]["l"] - pc)))
    str_ = sum(tr[-period:])
    if str_ == 0:
        return None, None
    return 100 * sum(pdm[-period:]) / str_, 100 * sum(ndm[-period:]) / str_


# ─────────────────────────────────────────────
# VOTUL
# ─────────────────────────────────────────────
def votes(kl):
    """Lista de (nume, vot) cu vot în {+1 bull, -1 bear, 0 neutru}.

    Sunt indicatorii pe care îi are oricine pe chart, în setările implicite —
    ăsta e tot rostul: măsurăm ce vede majoritatea, nu ce e 'corect'.
    """
    out = []
    closes = [b["c"] for b in kl]
    price = closes[-1]

    def vote(name, cond_bull, cond_bear):
        out.append((name, 1 if cond_bull else -1 if cond_bear else 0))

    r = _rsi(closes, 14)
    if r is not None:
        vote("RSI>50", r > 50, r < 50)
        vote("RSI extrem", r > 70, r < 30)

    if len(closes) >= 60:
        e9, e21, e50 = _ema(closes, 9)[-1], _ema(closes, 21)[-1], _ema(closes, 50)[-1]
        vote("EMA9/21", e9 > e21, e9 < e21)
        vote("EMA21/50", e21 > e50, e21 < e50)
        vote("preț vs EMA50", price > e50, price < e50)
    if len(closes) >= 200:
        e200 = _ema(closes, 200)[-1]
        vote("preț vs EMA200", price > e200, price < e200)

    ml, ms = _macd(closes)
    if ml is not None:
        vote("MACD vs semnal", ml > ms, ml < ms)
        vote("MACD peste 0", ml > 0, ml < 0)

    k, d = _stoch(kl)
    if k is not None:
        vote("Stoch K/D", k > d, k < d)
        vote("Stoch extrem", k > 80, k < 20)

    ub, lb = _bollinger(closes)
    if ub is not None:
        vote("Bollinger", price > ub, price < lb)

    c = _cci(kl)
    if c is not None:
        vote("CCI", c > 100, c < -100)

    w = _williams_r(kl)
    if w is not None:
        vote("Williams %R", w > -20, w < -80)

    dip, din = _di(kl)
    if dip is not None:
        vote("DI+/DI-", dip > din, dip < din)

    if len(closes) >= 11:
        vote("Momentum(10)", price > closes[-11], price < closes[-11])

    s20 = _sma(closes, 20)
    if s20:
        vote("preț vs SMA20", price > s20, price < s20)

    if len(kl) >= 21:
        hh = max(b["h"] for b in kl[-21:-1])
        ll = min(b["l"] for b in kl[-21:-1])
        vote("breakout 20 bare", price > hh, price < ll)

    return out


def consensus(kl):
    """Măsura de aglomerare. None dacă nu sunt destule bare pentru un vot valid.

    - net: bull - bear (semnul = în ce parte e turma)
    - pct: 0..100, cât de unanimă e turma (50 = split, 100 = toți bull)
    - crowded: True doar la extreme; doar acolo are sens ideea de fade
    """
    v = votes(kl)
    if len(v) < 10:
        return None
    bull = sum(1 for _, x in v if x > 0)
    bear = sum(1 for _, x in v if x < 0)
    decided = bull + bear
    if decided == 0:
        return None
    pct = 100.0 * bull / decided
    net = bull - bear
    return {
        "n": len(v), "bull": bull, "bear": bear, "net": net, "pct": pct,
        # Pragul e ales să prindă coada distribuției, nu jumătatea ei.
        "crowded": pct >= 75 or pct <= 25,
        # +1 = turma e long (deci fade = short), -1 = turma e short.
        "herd_dir": 1 if pct >= 75 else -1 if pct <= 25 else 0,
        "votes": v,
    }


def label(cons):
    """Etichetă scurtă pentru alerte/HUD."""
    if not cons:
        return "n/a"
    if cons["herd_dir"] == 1:
        return f"TURMĂ LONG {cons['bull']}/{cons['n']} ({cons['pct']:.0f}%)"
    if cons["herd_dir"] == -1:
        return f"TURMĂ SHORT {cons['bear']}/{cons['n']} ({100 - cons['pct']:.0f}%)"
    return f"split {cons['bull']}/{cons['bear']} ({cons['pct']:.0f}%)"
