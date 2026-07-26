# crypto-scanner (CryptoIntel)

Scanner de piață cripto care rulează **fără server propriu**, pe GitHub Actions,
și trimite alerte pe Telegram. Nu necesită browser sau telefon deschis.

## Arhitectură

```
GitHub Actions (cron)
        │
        ▼
   scanner.py ──────► Telegram (alerte)
        │
        ├─► datasrc.py    sursele de date (klines, funding, poziționare)
        ├─► sfp.py        strategia SFP / Playbook 1
        └─► consensus.py  Consensus Radar (informativ)
```

Un singur job lung (~5h45m) cu buclă internă la 5 minute, nu cron la 5 minute:
cron-ul GitHub pe free tier era executat în realitate o dată la 1-2 ore, deci
alertele veneau rar sau deloc.

## Fișiere

| Fișier | Rol |
|---|---|
| `scanner.py` | Motorul principal: Confluence Score, Liquidity Sweep, alerte de preț, Telegram, bucla de scanare |
| `sfp.py` | Strategia SFP (sweep & reclaim pe M5) — detectorul, nivelurile, mesajul de alertă |
| `datasrc.py` | Stratul de date: rezolvarea sursei per simbol, klines, ticker, funding, long/short ratio |
| `consensus.py` | Consensus Radar — 17 indicatori clasici votează; **informativ, nu intră în scor** |
| `tools/` | Probe-uri de endpoint, self-test, backtest (rulate manual din Actions) |
| `BACKTEST.md` | Rezultatele măsurate — de citit înainte de a modifica strategia |
| `watchlist.txt` | Simboluri de urmărit (fallback dacă watchlist-ul din cloud nu e configurat) |

## Cele trei semnale, și ce încredere au

1. **Confluence Score** (`scanner.py`) — scor 0-100 din EMA multi-timeframe,
   RSI, volum+CVD, Fibonacci, Markov, filtrat prin ADX. Marcat explicit în
   alertă ca **informativ**: backtestul nu a găsit edge la intrare mecanică.
2. **Liquidity Sweep** (`scanner.py`, `sweep_signal`) — sweep + reclaim pe 15m.
3. **SFP / Playbook 1** (`sfp.py`) — sweep & reclaim pe M5 la niveluri de
   lichiditate (PDH/PDL, high/low-ul zilei, equal highs/lows, numere rotunde,
   range-ul Asiei), cu divergență CVD la extremă. Rulează **doar** în primele
   90 de minute ale sesiunilor Londra și NY. Singura strategie cu edge raportat
   în backtestul original (+0.29R/trade pe DOGE, PF 1.81) — **dar vezi
   `BACKTEST.md`: re-măsurarea nu reproduce cifra.**

Execuția e discreționară peste tot. Scannerul semnalează, nu tranzacționează.

## Sursele de date

Runnerele GitHub sunt geo-blocate pe Binance, ceea ce dictează toate alegerile:

| Sursă | Stare din Actions |
|---|---|
| `data-api.binance.vision` (mirror spot) | ✅ singura cu **taker-buy volume real**, deci singura unde CVD-ul e real |
| `fapi.binance.com` / `api.binance.com` | ❌ HTTP 451 |
| Bybit | ❌ HTTP 403 |
| MEXC contract | ✅ funding (~540 zile istoric), OI curent |
| OKX | ✅ long/short account ratio — dar **doar 2 zile de istoric la 5m** |

`datasrc.py` rezolvă sursa o singură dată per simbol:
mirror Binance → spot MEXC → futures MEXC. Spot-ul MEXC are prioritate față de
futures pentru că tickerele se pot ciocni (contractul `EWT_USDT` futures e cu
totul alt activ decât tokenul EWT spot).

## Configurare (variabile de mediu / GitHub Secrets)

Nu există chei în cod. Toate sunt din mediu:

| Variabilă | Rol |
|---|---|
| `TG_TOKEN`, `TG_CHAT_ID` | Telegram. Fără ele, scannerul rulează dar nu trimite |
| `MIN_SCORE` | Pragul de Confluence Score pentru alertă (implicit 65) |
| `SFP_SYMBOLS` | Pe ce perechi rulează SFP (implicit `DOGEUSDT,SOLUSDT`) |
| `SCAN_INTERVAL_SEC` | Intervalul buclei interne (implicit 300) |
| `MAX_RUNTIME_SEC` | Ieșire curată înainte de limita de 6h a jobului |
| `RUN_ONCE` | `1` = o singură trecere apoi ieși |
| `ALERTS_URL`, `WATCHLIST_URL`, `ALERTS_KEY` | Alerte de preț și watchlist din Netlify Blobs (opțional) |
| `COLLECT_CROWDING` | `0` oprește colectarea de date de poziționare |

## Rulare

```bash
pip install -r requirements.txt

TEST_PING=1 python scanner.py          # verifică livrarea pe Telegram
RUN_ONCE=1  python scanner.py          # o singură trecere
            python scanner.py          # buclă continuă
```

Din GitHub Actions: workflow-ul `crypto-scan` pornește singur pe cron.
Workflow-ul `probe-endpoints` (manual) rulează probe-urile, self-testul și
backtestul — niciunul nu atinge Telegram-ul.

## Principii respectate în cod

- **Ce nu poate fi backtestat nu intră în scor.** Datele de poziționare reală
  (long/short ratio) sunt afișate și colectate, dar nu influențează nimic,
  pentru că sursa nu are istoric suficient ca să fie validate.
- **Degradare, nu prăbușire.** Dacă funding-ul lipsește, gradul devine B. Dacă
  o sursă tace, simbolul e sărit. O eroare într-o parte nu oprește scanarea.
- **Fără filtre care se dezactivează în tăcere.** Pe surse fără taker-buy
  volume, CVD-ul ar fi o linie plată și testul de divergență din SFP ar trece
  întotdeauna — de aceea acele simboluri sunt sărite explicit.
