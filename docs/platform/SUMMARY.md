# Super Market API — Summary for the Predictions Cup bot

Source: `docs/platform/openapi.json` (OpenAPI 3.1, "Super Market API 1.0.0", 80 paths) is the authoritative spec — this summary is reconciled against it. Prose guide pages live in `docs/platform/guide.md`. Where this summary and the raw spec disagree, the raw spec wins; every claim below either cites a path/schema or is marked "not in spec".

## Basics

- **Base URL:** `https://www.thesuper.market/api/v1`
- **Auth:** `Authorization: Bearer <key>` (or `X-API-Key: <key>`). Create in Settings → API Keys. Request scopes **read + trade only** (admin is useless to us). Key is shown once — store in `.env`.
- Our key will be **organization-bound** (SIG org). Omitting `tournamentId` falls back to the org's *default* tournament, which may not be the Predictions Cup. **Always pass the Cup's `tournamentId` explicitly on every read and write.**
- Find it: `GET /tournaments` (lists accessible tournaments) → `GET /tournaments/{slug}` → use its `id` (UUID) as `tournamentId`.
- All JSON; timestamps ISO-8601 UTC. Errors: `{ "error": { "code", "message", "details"? } }` — branch on `code`.

## Data model

- **Market** = container. **Exchange** = the tradable contract; every order/trade/position uses an `exchangeId`.
- Binary market = 1 exchange (YES/NO). Multi-outcome = N mutually exclusive exchanges. Composite markets have AND/OR/NOT/IF node trees (`GET /markets/{id}/nodes`).
- Each tournament has an **isolated order book and balance**. In tournament context `latestPrice` is null until a tournament trade happens.

## Prices and orders

- All prices are **YES-normalized in [0, 1]**. For NO, effective cost is `1 − price`. Order books are YES-perspective → effectively **one book per binary market** (no separate YES/NO book arbitrage).
- **Tick 0.005**, limit prices between **0.005 and 0.995**. Off-tick → 400 `VALIDATION_ERROR`. (`OrderInput` schema, `#/components/schemas/OrderInput`.)
- **`quantity`**: positive integer, max `2147483647` (engine holds it as signed 32-bit) — larger values are rejected with `VALIDATION_ERROR`. (`OrderInput.quantity`.)
- Market order: omit `price` (or `price: 1` buy / `price: 0` sell). Market orders can't have `expirationDate` (`OrderInput.expirationDate` description: "Not supported for market orders").
- **`expirationDate`**: optional ISO-8601 datetime, must be in the future; format/bound is just "future" — **no explicit max duration is given in the spec** (not in spec — treat our own short-expiry policy as a house rule, not a platform limit).
- `POST /orders` body (`SingleOrderInput` = `OrderInput` + required `idempotencyKey`): `exchangeId` (numeric string), `side` (yes/no), `action` (buy/sell), `quantity`, `price`, `tournamentId`, **`idempotencyKey`** (string, 1–255 chars), optional `expirationDate`.
  - **Tournament defaulting trap applies here too:** an org-bound key that omits `tournamentId` defaults to the *org's default tournament*, not necessarily the Cup. Always pass the Cup's `tournamentId` explicitly (per Hard Rule).
- Response fields per spec description (no dedicated named response schema found for single order — same shape used across order endpoints): `orderId`, `open`, `remainingQuantity`, `quantityTraded`, `totalCost`, `fillPrice`, `terminalReasonCode`, `all` (nullable capital-efficiency info).
- `GET /orders` (`Order` schema): `id`, `exchangeId`, `side`, `action`, `quantity` (always positive — executable remainder while `open`, filled+remainder once closed), `priceLimit`, `open`, `createdAt`, `expirationDate`. `status` filter: `open` (default, resting & not expired) / `closed` (filled, cancelled, or expired) / `expired` (legacy rows still stored `open` but past `expirationDate`, plus engine cancels with `terminalReasonCode: "Expired"`) / `all`.
  - **Important:** `open: true` does **not** guarantee the order hasn't passed its `expirationDate` — the spec explicitly warns "`open: true` yet be past its `expirationDate`; use `status=expired` to query that state." Our expiry tracking (dead-man's switch) must not rely on `open` alone; poll with `status=expired` or track expiry locally.
