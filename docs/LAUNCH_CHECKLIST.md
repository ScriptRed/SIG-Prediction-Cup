# Launch Checklist — Predictions Cup

Trading opens **Thursday 1 October 2026, 12:00 ET = 17:00 London (BST)**.
Record every answer in the "Found" column, then copy findings into `docs/platform/SUMMARY.md` or `docs/PROJECT_BRIEF.md` §4 and commit.

---

## A. Before Thursday (by Wednesday night)

### Account and API
- [ ] Profile set **fully private** (before any trade)
- [ ] API key created: scopes **read + trade only**, stored in `.env` on WSL and VM, never pasted anywhere
- [ ] Can the key read before launch? Try `GET /tournaments` → note whether it works
- [ ] Cup `tournamentId` (UUID) and slug found via `GET /tournaments` → `GET /tournaments/{slug}`; added to `settings.yaml`
- [ ] Your `profile_id` found (needed for the `user:{profile_id}` realtime channel)
- [ ] Conflict check: any campaign you or family work/intern/volunteer for → add those markets to a blocklist in config

### Infrastructure
- [ ] VM running, `ssh predcup@SERVER_IP` works, root/password login disabled, firewall on
- [ ] Clock synced (`timedatectl` → synchronized: yes)
- [ ] Repo cloned on VM, `.env` copied via `scp`, `git status` on VM shows `.env` not tracked
- [ ] Telegram bot sends a test message to your phone
- [ ] Telegram bot **ignores commands from any other chat ID**
- [ ] Latency from VM measured: `curl -o /dev/null -s -w "%{time_total}\n" https://www.thesuper.market/api/v1/tournaments` (with auth header), plus Kalshi and Polymarket

### Bot
- [ ] All tests pass on the VM (`pytest`)
- [ ] Full loop run for hours against `mock_exchange` without errors
- [ ] **Kill switch tested end to end** against mock: `/kill` from phone AND `touch KILL` both cancel everything and stop quoting
- [ ] Kill switch confirms via `GET /orders?status=open` (scoped to Cup) and alerts if anything remains
- [ ] Shadow mode toggle works (logs intended quotes, places nothing)
- [ ] Risk limits in `settings.yaml` set to launch values (small size, 3–5 markets only)
- [ ] Net party-exposure limit present
- [ ] Markout logging (1 / 5 / 30 min) present

### Markets (if visible before launch)
- [x] Are Cup markets visible before trading opens? **Yes** — confirmed 2026-09-28, all 237 readable via `GET /markets`, tournament itself still `status: "draft"`. Section B's market-list items done early (above); Info-tab and market-map items still open

---

## B. Launch day, before 17:00 London

### Market list
- [x] Full list of Cup markets saved (id, title, category, exchange ids) — `data/cup_markets.csv`, 237 markets, 2026-09-28. Also parsed: state, office, district, party, race key
- [x] The four chamber-control markets identified — category is **"Freeform"**, not "Other" as assumed earlier: "Will the [Republican/Democratic] Party win the U.S. [Senate/House]?" (ids 151–154)
- [x] Any multi-outcome or composite markets? Note them — none. All 237 are single-exchange binary markets (`isComposite`/`isMultiOutcome` both false, one exchange each)

### Info tab of every market you'll trade (resolution risk)
- [ ] Resolution rules read
- [ ] **Settlement data source** (AP? state officials? other?) → if AP, set up an AP race-call alert source
- [ ] **Per-market close / settlement date** — anything before 4 Nov 12:00 ET?
- [ ] Party definitions (independents caucusing with a party?)
- [ ] **Ranked-choice settlement — Alaska, Maine:** confirm how/when SIG settles once RCV tabulation (which can run well past election night) finishes; don't assume a same-night result for these two states' Senate/House races
- [ ] **Runoffs — Georgia, Louisiana:** confirm SIG's settlement date/rule when no candidate clears a majority on election night and the race goes to a runoff weeks later
- [ ] **Races with a serious independent candidate but no corresponding SIG market** — start with **Michigan Governor** (SIG lists only R/D there; check whether other headline races have the same gap). An omitted independent's real vote share breaks the "R + D ≈ 100" assumption the overround scanner and any R-vs-D consistency check rely on
- [ ] N/A or cancellation conditions

### Market map
- [ ] `config/market_map.csv` filled for launch markets: Kalshi ticker, Polymarket token id, **polarity**, rule differences, confidence
- [ ] **Every row checked by hand** — polarity especially. One inverted row = bot quotes the wrong price confidently

---

## C. At and after 17:00 London — go-live sequence

1. [ ] Books at open: are they seeded by SIG market makers? Note typical spreads and depth in headline vs niche markets
2. [ ] Manual opening trades on obvious mispricings (also secures rank eligibility: ≥1 trade)
3. [ ] Bot in **shadow mode for ~1 hour**: compare intended quotes with the live books; any quote far from market = investigate before going live
4. [ ] Switch to live: **3–5 well-mapped markets, small size**
5. [ ] First reconciliation passes cleanly (local positions = `/tournaments/{slug}/portfolio/positions`)
6. [ ] Scale up only after several clean reconciliations and non-negative markouts

---

## D. Measure live (not in the spec — record what you observe)

| Question | How to find out | Found |
|---|---|---|
| Rate limits | Log rate-limit response headers; note request rate at first 429 | Hit `429 RATE_LIMITED` on `GET /exchanges/{id}/price` with ~237 back-to-back requests and no delay between them (2026-09-28, scripts/scan_race_overround.py). 0.25–0.3 s pacing + exponential backoff on 429 cleared it. Exact requests/sec threshold not measured |
| `Retry-After` present on 429? | Log headers | |
| Minimum / maximum `expirationDate` allowed | Start with 60 s expiry; try shorter only after it works | |
| Does an expired order still show `open: true`? | Query `status=expired` vs `status=open` after an expiry | |
| Expiry emits no realtime event (docs) — confirm | Watch `user:` channel when a quote expires | |
| Position limits exist? Error code? | Any unexpected 4xx on orders → log code and message | |
| `tournament:{id}` channel delivers Cup trades | Compare websocket trades with `GET /exchanges/{id}/trades` | |
| Revision gaps — how often? | Count resyncs in logs | |
| `latestPrice` null until first Cup trade — confirm | Check a quiet market | |
| Which relationships exist between Cup markets | `GET /relationships` and `/relationships/graph` | |
| Violations feed useful / how fast competed away | Subscribe to `relationships:violations:{tournamentId}` | |
| Smart score | `GET /tournaments/{slug}/me/smart-score` | |
| Markouts at 1 / 5 / 30 min | From `events_log` after first fills | |
| Fill rate per market (headline vs niche) | From `events_log` | |

---

## E. Later in October

- [ ] **API key expires 27 Dec 2026** — after trading closes (4 Nov) but rotate/renew before then anyway, in case settlement checks or corrections after close still need it
- [ ] Super Signal appears (after 24 h and once elite traders exist) — how is "strongest traders" defined?
- [ ] Changelog page — does it exist? Save it if so
- [ ] Do new markets get listed mid-competition? (opening-moment opportunities)
- [ ] Leaderboard: what does the top look like after week 1 / 2 / 3?

## F. Election night (3–4 Nov)

- [ ] Does a race stay tradeable between its source calling it and the next 4-hourly settlement run? (observe on the first called race)
- [ ] Do chamber-control markets close to trading before 4 Nov 12:00 ET?
- [ ] Quote-pull rule firing correctly on calls and poll closings
- [ ] Settled payouts arriving in balance before the close (capital recycling)
