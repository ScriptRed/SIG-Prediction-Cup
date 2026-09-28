# Super Market API — Summary for the Predictions Cup bot

Source: API reference pasted from the platform (Scalar docs, v1.0.0, OpenAPI 3.1). Raw text lives in `docs/platform/api_reference.md`. Where this summary and the raw docs disagree, the raw docs win.

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
- **Tick 0.005**, limit prices between **0.005 and 0.995**. Off-tick → 400.
- Market order: omit `price` (or `price: 1` buy / `price: 0` sell). Market orders can't have `expirationDate`.
- `POST /orders` body: `exchangeId`, `side` (yes/no), `action` (buy/sell), `quantity` (positive int), `price`, `tournamentId`, **`idempotencyKey` (required)**, optional `expirationDate`.
- Response: `orderId`, `open`, `remainingQuantity`, `quantityTraded`, `totalCost`, `fillPrice`, `terminalReasonCode`, `all` (capital-efficiency info).
- **Netting:** an opposite-side buy nets against your position at FIFO lot cost. A sell that is flat, opposite, or larger than held is converted to its complement buy (`sell yes q@p ≡ buy no q@(1−p)`).
- **Self-trade prevention:** an order that would cross your own resting order fills others first, then the crossing remainder is cancelled (`SelfTradePrevented`). Quoting both sides is allowed.
- **`expirationDate` on limit orders** → use a short expiry on every quote as a dead-man's switch (if the bot dies, quotes expire by themselves). Note: expiry emits no realtime event — track it yourself.

## Order endpoints

| Endpoint | Use |
|---|---|
| `POST /orders` | single order |
| `POST /orders/batch` | up to 50, each independent, 207 on partial success; may 503 partway — retry with same key to resume |
| `POST /orders/multi-leg` | up to 10, **atomic** (all or nothing) — use for two-sided quotes |
| `POST /orders/cancel-all` | scoped by `exchangeId` or `marketId` — preferred way to re-quote |
| `GET /orders`, `GET /orders/{id}`, `DELETE /orders/{id}`, `GET /orders/{id}/fills` | inspect / cancel |

**Re-quote pattern (from docs):** cancel-all scoped to the exchange → only after a **200** re-post quotes. On 503: retry cancel-all with backoff. On 207/422: confirm via `GET /orders?status=open` (same scope) that nothing remains before re-posting, so quotes never stack.

## Retry and error rules

- Retry with exponential backoff + jitter: `429 RATE_LIMITED`, `503 TX_CONFLICT`, `503 SERVICE_UNAVAILABLE`.
- `409 REQUEST_IN_FLIGHT`: same idempotency key still executing — wait ~90 s, retry identical payload + same key.
- **`502 ORDER_STATUS_UNKNOWN`: the order may have gone through.** Check positions or retry the identical request with the same key (safe, won't double-place). Never retry with a new key.
- Reusing a key with a different payload → 409 CONFLICT. One key per logical order.
- Do **not** auto-retry other 4xx (bad input, insufficient balance, scope, membership).
- **Rate limits are per key; numbers are not published.** Measure carefully, keep a margin, avoid tight polling.

## Market data

- `GET /markets` (limit ≤100, `tournamentId`, `search`, `status`, `category=election-outcome|freeform…`, `ids`)
- `GET /markets/{id}/orderbook?depth=` (≤200) — includes **`overround`** and **`hasArbitrageOpportunity`** (useful for multi-outcome markets)
- `GET /exchanges/{id}/price` — latest, best bid, best ask, spread
- `GET /exchanges/{id}/orderbook`, `GET /exchanges/{id}/price-history` (candles), `GET /exchanges/{id}/trades` (trade tape), `GET /exchanges/prices` (bulk snapshot), `GET /exchanges?ids=` (≤100)
- Pagination: `pagination.hasMore` + opaque `nextCursor` (limit typically ≤200).

## Portfolio (important trap)

- `/portfolio/positions`, `/pnl`, `/history`, `/settlements`, `/transactions` **always report the org's default tournament** and ignore `tournamentId`.
- **For the Cup use `/tournaments/{slug}/portfolio/positions|pnl|fills|history|settlements|transactions`.** Only `/portfolio/collateral` and `/portfolio/fills` accept `tournamentId`.
- Positions include `quantity`, `avgCost`, `currentPrice`, `unrealizedPnl` and FIFO `lots`.

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

- The engine keeps a graph of logical links between markets (monotonic, complementary, mutually exclusive, implication, boolean, conditional) and evaluates price constraints: `GET /relationships`, `/relationships/graph`, `/relationships/constraints` (with **suggested corrective trades**).
- **Capital efficiency:** related positions can need less collateral (`all.collateralSavings`, `guaranteedPayoutFloor`, `outstandingAdvance`). Relevant for sizing baskets — investigate which relationships exist in the Cup.
- Implication: simple logical arbitrage between linked markets is flagged publicly to everyone → expect it to be competed away fast. Treat the violations feed as a free alert source, not a private edge.

## Leaderboard and scoring hints

- `GET /leaderboards?tournamentSlug=&period=all&sort=pnl` — ranked by P&L, ties share a rank (1-1-3). Also `/tournaments/{slug}/leaderboard`.
- `GET /tournaments/{slug}/me/smart-score` — our own "smart score"; likely related to elite status for the Super Signal. Worth tracking.

## What the admin (DMM) endpoints reveal (we can't call them)

- SIG **seeds tournament liquidity** (p2p mode = order book seeded with maker quotes) and tracks market-maker inventory/exposure.
- SIG has **per-trader analytics**, an **elite cohort summary**, rankings, and an **iCIMS export of qualified candidates** (recruiting pipeline).
- SIG can **preview and execute settlement corrections** after the fact.

## Still unknown

- Actual rate-limit numbers.
- Whether Cup markets keep trading overnight on election night.
- Full details for price-history, trades, batch, multi-leg, cancel-all body, relationships/constraints, tournament and leaderboard endpoints (pasted docs cut off at Models) — get the OpenAPI JSON.
- Which relationships exist between Cup markets.