- **Netting:** an opposite-side buy nets against your position at FIFO lot cost (`avgCost`/`lots` on the position read show the survivor). A sell that is flat, opposite, or larger than held is canonicalized by the engine into its complement buy (`sell yes q@p ≡ buy no q@(1−p)`); the response's canonical `action`/`side`/`price`/`quantity` reflect this.
- **Self-trade prevention:** an order that would cross your own resting order fills other counterparties first, then the crossing remainder is cancelled (`terminalReasonCode: "SelfTradePrevented"`). Quoting both sides is allowed.
- **`expirationDate` on limit orders** → use a short expiry on every quote as a dead-man's switch (if the bot dies, quotes expire by themselves). Expiry emits **no realtime event** — track it yourself (confirmed: no websocket event for expiry in the realtime section of the spec).
- **Consent/terms gate:** any order call can return `403 TERMS_NOT_ACKNOWLEDGED` with `error.details.missingDocuments` (each has `documentId`, `kind`, `version`, `organizationId`, `organizationSlug`, `organizationName`, `supportEmail`, `currencyName`, `url`, `host`) if a required platform document hasn't been accepted. Not previously documented — worth a startup check / alert if hit live.

## Order endpoints

| Endpoint | Use |
|---|---|
| `POST /orders` | single order (`SingleOrderInput`) |
| `POST /orders/batch` | up to 50 orders (`BatchOrderInput`, `orders[]` maxItems 50), each independent — **not atomic**, partial success is normal; see below |
| `POST /orders/multi-leg` | up to 10 legs (`legs[]` maxItems 10), **atomic** (all-or-nothing) — use for two-sided quotes; optional `relationshipConstraint` (UUID of an active ALL relationship) validates all legs against it pre-dispatch |
| `POST /orders/cancel-all` | scope by `exchangeId` **or** `marketId` (mutually exclusive, 400 if both) plus optional `tournamentId` — preferred way to re-quote |
| `GET /orders`, `GET /orders/{id}`, `DELETE /orders/{id}`, `GET /orders/{id}/fills` | inspect / cancel |

**`/orders/batch` semantics (spec: description on `POST /orders/batch`):**
- All orders validated upfront; if any fails validation the **whole batch** is rejected 400 before anything is placed. Past that gate, execution is best-effort per item — a failure does not roll back placed orders.
- Orders are placed one at a time under a bounded execution time budget; a batch that can't finish in budget stops early and returns **503**, keeping everything already placed — retry the same key to resume from where it stopped (repeatable until a final 200/207/422).
- Response codes: `200` all succeeded · `207` partial (mix of success/fail) · `422` all failed terminally · `429` every item rate-limited (nothing placed) · `502` every item's dispatch outcome unknown (`ORDER_STATUS_UNKNOWN` per item) · `503` no item succeeded and a transient/mixed outcome remains, or budget ran out mid-batch (**503 here does not mean nothing executed** — earlier items in the batch may already be live).
- `Retry-After` header carries the *longest* wait any rate-limited item reported; may be absent. Reuse the same idempotency key to resume a 503/retry a 429; after a completed 207, resubmit only the failed items under a **new** key.
- Use `/orders/multi-leg` instead when you need all-or-nothing.

