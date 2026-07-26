# Rezultate de backtest — iulie 2026

Scop: o singură pagină cu ce a fost măsurat, ca deciziile să nu se ia din
memorie sau din reclamă. Toate rulările: `Actions → probe-endpoints → backtest`.

## Context: de unde a pornit

Un indicator de TradingView (`Apex Alpha MM 5.3.0`) promitea, printre altele,
un „Institutional Counter-Telemetry Matrix" și un „Consensus Radar" care
afișa `RETAIL BEAR TRAP (-17/26)`.

Pine nu are acces la poziționarea reală a nimănui pe nicio bursă, deci
`-17/26` nu putea fi altceva decât **un numărător de voturi**: 26 de
indicatori clasici, dintre care 17 bearish. Adică „consensul retail" = ce vede
un om care se uită la indicatorii standard, citit contrarian.

Ideea în sine e rezonabilă și ieftin de reprodus, deci a fost reprodusă
(`consensus.py`, 17 indicatori) și **măsurată înainte de a fi conectată**.

## Ce date sunt disponibile din GitHub Actions

Măsurat din runner, nu presupus (`tools/probe_endpoints.py`, `probe_history.py`):

| Sursă | Stare | Istoric |
|---|---|---|
| Binance spot (mirror `data-api.binance.vision`) | ✅ merge, cu taker-buy real | 12+ luni M5 |
| Binance futures (`fapi`) | ❌ HTTP 451 (geo-blocat) | — |
| Bybit | ❌ HTTP 403 (CloudFront blochează țara runnerului) | — |
| MEXC contract — funding | ✅ | ~540 zile |
| MEXC contract — open interest | ✅ doar valoarea curentă | — |
| OKX — candles, funding, OI curent | ✅ | — |
| OKX — **long/short account ratio** | ✅ | ⚠️ **2 zile la 5m**, 30 zile la 1H |

Concluzia care contează: **datele de poziționare reală există live, dar
practic fără istoric.** Nu pot fi backtestate azi, deci nu au voie în scor.
Sunt afișate ca informație și colectate în starea scannerului
(`sfp.record_crowding`), ca peste câteva săptămâni să existe istoric propriu.

## Metodologie

- 12 luni de M5, DOGEUSDT + SOLUSDT, sursă = mirror spot Binance.
- Semnalele vin din `sfp.detect_sfp`, apelat pe felii de 900 de bare —
  identic cu ce vede producția. Fără lookahead.
- Execuția respectă playbook-ul din alertă: limit la nivelul recuperat
  (valabil 6 bare), SL structural, TP1 = 1R pe 50%, apoi SL la BE,
  TP2 = pool-ul opus, time-stop 3 bare fără +0.5R.
- Ipoteze conservatoare: dacă o bară atinge și SL și TP, se presupune SL-ul
  primul; costuri 0.02% maker la intrare + 0.05% taker la fiecare ieșire.
- Se raportează și eroarea standard — fără ea, o diferență de expectancy nu
  se poate distinge de zgomot.

## Rezultat 1 — filtrul de consens: NU

DOGEUSDT, 202 tradeuri:

| Selecție | n | expectancy | WR | PF |
|---|---|---|---|---|
| toate (baseline) | 202 | −0.167R | 56% | 0.66 |
| turma CONTRA noastră (fade) | 149 | −0.165R ±0.088 | 57.7% | 0.66 |
| consens neutru | 51 | −0.203R ±0.139 | 51.0% | 0.62 |

SOLUSDT, 187 tradeuri:

| Selecție | n | expectancy | WR | PF |
|---|---|---|---|---|
| toate (baseline) | 187 | −0.277R ±0.086 | 48.1% | 0.54 |
| turma CONTRA noastră (fade) | 143 | −0.302R ±0.104 | 47.6% | 0.53 |

Praguri de aglomerare de la 60% la 85%: toate negative, pe ambele simboluri,
fără tendință monotonă. Pe DOGE fade-ul e identic cu baseline-ul (−0.165 vs
−0.167); pe SOL e mai prost decât baseline-ul.

**Verdict: ideea nu se validează.** Consensul de indicatori rămâne o linie
informativă în alertă (`🐑 Turmă: ...`) și nu intră în niciun scor și nu
filtrează nimic.

## Rezultat 2 — ce PARE să discrimineze: funding-ul

Filtrul care există deja în cod (`sfp.funding_grade`, grad A = funding extrem
pe partea aglomerată), pe DOGE:

| Selecție | n | expectancy | WR | PF |
|---|---|---|---|---|
| grad A | 29 | −0.005R ±0.146 | **72.4%** | 0.98 |
| grad B | 173 | −0.194R ±0.082 | 53.8% | 0.63 |

Separare mare, dar `n=29` și intervalul include zero — **sugestiv, nu
dovedit**. Pe SOL nu se reproduce (grad A: −0.312R). De reevaluat cu mai
multe date; nu e bază de decizie acum.

## Rezultat 3 — semnal de alarmă pe baseline-ul SFP

Agregat pe ambele simboluri: **n=389, expectancy −0.220R ±0.056**, adică
~4 erori standard sub zero. Asta contrazice backtestul documentat în
`sfp.py` (DOGE +0.29R/trade, PF 1.81).

Diferențe cunoscute între cele două măsurători:

1. **Date spot, nu perp.** Sursa a fost schimbată pentru că `fapi` dă 451 din
   Actions. Wick-urile spot vs. perp nu coincid perfect, iar detectorul
   trăiește exact din wick-uri.
2. **Regula de ieșire.** Cu SL mutat la BE imediat după TP1 și cu presupunerea
   pesimistă intrabară, un câștigător tipic aduce +0.475R iar un pierzător
   −1.07R. La 56% rată de câștig, aritmetica dă ≈ −0.20R — adică exact ce se
   observă. Structura se poate autoanula, independent de calitatea detecției.
3. **Perioadă diferită**, deci posibilă degradare a edge-ului.

Analiza de sensibilitate (același set de semnale, reguli de ieșire diferite)
e în raportul rulării — vezi artefactul `backtest-report` al ultimei rulări
`backtest`.

**Ce NU s-a schimbat pe baza asta:** niciun parametru al SFP-ului. Constatarea
e raportată, nu acționată — decizia e a proprietarului strategiei.
