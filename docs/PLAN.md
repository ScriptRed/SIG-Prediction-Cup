# Build Plan

Tick boxes as steps are completed. Competition opens 12:00 ET Thu 1 Oct 2026; trading closes 12:00 ET Wed 4 Nov 2026.

## Stage 0 — Setup
- [x] Save platform API reference and all guide pages into `docs/platform/` (register first if docs need login)
- [x] Record from docs: auth, endpoints, websocket?, rate limits, position limits, price format, tick size → summary in `docs/platform/SUMMARY.md`
- [ ] Save Kalshi and Polymarket API docs into `docs/kalshi/`, `docs/polymarket/`
- [ ] Cloud VM (US-East), Telegram bot token, `.env` created (git-ignored)
- [x] `requirements.txt`, `.gitignore`, `config/settings.yaml` skeleton

## Stage 1 — Before 1 October (manual trading with good fair values)
- [ ] 1. `models.py` (done) + `store.py` (only `events_log` so far — markets, market_map, external_prices, fair_values, orders, fills, positions tables still open)
- [x] 2. `venues/base.py` + `sim/mock_exchange.py`
- [ ] 3. `venues/kalshi.py` + `venues/polymarket.py` (read-only, poll every 5–15 s)
- [ ] 4. `config/market_map.csv` (Claude drafts, human verifies every row)
- [ ] 5. `fairvalue.py` v1
- [ ] 6. `venues/sig.py` read-only, then order placement
- [ ] 7. `dashboard/app.py` + `alerts.py` (gap > threshold, YES+NO arb, pair inconsistencies)
- [ ] 8. Election-night quote-pull rule: pull quotes in a race on a credible call and in all uncalled races in a state at poll close

## Stage 2 — 1–7 October (automated market making)
- [x] 9. `risk.py` + kill switch (tests first)
- [ ] 10. `strategies/quoter.py` (+ optional stink orders)
- [ ] 11. Offline test vs mock exchange with replayed Kalshi history
- [ ] 12. Shadow mode on real platform (log-only)
- [ ] 13. Live under the size ramp (`risk.size_ramp`: 10% launch fraction, ×2 per 120 clean reconciliations, any mismatch/unexpected 4xx/429 drops a step + alerts), reconciliation every minute. Ramp logic in `risk.py` done; still to wire: reconciliation loop → `record_reconciliation()`, `sig.py` 429s → `record_rate_limited()`, `main.py` builds `SizeRamp` from config
- [ ] 14. Docker + systemd on VM, daily summary + heartbeat to Telegram

## Stage 2b — Market tiers, ratings fair value, district model (planned 2026-09-29, not started)

Launch-day plan (1 Oct): trade by hand at the open against Kalshi mispricings and parity gaps while the bot runs in shadow mode (step 12); the bot then goes live under the size ramp (step 13).

- [ ] T1. Market tiers in `config/market_map.csv` (new `tier` column): **A** = Kalshi/Polymarket anchored, **B** = ratings-based fair value, **C** = parity only. Nothing trades automatically unless both its tier and its fair-value source allow it (A → market-based fair value; B → ratings, only once T4's review gate is passed; C → no fair-value quoting, parity/consistency trades only). `verified=false` still means no automatic trading, whatever the tier.
- [ ] T2. `fairvalue.py` second source type `"ratings"`, read from hand-maintained `config/ratings.csv` (`race_key, source, rating, date`). Rating → probability mapping and per-rating uncertainty in `settings.yaml`. Own staleness rule based on the rating `date` (max age in config), not the 60 s outside-data rule.
- [ ] T3. `config/ratings.csv` first fill (hand-maintained; no scraping of rating sites without checking ToS/robots.txt).
- [ ] T4. Safe-seat module: flag markets priced well above their ratings-based probability → alert/dashboard for manual review. No automatic trading on a flagged market until I've reviewed it.
- [ ] T5. Mid-October: district model (partisan lean + national environment + incumbency). National environment derived from Kalshi's House-control price and the Tier A races. Feeds Tier B fair values and `scenario.py` (step 15) for Phase 2.

## Stage 3 — Mid October
- [ ] 15. `scenario.py` correlated Monte Carlo; calibrate to Kalshi chamber-control prices; compare with platform
- [ ] 16. `strategies/scanner.py` consistency alerts using scenario model
- [ ] 17. `news.py` RSS → LLM classification → quote-pull triggers + alerts

## Stage 4 — Late October (election night)
- [ ] 18. `election_night/baselines.py` (MIT Election Lab county data, turnout, reporting order per state)
- [ ] 19. `election_night/ingest.py` scrapers per target state (test on 2024 pages)
- [ ] 20. `election_night/project.py` projection with vote-type adjustment
- [ ] 21. Replay test on 2022/2024 results
- [ ] 22. Confirm whether markets trade overnight on 3–4 Nov

## Stage 5 — Final days
- [ ] 23. Price candidate underdog baskets with `scenario.py` vs leaderboard position
- [ ] 24. Manual decision; loosen risk limits via config; enter basket
