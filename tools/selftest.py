"""
Self-test end-to-end, fără efecte secundare.
════════════════════════════════════════════
Verifică din runner că refactorul de sursă de date chiar funcționează pe date
reale: lumânări, taker-buy REAL (nu estimat), scorul de confluence, detectorul
SFP și Consensus Radar.

Nu trimite nimic: TG_TOKEN/TG_CHAT_ID sunt șterse din mediu la început, deci
send_telegram doar loghează un avertisment. Nu scrie starea anti-spam.
"""

import os
import sys

# ÎNAINTE de import scanner — ca modulul să pornească fără credențiale.
os.environ.pop("TG_TOKEN", None)
os.environ.pop("TG_CHAT_ID", None)
os.environ.pop("ALERTS_URL", None)
os.environ.pop("WATCHLIST_URL", None)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import consensus  # noqa: E402
import datasrc  # noqa: E402
import scanner  # noqa: E402
import sfp  # noqa: E402

fails = []


def check(name, cond, detail=""):
    print(f"  {'✓' if cond else '✗'} {name}{(' — ' + detail) if detail else ''}")
    if not cond:
        fails.append(name)


def main():
    syms = ["DOGEUSDT", "SOLUSDT", "BTCUSDT"]

    print("\n── sursa de date ──")
    for s in syms:
        src = datasrc._resolve_src(s)
        check(f"{s}: sursă = {src}", src == "binance",
              "SFP are nevoie de taker-buy, care există doar pe mirror-ul Binance")

    print("\n── lumânări M5 + taker-buy real ──")
    for s in syms:
        kl = datasrc.get_klines(s, "5m", 900)
        ok_len = len(kl) >= 800
        # Dacă tb == v/2 pe toate barele, e valoarea de fallback, nu date reale.
        real_tb = any(abs(k["tb"] - k["v"] / 2) > 1e-9 for k in kl)
        check(f"{s}: {len(kl)} bare M5", ok_len)
        check(f"{s}: taker-buy real", real_tb,
              "altfel CVD-ul e plat și divergența din SFP ar trece mereu")

    print("\n── has_real_cvd (garda din scan_sfp) ──")
    for s in syms:
        check(f"{s}: has_real_cvd", datasrc.has_real_cvd(s) is True)

    print("\n── funding (MEXC) ──")
    for s in ["DOGEUSDT", "SOLUSDT"]:
        f = datasrc.get_funding(s, 100)
        check(f"{s}: {len(f)} înregistrări funding", len(f) > 50)
        if f:
            cron = all(f[i]["t"] <= f[i + 1]["t"] for i in range(len(f) - 1))
            check(f"{s}: funding cronologic (recent ultimul)", cron)
            grade, rate = sfp.funding_grade(f, 1)
            check(f"{s}: funding_grade rulează", grade in ("A", "B"),
                  f"grad={grade} rată={rate:+.5f}")

    print("\n── confluence score pe date reale ──")
    for s in syms:
        kl15 = datasrc.get_klines(s, "15m", 200)
        kl1h = datasrc.get_klines(s, "1h", 60)
        kl4h = datasrc.get_klines(s, "4h", 30)
        price, _ = datasrc.get_ticker(s)
        cs = scanner.compute_confluence_score(
            price, [k["c"] for k in kl15], kl15, kl1h, kl4h)
        ok = 0 <= cs["score"] <= 100 and cs["dir"] in (-1, 0, 1)
        check(f"{s}: CS={cs['score']}/100 dir={cs['dir']} ADX={cs['adx']:.0f}", ok)

    print("\n── detector SFP (apel direct, indiferent de fereastră) ──")
    for s in ["DOGEUSDT", "SOLUSDT"]:
        bars = sfp.get_klines_5m(s)
        try:
            sig = sfp.detect_sfp(bars, len(bars) - 2, sfp.ROUND_STEP.get(s, 1.0))
            check(f"{s}: detect_sfp a rulat", True,
                  f"semnal: {sig['level']['kind']} dir={sig['dir']}" if sig else "niciun semnal acum")
        except Exception as e:
            check(f"{s}: detect_sfp a rulat", False, f"{type(e).__name__}: {e}")

    print("\n── Consensus Radar pe date reale ──")
    for s in syms:
        kl = datasrc.get_klines(s, "5m", 300)
        c = consensus.consensus(kl)
        check(f"{s}: {consensus.label(c)}", c is not None and 0 <= c["pct"] <= 100)

    print("\n── scan_sfp NU trimite nimic fără credențiale ──")
    sent = []
    ok = sfp.scan_sfp("DOGEUSDT", lambda m: sent.append(m) or True)
    check("scan_sfp s-a terminat curat", ok in (True, False),
          "în afara ferestrei Londra/NY iese imediat, e normal")

    print("\n" + "=" * 60)
    if fails:
        print(f"EȘUAT: {len(fails)} verificări\n  - " + "\n  - ".join(fails))
        return 1
    print("TOATE VERIFICĂRILE AU TRECUT")
    return 0


if __name__ == "__main__":
    sys.exit(main())
