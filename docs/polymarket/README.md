# Polymarket API docs (read-only use)

Saved 2026-10-01 from https://docs.polymarket.com (robots.txt allows all but
`/cdn-cgi/` and `/_next/`; content signal `ai-input=yes`). Polymarket is
market data only for us (CLAUDE.md Hard Rule 5): we use public GET endpoints,
no auth, no orders.

| File | Source |
| - | - |
| `gamma-openapi.yaml` | `/api-spec/gamma-openapi.yaml` — Gamma (markets, events, search). Source of truth for shapes. |
| `clob-openapi.yaml` | `/api-spec/clob-openapi.yaml` — CLOB. We use only `GET /book` (documented `GET /books` answered 400 live on 2026-10-01). |
| `rate_limits.md` | `/api-reference/rate-limits.md` — Cloudflare IP limits (throttled, not rejected). |
| `market-data_*.md`, `concepts_*.md` | prose guides (outcome/token layout, prices, order books, neg risk) |
| `api_reference_index.md`, `getting-started_api.md`, `changelog_predictions.md` | index, base URLs, changelog |

Facts `predcup/venues/polymarket.py` relies on, and where they come from:

- Gamma `outcomes`, `outcomePrices`, `clobTokenIds` are **JSON-encoded strings**
  (spec: `type: string`; market-data_market-details.md "Market Outcomes"),
  correlated by index; "Index `0` describes the YES outcome and index `1`
  describes the NO outcome". We also require the labels to be exactly
  `["Yes", "No"]` and refuse anything else.
- Prices are dollars per share in [0, 1]; a share pays $1
  (concepts_prices-orderbook.md, concepts_positions-tokens.md). Book prices
  and sizes are decimal strings (`OrderSummary`).
- **Conflict:** `clob-openapi.yaml` says bids are sorted descending and asks
  ascending; market-data_prices-order-books.md says bids ascending and asks
  descending (best = last). The adapter takes max(bid) / min(ask) and never
  relies on order.
- `OrderBookSummary.timestamp` units are not documented (example
  `'1234567890'`); we use our own fetch time for staleness.
- Rate limits (per IP, 10 s windows): Gamma `/markets` 300, `/public-search`
  350; CLOB `/book` 1500, `/books` 500.
