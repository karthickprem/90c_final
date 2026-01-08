"""
BTC 15-MIN UP/DOWN PROBABILITY RESEARCH

Goal: Build an algorithm to calculate TRUE probability of Up vs Down
based on:
1. Opening price (price to beat)
2. Current BTC price  
3. Time remaining in window
4. Historical volatility

THEORY:
This is essentially a binary/digital option pricing problem.
- Payout = $1 if BTC_end >= BTC_open (Up wins)
- Payout = $0 if BTC_end < BTC_open (Down wins)

Using simplified Black-Scholes for binary options:
P(Up) = N(d2) where:
d2 = ln(S/K) / (σ * √T)

Where:
- S = current BTC price
- K = strike/opening price (price to beat)
- σ = volatility (annualized, then scaled to time period)
- T = time remaining (as fraction of year)
- N() = cumulative normal distribution

Let's collect data and calibrate this model.
"""

import requests
import json
import time
import math
import statistics
from datetime import datetime, timezone
from pathlib import Path
from scipy import stats  # For normal distribution

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

session = requests.Session()


def fetch_btc_price_from_market(slug: str) -> dict:
    """
    Fetch BTC price info from a Polymarket window.
    Returns opening price context from market data.
    """
    try:
        r = session.get(f"{GAMMA_API}/markets?slug={slug}", timeout=10)
        markets = r.json()
        if not markets:
            return None
        
        market = markets[0]
        return {
            "slug": slug,
            "question": market.get("question"),
            "description": market.get("description", "")[:500],
            "end_date": market.get("endDate"),
            "resolution_source": "Chainlink BTC/USD",
        }
    except:
        return None


def fetch_current_btc_price() -> float:
    """
    Fetch current BTC price from a public API.
    We'll use CoinGecko as it's free and reliable.
    """
    try:
        # CoinGecko API
        r = session.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": "bitcoin", "vs_currencies": "usd"},
            timeout=10
        )
        data = r.json()
        return data.get("bitcoin", {}).get("usd", 0)
    except:
        return 0


def fetch_btc_historical_volatility(periods: int = 24) -> float:
    """
    Calculate BTC historical volatility from recent price data.
    Returns annualized volatility.
    """
    try:
        # Get hourly candles from CoinGecko
        r = session.get(
            "https://api.coingecko.com/api/v3/coins/bitcoin/market_chart",
            params={"vs_currency": "usd", "days": "1"},
            timeout=10
        )
        data = r.json()
        prices = [p[1] for p in data.get("prices", [])]
        
        if len(prices) < 10:
            return 0.5  # Default 50% annual vol
        
        # Calculate returns
        returns = []
        for i in range(1, len(prices)):
            ret = math.log(prices[i] / prices[i-1])
            returns.append(ret)
        
        # Standard deviation of returns
        std_ret = statistics.stdev(returns)
        
        # Annualize: multiply by sqrt(periods_per_year)
        # CoinGecko gives ~5-min data for 1 day, so ~288 periods/day
        # Annualize: sqrt(288 * 365)
        periods_per_year = 288 * 365
        annual_vol = std_ret * math.sqrt(periods_per_year)
        
        return annual_vol
    except Exception as e:
        print(f"Error fetching volatility: {e}")
        return 0.5  # Default


def calculate_probability_up(
    current_price: float,
    opening_price: float,
    seconds_remaining: float,
    annual_volatility: float = 0.5,
) -> float:
    """
    Calculate probability of "Up" winning using binary option pricing.
    
    Formula (simplified Black-Scholes for binary):
    P(Up) = N(d2)
    d2 = ln(S/K) / (σ * √T)
    
    Where T is in years.
    """
    if opening_price <= 0 or current_price <= 0:
        return 0.5
    
    # Convert seconds to years
    seconds_per_year = 365.25 * 24 * 3600
    T = seconds_remaining / seconds_per_year
    
    if T <= 0:
        # Window ended - return 1 or 0 based on current vs opening
        return 1.0 if current_price >= opening_price else 0.0
    
    # d2 calculation
    # For short time periods, we simplify by ignoring drift
    log_ratio = math.log(current_price / opening_price)
    vol_sqrt_t = annual_volatility * math.sqrt(T)
    
    if vol_sqrt_t == 0:
        return 1.0 if current_price >= opening_price else 0.0
    
    d2 = log_ratio / vol_sqrt_t
    
    # N(d2) using scipy
    prob_up = stats.norm.cdf(d2)
    
    return prob_up


def calculate_edge(
    market_price: float,
    true_probability: float,
) -> float:
    """
    Calculate edge: true probability - market price.
    Positive edge = market undervalues the outcome.
    """
    return true_probability - market_price


