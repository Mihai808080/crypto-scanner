"""Formatare de prețuri, comună pentru scanner.py și sfp.py.

Fișier separat ca să nu apară import circular: scanner importă sfp, deci sfp
nu poate importa înapoi din scanner.
"""


def fmt_price(p):
    """Preț cu destule zecimale ca să se vadă diferența dintre niveluri.

    BUG REPARAT (iul. 2026): mesajele Telegram foloseau `%.4f` fix. Pentru o
    monedă la 0,002441, entry (0,002441) și SL (0,002431) se rotunjeau AMÂNDOUĂ
    la „0,0024" — păreau identice, deși valorile calculate erau corecte. La
    monede sub 0,0001 (1000BONK, 1000SHIB) toate nivelurile apăreau „0,0000".

    Numărul de zecimale se adaptează la mărimea prețului, iar zerourile de la
    coadă se taie ca să nu iasă un șir inutil de lung.
    """
    try:
        p = float(p)
    except (TypeError, ValueError):
        return "—"
    a = abs(p)
    if a >= 1000:
        d = 2
    elif a >= 100:
        d = 3
    elif a >= 1:
        d = 4
    elif a >= 0.01:
        d = 6
    elif a >= 0.0001:
        d = 8
    else:
        d = 10
    s = f"{p:,.{d}f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s
