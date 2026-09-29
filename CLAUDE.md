# CLAUDE.md

Trading system for the Susquehanna Predictions Cup (simulated prediction markets on the 2026 U.S. midterms, 1 Oct – 4 Nov 2026). Strategy and competition rules: `docs/PROJECT_BRIEF.md`. Build order and current status: `docs/PLAN.md`. Platform API reference: `docs/platform/`.

## Hard rules (never break these)

1. **Every order goes through `risk.check()`.** No code path may call a venue's `place_order` directly. Tests must enforce this.
2. **LLMs never place or size orders.** LLM output (news classification, market mapping) may only produce alerts, flags or quote-pull triggers.
3. **Do not guess the competition platform's API.** If an endpoint, field, limit or behaviour is not in `docs/platform/`, stop and ask. Leave a `# TODO(api):` marker rather than inventing it.
4. **One account, strictly individual.** Never write code that coordinates with, transfers value to, or trades against other participants' accounts deliberately.
5. **Respect rate limits** with a safety margin, and respect robots.txt / terms of service for any scraping. Kalshi and Polymarket are read-only (market data only).
6. **Secrets only via environment variables** (`.env`, git-ignored). Never commit keys, never log them.
7. **Kill switch must always work:** Telegram `/kill` command or the file `KILL` in the repo root → cancel all orders, stop quoting.
8. **Tests first** for `risk.py`, order/position reconciliation, and anything touching orders.

## Platform API rules (see `docs/platform/SUMMARY.md`)

- The full OpenAPI spec is `docs/platform/openapi.json`. Treat it as the source of truth for request and response shapes. Search it for the endpoint you need; don't read the whole file.
- Base URL `https://www.thesuper.market/api/v1`, Bearer key with read + trade scopes.
- **Always pass the Cup's `tournamentId`** on every read and order. Never rely on the org default.
- Positions/P&L come from **`/tournaments/{slug}/portfolio/*`**, never the no-argument `/portfolio/*` reads.
- Prices are YES-normalized; limit prices must be on the **0.005 tick** within [0.005, 0.995]. Round in the adapter.
- **Every order gets a fresh `idempotencyKey`**; retries reuse the same key and identical payload. On `502 ORDER_STATUS_UNKNOWN`, reconcile or retry with the same key — never a new key.
- Retry only 429, 503 and 409 `REQUEST_IN_FLIGHT`, with exponential backoff + jitter. Never auto-retry other 4xx.
- **Every resting quote has a short `expirationDate`** (dead-man's switch).
- Re-quote = scoped `POST /orders/cancel-all` → re-post only after 200 (on 207/422 confirm no open orders remain first). Two-sided quotes use atomic `POST /orders/multi-leg`.
- Realtime: subscribe to `tournament:{tournament_id}` (Cup trades are not on `market:*`) and `user:{profile_id}`; resync from REST on revision gaps, reconnects and token refresh (tokens last 3 h).

## Conventions

- Python 3.12, `asyncio`, `httpx`, `websockets`, `pydantic` v2 models, SQLite (WAL mode), `numpy`/`scipy`, Streamlit dashboard, `python-telegram-bot`, `pytest`.
- **All prices internally are floats in [0, 1]** = probability / SUSQies per share. Convert at the venue boundary only.
- All timestamps UTC, timezone-aware.
- Type hints everywhere; small pure functions for models and maths so they are testable.
- Config in `config/settings.yaml`; nothing tunable hard-coded.
- Log every quote, order, fill, cancel, fair-value change and risk rejection to the `events_log` table.
- Commit after each passing step with a clear message.

## Architecture

```
predcup/
  CLAUDE.md
  docs/                      PROJECT_BRIEF.md, PLAN.md, platform/, kalshi/, polymarket/
  config/
    settings.yaml            limits, thresholds, schedules (no secrets)
    market_map.csv           platform_id, kalshi_ticker, poly_token_id, polarity, rule_diff_notes, confidence, verified
  predcup/
    models.py                Market, OrderBook, Order, Fill, Position (pydantic)
    store.py                 SQLite persistence
    venues/
      base.py                abstract Venue interface
      sig.py                 competition platform adapter
      kalshi.py              read-only
      polymarket.py          read-only (Gamma + CLOB, no auth needed for reads)
    fairvalue.py             blend sources → fair value + uncertainty
    scenario.py              correlated Monte Carlo (consistency checks + Phase 2 sizing)
    risk.py                  every order passes here; limits, kill switch
    strategies/
      quoter.py              market making (simplified Avellaneda–Stoikov)
      scanner.py             arbitrage / consistency alerts
    news.py                  RSS → LLM classification → quote-pull triggers
    election_night/
      baselines.py           county expected results
      ingest.py              results scrapers
      project.py             live projection
    alerts.py                Telegram
    main.py                  orchestration loop
  dashboard/app.py           Streamlit
  sim/mock_exchange.py       fake order book implementing Venue, for offline tests
  tests/
```

### Venue interface (`venues/base.py`)

`get_markets()`, `get_book(market_id)`, `place_order(market_id, side, price, size)`, `cancel(order_id)`, `cancel_all()`, `get_positions()`, `get_balance()`. `sim/mock_exchange.py` implements the same interface; every strategy must run against it before going live.

### Key logic

- **Fair value:** liquidity-weighted average of Kalshi and Polymarket prices in log-odds space, adjusted for resolution differences from `market_map.csv`, optional manual override. Uncertainty widens with venue disagreement, thin books and stale data. No external match → no automatic trading. A `market_map.csv` row with `verified=false` counts as no match — `fairvalue.py` must never trade off an unverified row, however high its `confidence`.
- **Quoter:** reservation price = fair value − k × inventory; half-spread = max(min_edge, c × uncertainty). Re-quote when fair value moves > 1 point or outside prices move sharply; pull all quotes before scheduled events in config; stop if outside data is > 60 s stale.
- **Risk (Phase 1 defaults, in config):** max 5% bankroll per market, max 60% total exposure, per-market max order size, price must be within X points of fair value, daily loss stop 8%, stale-data stop. **Size ramp:** order size and per-market cap start at `risk.size_ramp.launch_fraction` and step up ×`step_multiplier` only after N clean reconciliations with no unexpected 4xx/429; any failure drops one step and alerts. Phase 2 loosens limits only via an explicit config change.
- **Reconciliation:** compare local positions with `get_positions()` every minute; on mismatch → cancel all, halt, alert.
- **Netting:** holding YES and buying NO cancels pairs and pays 1 per pair immediately; position logic must model this.

## Commands

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pytest                      # run tests
python -m predcup.main      # run the system (reads .env and config/settings.yaml)
streamlit run dashboard/app.py
```

## How to work in this repo

- Before starting, read `docs/PLAN.md`, find the next unchecked step, and confirm the plan with me.
- One step per session. Write tests first, implement, run `pytest`, then tick the box in `docs/PLAN.md` and commit.
- If a design decision changes, update this file or `docs/PLAN.md` in the same commit.