def run_analysis():
    """
    Run comprehensive analysis of BTC 15m markets.
    """
    print("=" * 70)
    print("BTC 15-MIN UP/DOWN PROBABILITY RESEARCH")
    print("=" * 70)
    
    # Step 1: Get current BTC price
    print("\n1. FETCHING CURRENT BTC PRICE...")
    btc_price = fetch_current_btc_price()
    print(f"   Current BTC: ${btc_price:,.2f}")
    
    # Step 2: Calculate historical volatility
    print("\n2. CALCULATING HISTORICAL VOLATILITY...")
    annual_vol = fetch_btc_historical_volatility()
    print(f"   Annual volatility: {annual_vol*100:.1f}%")
    print(f"   15-min volatility: {annual_vol * math.sqrt(15/(365.25*24*60)) * 100:.3f}%")
    
    # Step 3: Get current market window
    print("\n3. FETCHING CURRENT MARKET WINDOW...")
    ts = int(time.time())
    window_start = ts - (ts % 900)
    window_end = window_start + 900
    slug = f"btc-updown-15m-{window_start}"
    
    market_info = fetch_btc_price_from_market(slug)
    if market_info:
        print(f"   Market: {market_info['question']}")
        print(f"   End: {market_info['end_date']}")
    
    seconds_remaining = window_end - ts
    print(f"   Seconds remaining: {seconds_remaining}")
    
    # Step 4: Fetch market prices
    print("\n4. FETCHING MARKET PRICES...")
    r = session.get(f"{GAMMA_API}/markets?slug={slug}", timeout=10)
    markets = r.json()
    
    if markets:
        market = markets[0]
        tokens = market.get("clobTokenIds", [])
        if isinstance(tokens, str):
            tokens = json.loads(tokens)
        
        outcomes = market.get("outcomes", [])
        if isinstance(outcomes, str):
            outcomes = json.loads(outcomes)
        
        for i, (outcome, token) in enumerate(zip(outcomes, tokens)):
            try:
                book = session.get(f"{CLOB_API}/book", params={"token_id": token}, timeout=5).json()
                bids = book.get("bids", [])
                asks = book.get("asks", [])
                
                if bids and asks:
                    bid = float(bids[0]["price"])
                    ask = float(asks[0]["price"])
                    mid = (bid + ask) / 2
                    print(f"   {outcome}: bid={bid:.4f}, ask={ask:.4f}, mid={mid:.4f}")
            except:
                pass
    
    # Step 5: Calculate theoretical probabilities
    print("\n5. PROBABILITY ANALYSIS...")
    print("-" * 70)
    
    # We don't know the exact opening price from API, so let's simulate scenarios
    print("\nScenario analysis: What if opening price was...")
    
    scenarios = [
        ("BTC exactly at open", btc_price),
        ("BTC 0.1% above open", btc_price / 1.001),
        ("BTC 0.2% above open", btc_price / 1.002),
        ("BTC 0.5% above open", btc_price / 1.005),
        ("BTC 0.1% below open", btc_price / 0.999),
        ("BTC 0.2% below open", btc_price / 0.998),
        ("BTC 0.5% below open", btc_price / 0.995),
    ]
    
    print(f"\n{'Scenario':<25} {'Open Price':<12} {'P(Up)':<10} {'P(Down)':<10}")
    print("-" * 60)
    
    for name, opening in scenarios:
        p_up = calculate_probability_up(btc_price, opening, seconds_remaining, annual_vol)
        p_down = 1 - p_up
        print(f"{name:<25} ${opening:>10,.0f} {p_up*100:>8.1f}% {p_down*100:>8.1f}%")
    
    # Step 6: Edge calculation
    print("\n6. EDGE CALCULATION...")
    print("-" * 70)
    print("\nIf market prices Up at 50c (0.50) and true probability is:")
    
    market_price = 0.50
    for true_p in [0.55, 0.60, 0.70, 0.80, 0.90]:
        edge = calculate_edge(market_price, true_p)
        print(f"   P(Up)={true_p*100:.0f}% → Edge = {edge*100:.1f}c per $1")
    
    # Step 7: Time decay analysis
    print("\n7. TIME DECAY ANALYSIS...")
    print("-" * 70)
    print("\nHow probability changes as time runs out:")
    print("(Assuming BTC is 0.2% above opening price)")
    
    opening = btc_price / 1.002  # 0.2% above
    
    print(f"\n{'Time Left':<15} {'P(Up)':<10} {'Confidence':<15}")
    print("-" * 40)
    
    for mins in [15, 10, 5, 2, 1, 0.5, 0.1]:
        secs = mins * 60
        p_up = calculate_probability_up(btc_price, opening, secs, annual_vol)
        conf = "Low" if 0.4 < p_up < 0.6 else "Medium" if 0.3 < p_up < 0.7 else "High"
        print(f"{mins:>5.1f} min      {p_up*100:>6.1f}%    {conf}")
    
    print("\n" + "=" * 70)
    print("KEY INSIGHTS")
    print("=" * 70)
    print("""
1. VOLATILITY MATTERS
   - Higher volatility = less certainty = prices stay closer to 50/50
   - Lower volatility = more certainty = clearer winners

2. TIME DECAY
   - As time runs out, probability converges to 0 or 100
   - Edge opportunities appear when market lags this convergence

3. DISTANCE FROM STRIKE
   - Further from opening price = higher confidence
   - But market usually prices this in quickly

4. EDGE STRATEGY
   - Monitor BTC price vs opening price
   - Calculate true probability
   - Buy when market price < true probability - buffer
   - Best opportunities: late window + clear winner + market lag
""")
    
    return {
        "btc_price": btc_price,
        "annual_vol": annual_vol,
        "seconds_remaining": seconds_remaining,
    }


if __name__ == "__main__":
    run_analysis()

