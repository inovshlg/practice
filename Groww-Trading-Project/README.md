# 9/21 EMA Crossover · NIFTY options · Groww

A personal algo-trading workbench built on Flask + PostgreSQL + the official
[`growwapi`](https://pypi.org/project/growwapi/) SDK:

| Page | What it does |
|---|---|
| **Account** (`/`) | TOTP login to Groww, profile, funds / margins, open positions |
| **Data** (`/data`) | Downloads 1-minute NIFTY spot + the option contracts the strategy needs into Postgres (incremental). Can also generate a synthetic **demo** market to try the app without keys |
| **Backtest** (`/backtest`) | Runs the strategy for 5/15/30m, any entry window, all three execution modes side by side; trade-by-trade audit on a candlestick chart |
| **Live / Paper** (`/live`) | Starts the same strategy in **paper** or **live** mode during market hours, with risk limits, a kill switch, a journal and a "replay a stored day" rehearsal |

---

## 1. Setup

```bash
cd ~/Groww-Trading-Project
./scripts/setup.sh                 # installs Postgres if missing, creates DB + venv
cp keys.txt.example keys.txt       # (setup.sh does this too)
```

`keys.txt` (never committed — it is in `.gitignore`):

```
API_KEY=<your Groww TOTP API key>
TOTP_SECRET=<the TOTP secret shown when you created the key>
```

Two bare lines (key, then secret) also work. A fresh 6-digit TOTP is generated with
`pyotp`, exchanged for an access token, and the token is cached in
`.groww_token.json` until 06:00 IST the next day.

```bash
.venv/bin/python run.py            # http://127.0.0.1:5000
.venv/bin/python -m pytest         # 87 tests (needs the groww_trading_test DB from setup.sh)
```

Environment overrides: `DATABASE_URL`, `GROWW_KEYS_FILE`, `FLASK_HOST`, `FLASK_PORT`, `FLASK_SECRET_KEY`.

**Hosting it on a domain (e.g. behind nginx):** run **one** process — the trading
engine lives inside it:

```bash
.venv/bin/gunicorn -w 1 --threads 8 -b 127.0.0.1:5000 run:app
```

Never use more than one worker (`-w 1`) or you would get several engines. Put the
site behind authentication (nginx basic-auth / Cloudflare Access): the Live page can
place real orders.

---

## 2. Plan / architecture

```
app/
  config.py            constants: lot size 65, strike step 50, ITM depth 5, friction rates, risk limits
  groww_client.py      TOTP auth, token cache, rate limiter + retry, candle parsing, orders
  db.py                Postgres schema + Database (candles, fetch log, expiries, backtest cache, live journal)
  data_loader.py       GrowwSource / DemoSource -> DataLoader (incremental) -> Postgres
  strategy/
    indicators.py      resample (09:15-aligned), EMA (SMA seed), crossover detection   <- single source of truth
    rules.py           exit / reversal / entry state machine                            <- shared by backtest + live
    contracts.py       strike / CE-PE / expiry selection, Groww symbol format          <- shared by backtest + live
    costs.py           slippage + Indian options cost stack                              <- shared by backtest + live
    backtest.py        event-driven backtester (all three modes)
    metrics.py         win rate, PF, payoff, expectancy, max DD (₹ and %)
  live/engine.py       TradingEngine, PaperBroker, GrowwBroker, GrowwMarketData, replay
  web.py               Flask pages + JSON API, LRU caches
  templates/, static/  UI (vanilla JS + TradingView lightweight-charts, vendored)
tests/                 87 tests: costs, indicators, rules, backtest scenarios, metrics, engine safety,
                       broker handling, journal/recovery, replay==backtest parity, API
```

Because the backtester and the live engine call the *same* signal, rule, contract and
cost functions, a paper replay of a stored day reproduces the backtest trade-for-trade
(this is asserted in `tests/test_engine.py::test_replay_reproduces_backtest_trade_for_trade`).

---

## 3. Strategy rules — every ambiguity made explicit

| Topic | Decision |
|---|---|
| Signal source | 9 EMA vs 21 EMA on **closed** NIFTY **spot** bars (not futures, not option prices) |
| Resampling | 1m → 5/15/30m, buckets aligned to **09:15** (30m: 09:15, 09:45 … 15:15). The last bar of the day may be shorter (15:15–15:30) |
| EMA | α = 2/(n+1), seeded with the SMA of the first n closes (TradingView style). **Continuous across days** — prior sessions warm up today's EMA (the loader downloads 14 extra days). No signal until EMA21 exists |
| Cross definition | First closed bar where sign(EMA9 − EMA21) differs from the last **non-zero** sign |
| Ties | An exact tie (difference = 0) is never a signal; it is skipped and comparison carries to the next bar |
| Signal time T | Close time of the signal bar (bar start + timeframe) |
| **Which bar fills** | The **open of the option's 1-minute candle starting at T** (first minute after the signal bar closes). Live: the order is sent within seconds of T |
| Missing fill candle | Use the first candle within 5 min (flag `fill_delayed`); otherwise skip the entry (listed in "skipped entries"). Exits fall back to the last close (flag `stale_exit`) |
| Entry window | Start ∈ {09:30, 10:00, 10:30}, cutoff ∈ {11:30 … 14:30} every 30 min. A cross opens a trade only if **start ≤ T ≤ cutoff, both inclusive** |
| Exit | First opposite cross **at any time before 15:00** (even after the cutoff), else **square-off at the 15:00 candle open** |
| Reversal | Opposite cross **inside** the window: close and open the reverse trade at the same minute. Outside the window: close only |
| T ≥ 15:00 | Never opens a trade |
| Position count | One position per mode; modes are simulated independently on identical signals |
| Bullish → | ITM5 buy: buy CE at ATM−250 · ATM buy: buy ATM CE · ATM sell: **sell ATM PE** |
| Bearish → | ITM5 buy: buy PE at ATM+250 · ATM buy: buy ATM PE · ATM sell: **sell ATM CE** |
| ATM | Nearest 50 to the **signal-bar close**; exact halves round **up** |
| Expiry | Nearest weekly expiry on/after the trade date (from Groww's expiry calendar). On expiry day the **0-DTE** contract is traded |
| Size | Lot size 65 × lots (default 1) |
| Cross # filter | Ordinal among crosses **inside today's entry window** (1st = first tradable signal). `day_cross_no` (counting from 09:15) is also shown |
| Whipsaw | Closed by an opposite cross within **2 signal bars** of entry |
| No-trade days | Listed with reason: no cross / no cross inside the window / every entry skipped / no expiry |
| Win | net PnL > 0 (₹0 counts as a loss) |
| Max DD % | Peak-to-trough of closed-trade equity (₹2,00,000 + cumulative net), % of the running peak |

### Friction

* **Slippage** (always adverse): buy = price × 1.005 + 0.5 pt, sell = price × 0.995 − 0.5 pt,
  floored at ₹0.05 and rounded to the 0.05 tick in the adverse direction.
* **Charges** per executed order: brokerage ₹20 (Groww F&O flat), **STT 0.15 % of sell premium**
  (rate after Budget 2026 — was 0.1 %; edit `config.py` if you want the old rate), NSE
  transaction charge 0.03503 %, SEBI ₹10/crore, stamp duty 0.003 % on buys, GST 18 % on
  brokerage + exchange + SEBI.
* **Gross** = PnL on raw candle prices, **Net** = Gross − slippage − charges. Live mode books
  Groww's actual average fill prices, so simulated slippage is not added on top.

---

## 4. Live / paper engine

* Polls every 5 s. Today's spot candles come from Groww; earlier sessions (EMA warm-up) from Postgres.
* Uses the same closed-bar rule: a bar is used only once the clock passes its close.
* **Only crosses that close after you press Start** are traded — older ones are logged and ignored.
* **Paper**: fills at the option LTP; slippage + charges applied exactly as in the backtest.
* **Live**: `MARKET` / `MIS` / `DAY` orders on NSE FNO; the engine polls the order until it is
  `EXECUTED/COMPLETED` (30 s timeout) and books `average_fill_price`.
* Guard rails: max trades/day, max realised loss/day (halts entries), max 4 lots, stale-data
  guard (no entries if the last spot candle is > 3 min old), automatic 15:00 square-off,
  **Stop & flatten** button, typed `LIVE` confirmation.
* If a **live exit** is rejected 3× or cannot be confirmed, the engine **HALTS**, never re-sends
  (to avoid a double order) and tells you to square off manually in the Groww app.
* Everything (sessions, signals, orders, fills, positions) is journaled in Postgres. If the
  server restarts mid-day, starting again re-adopts today's open position — verify it in Groww.
* **Replay** drives the real engine minute-by-minute through a stored day: rehearse before going live.

> Start with **paper** for several sessions and compare with the backtest for the same days
> before switching to live. Option selling (ATM sell) needs the full SPAN + exposure margin.

---

## 5. Caching

* Backtest results: in-process LRU **and** a Postgres cache keyed by (all parameters + data version);
  the browser also memoises runs, so flipping timeframe/window back and forth is instant.
* Trade charts: server LRU per (day, timeframe, contract) + browser cache, with the next two trades
  prefetched so ↑/↓ navigation is instant.
* Any new download bumps the data version, which invalidates every cache automatically.

## 6. Data notes

* Groww historical API: `get_historical_candles` (1m) for `NSE-NIFTY` (CASH) and option
  `groww_symbol`s like `NSE-NIFTY-30Sep25-24650-PE` (FNO), expiries via `get_expiries`.
* Only contracts the strategy could trade are fetched (≈ 4–12 per day), not whole chains.
* `fetch_log` records each (symbol, day), so re-runs only fetch new days.
* Demo data is a synthetic market and is labelled **DEMO** everywhere; switching source wipes the
  other source's data so the two are never mixed.
