# Live Sports Arbitrage Framework

## Why Sports > BTC 15-min for Arbitrage

| Factor | BTC 15-min | Live Sports |
|--------|------------|-------------|
| **Taker fees** | 0.5-1% at mid prices | Often 0% |
| **Information edge** | Nearly impossible | Possible (injuries, momentum) |
| **Competition** | Extremely high (bots) | Medium |
| **Market efficiency** | Very high | Variable (esp. live) |
| **Cross-platform arb** | Only Polymarket | PM + DraftKings + FanDuel + more |

## Two Main Strategies

### Strategy 1: Cross-Platform Arbitrage
Compare odds between:
- Polymarket
- DraftKings
- FanDuel
- BetMGM
- Pinnacle

When combined implied probability < 100%, you have guaranteed profit.

### Strategy 2: Live Game Momentum
- Watch games in real-time
- React to events faster than odds update
- Capture mispriced moments

## Implementation Plan

1. `odds_fetcher.py` - Fetch live odds from multiple platforms
2. `arb_finder.py` - Find arbitrage opportunities
3. `bet_calculator.py` - Calculate optimal stake splits
4. `monitor.py` - Real-time monitoring dashboard

