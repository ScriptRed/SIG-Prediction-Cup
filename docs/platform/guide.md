# Platform Guide (sig.thesuper.market/docs)

Pasted from the platform's user guide on 2026-09-28. Verbatim except for headings. The Changelog page was not included in the paste.

---

## Overview

The Super Market is a prediction market platform where you can trade markets on elections, economics, and politics. Trades use platform SUSQies instead of real-world currency, so there is no financial risk to play. Each participant begins the Midterm Elections competition with a balance of 100,000 SUSQies. This guide covers finding and trading markets, understanding settlement and payouts, your portfolio, standings, and account settings. If you use the API, see the [API Reference](https://sig.thesuper.market/api/v1/docs).

The current competition is Midterm Elections, running October 1, 2026 at noon Eastern through November 4, 2026 at noon Eastern. Trading is locked until it opens. See [The Predictions Cup Series](https://sig.thesuper.market/docs/tournaments-and-organizations) for competition results, prizes and ties.

---

## Markets & Trading

Markets turn questions into YES and NO sides that you can trade with platform SUSQies.

### Find a market

- Search the Markets page by title. Use category filters to narrow the subject. Use the status filter to show open, resolved, or all markets.
- Signed-in users can filter for markets they follow or markets where they have a position. Open markets accept trades. Resolved markets show the final outcome and trading history.

### Understand shares

- Each market has two sides: YES and NO. Buying or selling YES or NO are all ways of taking a side in the same market.
- A YES share wins if the market resolves YES. A NO share wins if the market resolves NO. Prices run from 0 to 1 SUSQie per share and represent probabilities. A YES price of 0.68 represents a 68% probability, while the NO price is its complement, 0.32. A winning share pays 1 SUSQie at settlement.

### Choose an order

- Choose YES or NO, then choose whether to buy or sell. The trade panel previews the shares, estimated cost or proceeds, and potential payout.
- A market order tries to trade immediately at available prices. Its preview accounts for price impact as it moves through available liquidity.
- A limit order lets you set the highest buy price or lowest sell price as a probability. Any unmatched amount rests in the order book until another trader matches it or you cancel it.

### Trade both sides

- You can sell shares you do not hold. Because a YES share and a NO share always pay out exactly 1 SUSQie together, selling YES at a price is the same trade as buying NO at the complement price. For example, selling YES at 0.52 without holding YES places a buy order for NO at 0.48.
- Buying the side opposite to your position works too. Each opposite share you buy cancels against one share you hold, and you are paid 1 SUSQie per cancelled pair immediately. That payment counts toward the purchase, so closing a position this way needs little or no balance up front.
- You can keep resting orders on both sides of a market at the same time. Your orders never trade against each other: an incoming order fills other traders first, and if it would cross your own resting order, the crossing remainder is cancelled and the panel tells you why.

### Understand liquidity

SIG competition markets use peer-to-peer matching. A trade can execute only when a compatible order is available. Market-maker quotes may supply that liquidity; you can also place a limit order to wait for another trader to match it.

### Use the market page

- Read the question, rules, and settlement date before trading. Price and volume charts show how the market has changed. The trade panel shows the order controls and your current position.
- News provides relevant updates. Sharing controls let you send the market to others. Comments and Takes are not available on the SIG site.
- See [Settlement & Payouts](https://sig.thesuper.market/docs/settlement-and-payouts) for what happens after resolution.

### Automation and API

- You are permitted to trade manually via the WebUI, or to use our API to create automated trading strategies. See the [API Reference](https://sig.thesuper.market/api/v1/docs) for endpoint permissions and details. Participant keys can trade but cannot create markets or use administrative functions.

---

## Settlement & Payouts

Settlement records the final outcome and pays or refunds open positions.

### When the competition is final

Trading closes at the published end time. The competition's final standings are locked only after every market has settled. Chamber-control questions and any race that goes to a runoff are settled by administrators once the result is official.

### Automatic settlement

These market types settle automatically from their configured data sources:

- Elections
- Economics
- Politics

Autosettlement workers check eligible contracts every four hours. A market remains pending until its source provides a valid result. When a result is available, the market records the outcome and processes positions.

An administrator can force settlement of an automatically settled market when an override is required. A documented reason is required for force settlement.

### Manual settlement

The four chamber-control questions are settled manually by an administrator once control of the relevant chamber is established. These markets remain closed to trading while awaiting their manual result; the automatic election-data checks do not settle them.

### Winning and losing positions

- Each winning share pays 1 SUSQie. Each losing share pays 0 SUSQies.
- For example, 25 winning shares pay 25 SUSQies. This is the gross payout, not the gain after the original purchase cost.
- A YES position wins when the market resolves YES. A NO position wins when the market resolves NO.

### Where payouts go

- Positions pay into the holder's balance in the competition where they were traded.

### Refunds

- A cancelled or N/A outcome refunds the remaining position instead of choosing a winner. The refund returns the refundable cost of the held shares.
- Once processed, payouts and refunds appear in settlement transaction history.

---

## Portfolio & Standings

The Portfolio page brings together your balance, positions, orders, and performance. Standings compare trading results with other participants.

### Read your portfolio

The portfolio summary shows:

- Number of positions
- Current position value
- Daily gains and losses
- Available SUSQie balance
- The value chart shows how the portfolio has changed over time. Current Holdings lists each position with its current value and gain or loss.
- Use search to find a market in your holdings. Filter by category or sort by resolution date, confidence, category, or gains and losses. Select Export to download the current holdings as a CSV file.
- The portfolio also lists open orders and transaction history. Cancel an open order there when you no longer want it to wait for a match.

### Use standings

- Standings rank participants within each individual competition in the Predictions Cup series. Each board shows rank and the relevant performance details.
- When participants tie, they share a rank and the next rank skips by the size of the tie, such as 1-1-3. Open a listed username to view that user's public profile.

---

## The Predictions Cup Series

The Predictions Cup is structured as a sequence of individual competitions focused by subject matter and time. The current instance is the 2026 Midterm Election which runs from October 1, 2026 noon ET to November 4, 2026 noon ET. Trading closes at that time; final rankings are confirmed once every market has settled. All of the markets you see are eligible to be traded here, and are related broadly to the 2026 United States midterm elections.

### Results

Users all begin each competition with 100,000 SUSQies. You must complete at least one trade to receive a rank. Among participants who meet that requirement, the ranking at the end of the competition is fully determined by your final SUSQie balance, the higher the balance the higher you rank.

### Prizes

Certain instances may have prizes. For the 2026 Midterm Election competition, the top ranked user will win $30,000. Second place will win $5,000 and third place will win $2,500.

### Ties

In the event of a tie, all users tied for a rank will split evenly the total prize pool they qualify for. For instance, if two users tie for first, they will each split evenly $35,000 representing the sum of the first place and second place prizes. The third place user will still get $2,500.

---

## Account & Notifications

Your profile controls your public identity, privacy, notifications, and security. The notification bell keeps recent account and market activity close at hand.

### Use the notification bell

The bell displays your unread notification count. Open it to read recent notifications, follow their links, or mark all notifications as read. Opening an unread item marks that item as read.

Notifications include:

- Settlements for markets you traded
- Filled orders

### Choose delivery preferences

In-app notifications remain available. Member notification emails are disabled for SIG, while account confirmation and password-reset emails remain available. Notification settings do not enable member notification emails for SIG.

### Manage your profile

Use the Profile tab to change your avatar. SIG assigns a generated username automatically, and participants cannot change it. Your email is displayed there but must be changed through support.

Privacy settings can make the entire profile private. For a public profile, you can separately control whether other users see your balance, traded markets, and standings statistics.

The Security tab contains account security controls and sign out.