**`/orders/multi-leg` semantics:** all legs succeed or none persist (true rollback on any leg failure). `relationshipConstraint` is validated against a pre-dispatch engine price snapshot — not held under the engine's execution lock, so a concurrent fill/graph change can still produce a violation after the fact; watch `GET /relationships/constraints` and violation broadcasts for that. Duplicate legs on the same exchange in the same tournament scope → 400 before any engine state is touched. Price validation is in YES terms (NO legs use YES-complement price); exhaustive mutually-exclusive relationships require `sum(P) = 1`, non-exhaustive require `sum(P) ≤ 1` — violation → 422 `RELATIONSHIP_VIOLATION`; engine/price-read unavailable → fails closed with 503. Leg-level errors include `details.leg` (zero-based failed leg index).

**`/orders/cancel-all` semantics:**
- No body → org-bound key cancels the **org's default tournament** (not necessarily the Cup!); unbound key cancels public-global orders. **Always pass `tournamentId` explicitly** even when also scoping by `exchangeId`/`marketId` — the same org-default trap that applies to reads applies here too.
- `{exchangeId}` or `{marketId}` narrow within whatever tournament context is resolved; the two are mutually exclusive (400 if both given). Combine with `tournamentId` to be explicit.
- An order already filled/cancelled by the time the sweep reaches it counts as cancelled (no error).
- Responses: `200` all cancelled · `207` partial failure · `422` all cancellations failed · **`503` `CancelAllPausedError`**: the trading engine paused the sweep — `error.details.cancelled` (int, cancelled so far) and `error.details.remaining` (int ≥1, unconfirmed) tell you how far it got; retry the *same* request to finish (a retry re-reads open orders, so already-gone orders count as done).

