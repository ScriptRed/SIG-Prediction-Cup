# Susquehanna Predictions Cup — Project Brief

Reference document for all planning chats. The repo is the source of truth; keep this file in `docs/` and re-upload to the Claude project when it changes.

## 1. The competition

- **What:** Free simulated prediction-market trading competition for university students, run by Susquehanna International Group (SIG). Platform operated by The Super Market (not affiliated with SIG). Site: https://sig.thesuper.market/
- **Markets:** 2026 U.S. midterm elections (Senate/House control, individual Senate and House races, some "Freeform" markets). No mention markets. SIG chooses markets; the number may change during the competition.
- **Dates:** Opens 12:00 ET, Thursday 1 October 2026. Trading closes 12:00 ET, Wednesday 4 November 2026 (trades must be received before this). Final rankings are confirmed only after **all** markets resolve (runoffs/slow counts can delay this by weeks).
- **Prizes:** Top 3 final SUSQies balances win $30,000 / $5,000 / $2,500. No tiebreakers: tied players split the combined prizes for their positions equally. Non-U.S. winners: 30% withholding. Winners must complete eligibility forms within 72 hours of notice, then provide ID, student ID and tax forms within 5 days.

## 2. Rules that matter for strategy and the bot

- 100,000 SUSQies on registration (also for late joiners). No cash value, cannot be bought or transferred.
- **One account per person. Participation is strictly individual, not team-based.** No pooling, no coordinated trading with other accounts, no transferring value between accounts. SIG can disqualify for unsportsmanlike conduct at its sole discretion and can void trades.
- **Bots are allowed:** unlimited bots on your one account via the API, subject to rate limits and position limits.
- Do not trade any market tied to a campaign you (or family) work, intern or volunteer for.
- SIG may act as market maker in any market (no obligation to provide liquidity).
- SIG may change a market's rules or resolution at any time, even after resolution. Each market's **Info** tab holds its resolution rules and data source.
- SIG owns all platform data (trades, bot activity).

## 3. Mechanics (from platform guide)

- Each market has a YES and a NO side. A winning share pays **1 SUSQie**; prices run 0–1.
- Central limit order book, price-time priority. You cannot choose your counterparty.
- **Netting:** buying the opposite side of a position you hold cancels share-for-share and pays 1 SUSQie per cancelled pair immediately. Buying NO at q while holding YES ≡ selling YES at 1 − q. Closing needs little or no balance.
- Leaderboard ranks by account value in real time; final score = balance after all markets resolve (so pushing prices at the close does not change final score).
- **Super Signal:** nightly snapshot of the positions of the competition's strongest traders, shown per market (enhanced price vs market price, elite count, 24h order flow, conviction, participation). Unavailable for markets <24h old, low volume, or before elite traders exist.

## 4. Still unknown — must confirm from docs

- **Answered by the API docs (see `docs/platform/SUMMARY.md`):** Bearer-key auth, websocket via Supabase Realtime (250 ms batches), tick 0.005, limit orders with optional expiry, batch and atomic multi-leg orders, one YES-normalized book per binary market (no YES/NO book arb), engine-monitored cross-market "ALL" relationships with public violation alerts and collateral savings for related positions.
- Actual rate-limit numbers (per key, unpublished).
- Whether markets keep trading overnight on election night (3–4 Nov) or halt at poll close.
- Settlement & Payouts, Portfolio & Standings, and Changelog guide pages.
- How "strongest traders" is defined for the Super Signal.
- Per-market resolution details: party definitions (independents), runoffs, ranked-choice counting, 50–50 Senate handling.

## 5. Core strategic insight

Only the top 3 of (likely) thousands are paid. Maximising expected SUSQies is not the goal; maximising the probability of a top-3 finish is. Steady fair-value trading finishes mid-table. Plan:

- **Phase 1 (October): grow the bankroll with low-risk edge.** Never bust early — the bankroll cannot be replenished.
- **Phase 2 (final days): one concentrated, correlated scenario bet** (a basket of same-direction underdogs betting on the direction of polling error), held to resolution. Size depends on leaderboard position.
- **Phase 3 (election night, if markets stay open):** trade live results faster than the market.

Holding rule: hold while the edge lasts; exit when price reaches fair value (Phase 1). Phase 2 is held to resolution deliberately for variance.

## 6. Strategies ranked (effectiveness for us / how common among competitors)

| Rank | Strategy | When | Common? |
|---|---|---|---|
| 1 | Concentrated polling-error basket, held to resolution | Final days | Medium (few do it coherently) |
| 2 | Election-night trading on live county results (if open) | 3–4 Nov | Low–medium |
| 3 | Selling overpriced longshots to top-3 chasers | All month | Low |
| 4 | Opening moments: unseeded books, first-day mistakes, new listings | 1 Oct, new markets | Low–medium |
| 5 | Cross-market consistency: chamber control vs races, Dem/Rep pairs, YES+NO | All month | Low–medium |
| 6 | Anchoring to Kalshi / Polymarket / forecast models | All month | Very high (edge decays fast) |
| 7 | Bot market making in thin / unseeded / low-attention markets | All month | Low |
| 8 | Resolution fine-print differences | Before entering | Low |
| 9 | Selling to trailing players' late desperation bets | Final week | Low |
| 10 | Sniping stale orders after news / outside moves | News events | Low (but racing SIG and bots) |
| 11 | Deep "stink" orders to catch big market orders | All month | Low |
| 12 | Super Signal: anticipate or fade the copiers after refresh | Mid–late Oct | Medium to view, low to exploit |
| 13 | Fading overreactions to single polls | Poll releases | Low |
| 14 | Cheap favourites (93–97 that are ~99%) for idle capital | Any time | Medium |

Only #1 and #2 can win on their own; #3–#14 build bankroll for them. Specialise in markets others ignore.

## 7. Guardrails

- Per-market and total exposure caps; daily loss stop; kill switch.
- Pull or re-price resting orders before scheduled events and when outside prices move.
- Exit when edge is gone (Phase 1).
- Run the bot on a cloud VM, not a laptop.
- Read every market's Info tab before trading it.
- Check the leaderboard before sizing Phase 2.
- Stay strictly individual; never coordinate with other accounts.

## 8. Expected behaviour of the field

Most registrants go inactive quickly; the real competition is a few hundred engaged players. Active players mostly: trade headline markets, bet their politics (student field may skew one way — check against Kalshi), cross the spread with market orders, copy Kalshi/Polymarket by hand, copy the Super Signal, and swing at longshots late if behind. Median active player finishes slightly under 100,000. Top will be bot-running CS/quant students, ex-SIG interns (eligible), and lucky high-variance players. Our aim: be in the first group so we have the biggest bankroll when we make the bet the third group makes.

## 9. Real-market evidence (Kalshi / Polymarket)

Profits are extremely concentrated in a tiny share of accounts, mostly automated: arbitrage bots, market makers, high-frequency systems. Steady earners win on market mechanics; the big political wins came from concentrated, well-researched conviction (e.g. the 2024 "Théo" bet, backed by commissioned polling). Copy-trading Polymarket wallets is low value (Kalshi is anonymous; top wallets are mostly hedged market makers; copy lag). Use big known-wallet moves only as alerts.

## 10. Decisions log

- Python stack, Claude Code for building, claude.ai project for planning. Gemini not used for now.
- Develop in WSL (matches the Linux production server); Claude Code installed inside WSL.
- Platform behind a single venue adapter so everything else can be built before API access.
- LLMs never place orders; they classify, map and alert only.
- Copy-trading deprioritised; kept as alert feature only.

(Append new decisions here with dates.)
