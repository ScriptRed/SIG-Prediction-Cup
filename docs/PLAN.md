# Build Plan

## Launch status (updated as each piece lands; no live automated orders until every item is done and the user says go)

- [x] NH/VT Governor Kalshi mapping (VT → GOVPARTYVT-26; NH stays on GOVPARTYNH-28, which is the 2026 race)
- [x] launch_report: tradeable-edge columns, unmapped-race section, "trade by hand at the open" list
- [x] LAUNCH_CHECKLIST §D: SIG seed quotes vs Kalshi moves
- [ ] `venues/sig.py`: reads, orders, cancel-all, batch, idempotency, retries, `record_rate_limited()`
- [ ] `fairvalue.py` v1 (Kalshi mid, verified Tier A only)
- [ ] `quoter.py` v1 (20–30 markets, small size, batch, configurable expiry)
- [ ] `main.py` in shadow mode (ramp, fusion set, risk manager, loop lag; hooks for KILL watcher + Telegram)
- [ ] Reconciliation loop (`/tournaments/{slug}/portfolio/positions` → `record_reconciliation()`)
- [ ] Merge `safety` branch (KILL watcher, Telegram /kill /resetramp, systemd), wire into `main.py` — built in a separate session
- [ ] Multi-hour run against the mock exchange


Tick boxes as steps are completed. Competition opens 12:00 ET Thu 1 Oct 2026; trading closes 12:00 ET Wed 4 Nov 2026.

## Stage 0 — Setup
- [x] Save platform API reference and all guide pages into `docs/platform/` (register first if docs need login)
- [x] Record from docs: auth, endpoints, websocket?, rate limits, position limits, price format, tick size → summary in `docs/platform/SUMMARY.md`
- [ ] Save Kalshi and Polymarket API docs into `docs/kalshi/`, `docs/polymarket/` (Kalshi done 2026-09-30: `openapi.yaml`, market-data quick start, rate limits; Polymarket still open)
- [ ] Cloud VM (US-East), Telegram bot token, `.env` created (git-ignored)
- [x] `requirements.txt`, `.gitignore`, `config/settings.yaml` skeleton

## Stage 1 — Before 1 October (manual trading with good fair values)
- [ ] 1. `models.py` (done) + `store.py` (`events_log`, `ramp_state`, `orders`, `fills`, `positions` done — markets, market_map, external_prices, fair_values tables still open)
- [x] 2. `venues/base.py` + `sim/mock_exchange.py`
- [ ] 3. `venues/kalshi.py` + `venues/polymarket.py` (read-only, poll every 5–15 s). Minimal `kalshi.py` done (GET market/event, parsed per `docs/kalshi/openapi.yaml`); polling + `Venue` interface still open
- [ ] 4. `config/market_map.csv` (Claude drafts, human verifies every row). Senate redrafted 2026-09-30: Kalshi lists 2026 Senate generals as `SENATE<ST>-26` events (candidate-named YES labels, party-based rules; specials `SENATE<ST>S`, Louisiana `KXSENATELA-26NOV`; `SENATELA-26` is the Kentucky race), matched by event title — all 70 state Senate rows now have a Kalshi ticker, none verified
- [x] 4a. **Launch-critical:** `scripts/show_mapping.py` for hand-verifying the map race by race: SIG side (Cup book, rules text or an explicit "none"), mapped Kalshi market (titles, outcomes, rules, dates, bid/ask, volume), polarity in words, warnings (primary/non-2026 contract, mids > 10 pts apart after polarity, Kalshi spread > 5 pts, low volume; thresholds in `settings.yaml` `mapping_review`). `--list` shows every race's tier/verified/confidence; `--mark-verified` sets `verified=true, tier=A` on that race's rows only after a typed `yes`. GET-only against both venues
- [x] 4b. **Launch-critical:** `scripts/launch_report.py` (GET-only): every Kalshi-mapped SIG market's Cup top of book vs Kalshi, polarity-adjusted gap, longshot overpricing (SIG ask − Kalshi where Kalshi < 10%), flags (party/state mismatch, not 2026, gap > 3 pts, empty/one-sided books, low volume, exclude list AK/GA/ME + all Independents; `settings.yaml` `launch_report`). Writes `data/launch_report.csv`, prints clean/flagged races and top 10 longshots
- [ ] 5. `fairvalue.py` v1
- [ ] 6. `venues/sig.py` read-only, then order placement
- [ ] 7. `dashboard/app.py` + `alerts.py` (gap > threshold, YES+NO arb, pair inconsistencies)
- [ ] 8. Election-night quote-pull rule: pull quotes in a race on a credible call and in all uncalled races in a state at poll close