**Re-quote pattern (confirmed against spec):** `POST /orders/cancel-all` scoped to `{exchangeId, tournamentId}` → only after a **200** re-post quotes. On `503 CancelAllPausedError`: retry the identical cancel-all (it resumes, doesn't restart). On 207/422: confirm via `GET /orders?status=open` (same scope) that nothing remains before re-posting, so quotes never stack.

## Retry and error rules

- Retry with exponential backoff + jitter: `429 RATE_LIMITED`, `503 SERVICE_UNAVAILABLE` (includes `CancelAllPausedError` and batch-budget 503s), `409 REQUEST_IN_FLIGHT`.
- `409 REQUEST_IN_FLIGHT`: same idempotency key still executing — retry identical payload + same key with backoff (spec doesn't give a fixed wait; ~90 s was our own earlier estimate, treat as a house default not a spec fact — **not in spec**).
- **`502 ORDER_STATUS_UNKNOWN`: the order may have gone through.** Check positions or retry the identical request with the same key (safe, won't double-place — the engine replays the stored response for a matching resolved payload). Never retry with a new key.
- Reusing a key with a different resolved payload → `409 CONFLICT`. One key per logical order/batch/multi-leg call. Reusing a key from a different organization also conflicts.
- Do **not** auto-retry other 4xx (bad input, insufficient balance, scope, membership, `RELATIONSHIP_VIOLATION`).
- **Numeric rate-limit values are not in the spec** — no `x-ratelimit-*` headers or published per-key numbers anywhere in `openapi.json`. Only the dynamic `Retry-After` header on 429/503/207 responses (when a rate-limited item supplied one) is documented — always honor it when present; otherwise use our own conservative backoff with margin. Confirmed still unknown, not just undocumented in the old pasted guide.

## Market data

- `GET /markets` (limit ≤100, `tournamentId`, `search`, `status`, `category=election-outcome|freeform…`, `ids`)
- `GET /markets/{id}/orderbook?depth=` (≤200) — includes **`overround`** and **`hasArbitrageOpportunity`** (useful for multi-outcome markets)
- `GET /exchanges/{id}/price` — latest, best bid, best ask, spread
- `GET /exchanges/{id}/orderbook`, `GET /exchanges/prices` (bulk snapshot), `GET /exchanges?ids=` (≤100)
- **`GET /exchanges/{id}/trades`** — newest-first, cursor-paginated trade tape, YES-normalized (directly comparable to `/price` and `/price-history`). Params: `tournamentId` (same org-default trap as everywhere else — omit and an org-bound key gets the org default tournament, not the Cup), `from`/`to` (ISO-8601, `from` inclusive / `to` exclusive, default full history → now), `limit` (1–200, default 50), `cursor`.
- **`GET /exchanges/{id}/price-history`** — OHLCV candles computed from trade history, YES-normalized, **sparse** (buckets with no trades are omitted — don't assume a candle exists for every period). Params: `tournamentId`, `resolution` (`1m`/`5m`/`1h`/`1d`/`1w`, default `1h` — buckets align to UTC multiples of the resolution from the Unix epoch, so **`1w` buckets start on Thursdays**, not Mondays/Sundays), `from` (floored to bucket boundary), `to` (exclusive, default now), `limit` (1–1000, default 200). Without `from`: newest `limit` candles ending at `to`. With `from`: oldest `limit` candles forward from it; if the window exceeds the cap, response `coverage.complete` is `false` and `to` becomes the next candle's start (exclusive continuation boundary) — page forward using that.
- Market schema (`Market`): `status` enum `open|closed|settled`, `settlementDate`, `settledWith`, `settledOn`. **No intraday trading-hours/halt-schedule field exists anywhere in the spec** — only tournament-level `startDate`/`endDate` (`TournamentSummary`) bound the whole competition. Whether Cup markets trade overnight on election night is genuinely **not in spec** — must ask SIG directly.
- Pagination: `pagination.hasMore` + opaque `nextCursor` (limit typically ≤200), except trades/price-history which use their own `from`/`to`/`cursor`/`limit` params above.

## Market resolution rules (Info tab) — recorded 2026-09-30

From each Cup market's **Info** tab on the SIG site; the same template is used across markets. Not in the API: the `Market` schema has no rules or description field (the `show_mapping` script prints "rules: NONE" for every market).

- **Data source:** official election results from election authorities. **Resolution is on final certified results**; recounts or legal challenges delay it. So races almost certainly **do not settle on election night**, and called races probably stay tradeable until the 4 Nov 12:00 ET close (to be confirmed by watching the first AP call — `docs/LAUNCH_CHECKLIST.md` §F). AP race calls are the fastest *signal*, not the settlement source.
- **Fusion:** a fusion candidate counts for **every party on the ticket**, so more than one party's market in a race can resolve YES. R and D are **not guaranteed complements**: the parity/overround scanner skips races flagged `fusion_risk=true` in `config/market_map.csv`, and `risk.py` keeps those races off the net R-vs-D axis.
- **Party:** wins count by **party affiliation, not caucusing** (an independent who caucuses with a party does not count for that party).
- Market `settlementDate` from the API reads 2026-11-04T17:00Z (= 12:00 ET, the trading close) on the markets checked so far (MA). Our reading, not stated by SIG: with certified-results resolution it marks the trading close, not when payouts happen.

## Portfolio (important trap)

- `/portfolio/positions`, `/pnl`, `/history`, `/settlements`, `/transactions` **always report the org's default tournament** and ignore `tournamentId`.
- **For the Cup use `/tournaments/{slug}/portfolio/positions|pnl|fills|history|settlements|transactions`.** Only `/portfolio/collateral` and `/portfolio/fills` accept `tournamentId`.
- `Position` schema fields: `exchangeId`, `marketId`, `marketTitle`, `option`, `settled`, `quantity` (positive = YES shares, **negative = NO shares** — sign encodes side, not always positive), `avgCost`, `currentPrice` (global price for unbound keys, tournament valuation price for org-bound keys; null only for zero-quantity tournament rows), `marketValue`, `costBasis`, `unrealizedPnl`, `unrealizedPnlPct`, `moneyEarned` (realized cash from prior closes), and FIFO `lots`.
- **No per-market or per-account position-limit / max-exposure field exists anywhere in the spec** (`positionLimit`, `maxPosition`, exposure caps — none of these appear). Confirms position limits mentioned in the platform guide/marketing copy are either enforced silently server-side with no queryable field, or don't exist as a hard technical cap — our own `risk.py` caps are the only enforcement we can rely on, not a platform-reported number.
- `GET /portfolio/collateral` (`PortfolioCollateral`): per capital-efficiency component — `outstandingAdvance`, `componentId`, `coveredExchangeIds`, `relationshipIds`, `guaranteedPayoutFloor` (min payout when engine can evaluate every joint outcome; null if unavailable), `diagnostic`. Useful for sizing Phase 2 baskets against related markets.

## Realtime (websocket)

- `POST /realtime/token` → `{ token, expiresAt, supabaseUrl, anonKey, channels.user }`. Token lasts **3 h** — refresh before expiry. Python: async `supabase-py` (`acreate_client`, `realtime.set_auth(token)`), channels with `{"config": {"private": True}}`.
- Events arrive in **250 ms batches**. Best-effort, no replay. Every batch has `delivery.revision` / `previousRevision`: on a gap, reconnect, token refresh or socket error → **resync from REST before acting**.
- Channels for us:
  - **`tournament:{tournament_id}`** → `market_batch` for Cup markets. **Cup trades are not mirrored to `market:*` channels — subscribe here.**
  - `user:{profile_id}` → `account_batch`: fills, orderUpdates, settlements, refunds, collateralChanges.
  - `relationships:violations:{tournamentId}` → cross-market constraint violations with suggested corrective trades.
  - `leaderboard:all` → tick (a few seconds' lag); refetch leaderboard.
- `market_batch`: `trades[]`, `bookDirty[]` (refetch that book), `marketSettled[]` (`settledWith` = winning label or `REFUND`).

## Relationships ("ALL" — Advanced Logic Linking)

- The engine keeps a graph of logical links between markets (monotonic, complementary, mutually exclusive, implication, boolean, conditional) and evaluates price constraints.
- `GET /relationships` — active canonical relationships enriched with market titles/outcome labels; filter by `marketId` **or** `exchangeId` (mutually exclusive), `type`, `depth` (1–4, default 1), `limit` (≤200, default 100), `cursor`, `tournamentId` (same org-default trap).
- `GET /relationships/graph` — traverses the graph outward from every exchange in one **required** `marketId`, returns deduplicated relationships + labeled exchange nodes; `depth` (1–4, default 2), `tournamentId`.
- `GET /relationships/constraints` (`AllRelationshipConstraint`) — engine-computed evaluation per relationship: `evaluationStatus` (`satisfied|violated|unevaluable`), `violationAmount` (distance from valid bound, 0 if satisfied/unevaluable), `bound`/`lowerBound`/`upperBound`/`currentValue`, `direction`, `constraint` semantics, `diagnostic`, and **`suggestedCorrectiveTrades[]`** (`AllRelationshipCorrectiveTrade`: `exchangeId`, `outcomeSide`, `action`, `rationale`, `marketId`/`marketTitle`/`outcome`/`currentPrice`). Filters: `marketId`, `relationshipId`, `violationsOnly` (bool), `minViolation` (0–1, default 0.01), `tournamentId`.
- **Capital efficiency:** related positions can need less collateral — see `GET /portfolio/collateral` above (`outstandingAdvance`, `guaranteedPayoutFloor`). Relevant for sizing baskets — investigate which relationships exist in the Cup once markets are live.
- Implication: simple logical arbitrage between linked markets is flagged publicly to everyone → expect it to be competed away fast. Treat the violations feed as a free alert source, not a private edge.

## Leaderboard and scoring hints

- `GET /leaderboards` — ranked by P&L; standard competition ranking (ties share a rank, next position skips by tie size, e.g. 1-1-3). Params: `tournamentSlug` (omit → org-bound key uses org default tournament, unbound key ranks global trading with `tournament_id IS NULL`), `groupId` (requires explicit tournament or org-bound key), `period` (`1d`/`7d`/`30d`/`quarter`/`all`, default `all`, **calendar-aligned UTC**: `1d`=today since 00:00 UTC, `7d`=this week since most recent Sunday 00:00 UTC, `30d`=this month since the 1st, `quarter`=current calendar quarter), `sort` (`pnl|roi|winRate|volume|trades`, only honored for explicit/org-defaulted tournament leaderboards — global path always sorts by P&L and rejects `sort`/`groupId` with 400), `limit` (≤100, default 50), `offset`.
- `GET /tournaments/{slug}/leaderboard` — members ranked by P&L within the tournament; `myRank` computed against the *same period* as the list (period P&L for `1d/7d/30d`, current portfolio value for `all`; `null` if caller hasn't traded in the period). Baselines come from a **daily snapshot taken at 00:00 UTC**. Extra param `season` (archived key like `2026-Q1` or `current`, global-tournament-only, requires `period=all`) — archived entries use `finalTotalValue`/`finalBalance` instead of `pnl`.
- `GET /tournaments/{slug}/me/smart-score` — one row per `marketType` the caller has been scored in; Smart Score is a **time-decayed composite of PnL, ROI, win rate, and Sharpe-scaled volatility**; `isElite=true` = in the latest elite cut for that marketType (this is almost certainly the mechanism behind Super Signal's "elite" cohort — first concrete confirmation of how "strongest traders" is defined). Empty array if no metrics snapshot yet.
- `GET /tournaments` — all tournaments the caller can access, including open org tournaments before first-action enrolment; filter `status` (`draft|active|ended|any`), `marketId`, `limit`/`offset`.
- `GET /tournaments/{slug}` (`TournamentSummary`) — `id` (the UUID to use as `tournamentId` everywhere else), `slug`, `name`, `description`, `status`, `startDate`, `endDate`, `initialBalance`, `currencyName`, `myBalance`, `joinedAt`, `isPendingEnrolment`. 403 if caller isn't a member unless the tournament is open or caller owns/admins the org.
- `GET /tournaments/{slug}/seasons` — current season + archived snapshots; **global tournaments only**, 404 otherwise (not applicable to the Cup, which is presumably a single-season tournament — confirm `status` via `GET /tournaments/{slug}`).

## What the admin (DMM) endpoints reveal (we can't call them)

- SIG **seeds tournament liquidity** (p2p mode = order book seeded with maker quotes) and tracks market-maker inventory/exposure.
- SIG has **per-trader analytics**, an **elite cohort summary**, rankings, and an **iCIMS export of qualified candidates** (recruiting pipeline).
- SIG can **preview and execute settlement corrections** after the fact, and reset tournament balances.

## Still unknown (confirmed absent from `openapi.json`, not just undocumented)

- **Actual rate-limit numbers** — no numeric values or `x-ratelimit-*` headers anywhere in the spec; only the dynamic `Retry-After` header on affected responses.
- **Settlement data source** — answered 2026-09-30 from the Info tabs: official certified results (see "Market resolution rules" above), not AP.
- **Whether Cup markets keep trading overnight on election night** — no trading-hours/halt-schedule field exists in `Market` or `Exchange` schemas; only tournament-level `startDate`/`endDate` bound the whole competition. Must ask SIG.
- **Position limits** (per-market or per-account) — no `positionLimit`/`maxPosition`/exposure-cap field anywhere in the spec. Either platform-enforced with no queryable field, or a soft rule — our `risk.py` limits are the only enforcement we control.
- Which relationships exist between Cup markets specifically — only knowable once markets are live, via `GET /relationships?marketId=`.
- Whether the Cup tournament is a "global tournament" (seasons-eligible) or a fixed single-season one — check `GET /tournaments/{slug}/seasons` once we have the slug; 404 there would confirm single-season.