## Stage 2 — 1–7 October (automated market making)
- [x] 9. `risk.py` + kill switch (tests first). 2026-09-30: fusion-risk races kept off the net R-vs-D axis (`fusion_race_keys`, required); `main.py` must build that set from `market_map.csv` via `predcup.market_map.fusion_race_keys`
- [ ] 10. `strategies/quoter.py` (+ optional stink orders)
- [ ] 10a. Loop-lag metric (`predcup/looplag.py`, done): still to wire — `main.py` starts `run_probe()`, quoter and reconciliation loops call `record()` each iteration. Heavy work (scenario, district model, election-night projection, dashboard) runs in separate processes and hands results over via SQLite (CLAUDE.md "Process split")
- [ ] 11. Offline test vs mock exchange with replayed Kalshi history
- [ ] 12. Shadow mode on real platform (log-only)
- [ ] 13. Live under the size ramp (`risk.size_ramp`: 10% launch fraction, ×2 per 45 clean reconciliations; mismatch/unexpected 4xx drops a step, 429s only on a burst of >5 in 10 min; step persisted, restart resumes one below; reset via `/resetramp` or `scripts/reset_ramp.py`), reconciliation every minute. Ramp logic done; still to wire: reconciliation loop → `record_reconciliation()`, `sig.py` 429s → `record_rate_limited()`, `main.py` builds `SizeRamp`, Telegram `/resetramp`
- [ ] 14. Docker + systemd on VM, daily summary + heartbeat to Telegram

## Stage 2b — Market tiers, ratings fair value, district model (planned 2026-09-29, not started)

Launch-day plan (1 Oct): trade by hand at the open against Kalshi mispricings and parity gaps while the bot runs in shadow mode (step 12); the bot then goes live under the size ramp (step 13).

- [ ] T1. Market tiers in `config/market_map.csv` (`tier` column added 2026-09-30, blank until assigned; `show_mapping --mark-verified` sets A): **A** = Kalshi/Polymarket anchored, **B** = ratings-based fair value, **C** = parity only. Nothing trades automatically unless both its tier and its fair-value source allow it (A → market-based fair value; B → ratings, only once T4's review gate is passed; C → no fair-value quoting, parity/consistency trades only). `verified=false` still means no automatic trading, whatever the tier.
- [ ] T2. `fairvalue.py` second source type `"ratings"`, read from hand-maintained `config/ratings.csv` (`race_key, source, rating, date`). Rating → probability mapping and per-rating uncertainty in `settings.yaml`. Own staleness rule based on the rating `date` (max age in config), not the 60 s outside-data rule.
- [ ] T3. `config/ratings.csv` first fill (hand-maintained; no scraping of rating sites without checking ToS/robots.txt).
- [ ] T4. Safe-seat module: flag markets priced well above their ratings-based probability → alert/dashboard for manual review. No automatic trading on a flagged market until I've reviewed it.
- [ ] T5. Mid-October: district model (partisan lean + national environment + incumbency). National environment derived from Kalshi's House-control price and the Tier A races. Feeds Tier B fair values and `scenario.py` (step 15) for Phase 2. Runs as a separate process; writes results to SQLite for the bot to read.

## Stage 3 — Mid October
- [ ] 14a. (Low priority, after launch; requested 2026-09-30) Find which Cup states allow fusion voting, citing a source (Ballotpedia or the state's election law). Add a `fusion_check_needed` column to `market_map.csv`, true for races in those states. The quoter must skip any race with `fusion_check_needed=true` whose `fusion_risk` hasn't been explicitly reviewed. Open design point: `fusion_risk` currently defaults to `false`, so a reviewed `false` can't be told apart from an unreviewed one. Needs a review marker (e.g. `fusion_reviewed` column, or blank = unreviewed), and `fusion_race_keys` must treat unreviewed as not safe
- [ ] 15. `scenario.py` correlated Monte Carlo; calibrate to Kalshi chamber-control prices; compare with platform (separate process, results via SQLite)
- [ ] 16. `strategies/scanner.py` consistency alerts using scenario model
- [ ] 17. `news.py` RSS → LLM classification → quote-pull triggers + alerts

## Stage 4 — Late October (election night)
- [ ] 18. `election_night/baselines.py` (MIT Election Lab county data, turnout, reporting order per state)
- [ ] 19. `election_night/ingest.py` scrapers per target state (test on 2024 pages)
- [ ] 20. `election_night/project.py` projection with vote-type adjustment (separate process, results via SQLite)
- [ ] 21. Replay test on 2022/2024 results
- [ ] 22. Confirm whether markets trade overnight on 3–4 Nov

## Stage 5 — Final days
- [ ] 23. Price candidate underdog baskets with `scenario.py` vs leaderboard position
- [ ] 24. Manual decision; loosen risk limits via config; enter basket
