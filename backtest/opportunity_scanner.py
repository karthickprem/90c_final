"""
POLYMARKET OPPORTUNITY SCANNER

Continuously monitors BTC 15-minute markets for:
1. Full-set opportunities (combined cost < 97c)
2. High-probability entries (99c with good timing)

Run: python opportunity_scanner.py
"""
import asyncio
import aiohttp
import time
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Optional, List, Dict
import os
import sys
import winsound  # Windows sound alert

# ============================================================
# CONFIGURATION
# ============================================================

# Alert thresholds
FULLSET_MAX_COST = 97  # Alert if UP_ask + DOWN_ask <= this
HIGH_PROB_MIN_PRICE = 98  # Alert if price >= this
TIME_REMAINING_FOR_HIGH_PROB = 120  # Seconds remaining for high-prob alert

# Scanning settings
SCAN_INTERVAL_SECONDS = 2  # How often to check
MARKET_REFRESH_SECONDS = 60  # How often to refresh market list

# API endpoints
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

# ============================================================
# DATA STRUCTURES
# ============================================================

@dataclass
class MarketInfo:
    condition_id: str
    question: str
    end_time: datetime
    up_token: Optional[str] = None
    down_token: Optional[str] = None
    yes_token: Optional[str] = None
    no_token: Optional[str] = None


@dataclass
class Quote:
    bid: float
    ask: float
    mid: float


@dataclass
class Opportunity:
    market: str
    opp_type: str  # "FULLSET" or "HIGH_PROB"
    up_ask: int
    down_ask: int
    combined: int
    edge: int
    time_remaining: int
    timestamp: datetime


# ============================================================
# API FUNCTIONS
# ============================================================

async def fetch_json(session: aiohttp.ClientSession, url: str, params: dict = None) -> Optional[dict]:
    """Fetch JSON from URL with error handling."""
    try:
        async with session.get(url, params=params, timeout=10) as resp:
            if resp.status == 200:
                return await resp.json()
            elif resp.status == 429:
                print("  [Rate limited, waiting...]")
                await asyncio.sleep(5)
                return None
            else:
                return None
    except Exception as e:
        print(f"  [API error: {e}]")
        return None


async def find_btc_15m_markets(session: aiohttp.ClientSession) -> List[MarketInfo]:
    """Find active BTC 15-minute up/down markets."""
    markets = []
    
    # Search for BTC markets
    url = f"{GAMMA_API}/markets"
    params = {
        "closed": "false",
        "limit": 100
    }
    
    data = await fetch_json(session, url, params)
    if not data:
        return markets
    
    for market in data:
        question = market.get("question", "").lower()
        
        # Look for BTC 15-minute markets
        if "btc" in question and ("15" in question or "fifteen" in question) and ("up" in question or "down" in question):
            try:
                end_time_str = market.get("endDate") or market.get("end_date_iso")
                if end_time_str:
                    end_time = datetime.fromisoformat(end_time_str.replace("Z", "+00:00"))
                    
                    markets.append(MarketInfo(
                        condition_id=market.get("conditionId", market.get("condition_id", "")),
                        question=market.get("question", ""),
                        end_time=end_time
                    ))
            except Exception as e:
                continue
    
    return markets


async def get_orderbook(session: aiohttp.ClientSession, token_id: str) -> Optional[Quote]:
    """Get bid/ask for a token."""
    url = f"{CLOB_API}/book"
    params = {"token_id": token_id}
    
    data = await fetch_json(session, url, params)
    if not data:
        return None
    
    try:
        bids = data.get("bids", [])
        asks = data.get("asks", [])
        
        best_bid = float(bids[0]["price"]) * 100 if bids else 0
        best_ask = float(asks[0]["price"]) * 100 if asks else 100
        mid = (best_bid + best_ask) / 2
        
        return Quote(bid=best_bid, ask=best_ask, mid=mid)
    except:
        return None


async def get_market_prices(session: aiohttp.ClientSession, condition_id: str) -> Optional[Dict]:
    """Get prices for both outcomes of a market."""
    url = f"{GAMMA_API}/markets/{condition_id}"
    data = await fetch_json(session, url)
    
    if not data:
        return None
    
    try:
        tokens = data.get("tokens", [])
        if len(tokens) >= 2:
            result = {
                "token0_price": float(tokens[0].get("price", 0.5)) * 100,
                "token1_price": float(tokens[1].get("price", 0.5)) * 100,
                "token0_outcome": tokens[0].get("outcome", ""),
                "token1_outcome": tokens[1].get("outcome", ""),
            }
            
            # Calculate "ask" as price to buy (what we care about)
            # For simplicity, use price + 1c as estimated ask
            result["token0_ask"] = min(result["token0_price"] + 1, 99)
            result["token1_ask"] = min(result["token1_price"] + 1, 99)
            
            return result
    except:
        pass
    
    return None


# ============================================================
# OPPORTUNITY DETECTION
# ============================================================

def check_fullset_opportunity(up_ask: float, down_ask: float, threshold: int = FULLSET_MAX_COST) -> Optional[Opportunity]:
    """Check if full-set opportunity exists."""
    combined = int(up_ask + down_ask)
    
    if combined <= threshold:
        edge = 100 - combined
        return Opportunity(
            market="BTC 15m",
            opp_type="FULLSET",
            up_ask=int(up_ask),
            down_ask=int(down_ask),
            combined=combined,
            edge=edge,
            time_remaining=0,
            timestamp=datetime.now()
        )
    return None


def check_high_prob_opportunity(price: float, time_remaining: int, side: str) -> Optional[Opportunity]:
    """Check if high-probability entry exists."""
    if price >= HIGH_PROB_MIN_PRICE and time_remaining <= TIME_REMAINING_FOR_HIGH_PROB:
        return Opportunity(
            market=f"BTC 15m {side}",
            opp_type="HIGH_PROB",
            up_ask=int(price) if side == "UP" else 0,
            down_ask=int(price) if side == "DOWN" else 0,
            combined=0,
            edge=100 - int(price),
            time_remaining=time_remaining,
            timestamp=datetime.now()
        )
    return None


# ============================================================
# ALERTS
# ============================================================

def alert(opportunity: Opportunity):
    """Sound and visual alert for opportunity."""
    print("\n" + "!" * 70)
    print("!" * 70)
    print(f"  *** OPPORTUNITY DETECTED: {opportunity.opp_type} ***")
    print(f"  Market: {opportunity.market}")
    print(f"  Time: {opportunity.timestamp.strftime('%H:%M:%S')}")
    
    if opportunity.opp_type == "FULLSET":
        print(f"  UP ask: {opportunity.up_ask}c")
        print(f"  DOWN ask: {opportunity.down_ask}c")
        print(f"  COMBINED: {opportunity.combined}c")
        print(f"  EDGE: {opportunity.edge}c per pair")
        
        # Estimate profit at $10/leg
        gross = opportunity.edge / 100 * 10 * 2
        fee_est = 0.65  # Approximate
        net = gross - fee_est
        print(f"  Est. profit at $10/leg: ${net:.2f}")
    else:
        price = opportunity.up_ask or opportunity.down_ask
        print(f"  Price: {price}c")
        print(f"  Time remaining: {opportunity.time_remaining}s")
        print(f"  Expected win rate: ~{price}%+")
    
    print("!" * 70)
    print("!" * 70 + "\n")
    
    # Sound alert (Windows)
    try:
        winsound.Beep(1000, 500)  # 1000 Hz for 500ms
        winsound.Beep(1500, 500)  # 1500 Hz for 500ms
    except:
        print("\a")  # Fallback beep


# ============================================================
# MAIN SCANNER
# ============================================================

async def scan_markets():
    """Main scanning loop."""
    print("=" * 70)
    print("POLYMARKET OPPORTUNITY SCANNER")
    print("=" * 70)
    print(f"\nSettings:")
    print(f"  Full-set alert: combined <= {FULLSET_MAX_COST}c")
    print(f"  High-prob alert: price >= {HIGH_PROB_MIN_PRICE}c with <= {TIME_REMAINING_FOR_HIGH_PROB}s left")
    print(f"  Scan interval: {SCAN_INTERVAL_SECONDS}s")
    print("\nStarting scan... (Press Ctrl+C to stop)\n")
    
    last_alert_time = {}  # Prevent duplicate alerts
    
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                now = datetime.now(timezone.utc)
                print(f"[{now.strftime('%H:%M:%S')}] Scanning...", end=" ")
                
                # Find active BTC 15m markets
                markets = await find_btc_15m_markets(session)
                
                opportunities_found = 0
                
                for market in markets:
                    # Calculate time remaining
                    time_remaining = (market.end_time - now).total_seconds()
                    
                    # Skip if too far out or already ended
                    if time_remaining < 0 or time_remaining > 900:
                        continue
                    
                    # Get prices
                    prices = await get_market_prices(session, market.condition_id)
                    if not prices:
                        continue
                    
                    up_ask = prices["token0_ask"]
                    down_ask = prices["token1_ask"]
                    
                    # Swap if needed based on outcome names
                    if "down" in prices["token0_outcome"].lower():
                        up_ask, down_ask = down_ask, up_ask
                    
                    # Check for full-set opportunity
                    opp = check_fullset_opportunity(up_ask, down_ask)
                    if opp:
                        opp.time_remaining = int(time_remaining)
                        opp.market = market.question[:50]
                        
                        # Avoid duplicate alerts (same market within 30s)
                        key = f"fullset_{market.condition_id}"
                        if key not in last_alert_time or time.time() - last_alert_time[key] > 30:
                            alert(opp)
                            last_alert_time[key] = time.time()
                            opportunities_found += 1
                    
                    # Check for high-prob opportunities
                    for side, price in [("UP", up_ask), ("DOWN", down_ask)]:
                        opp = check_high_prob_opportunity(price, int(time_remaining), side)
                        if opp:
                            opp.market = f"{market.question[:40]}... ({side})"
                            
                            key = f"highprob_{market.condition_id}_{side}"
                            if key not in last_alert_time or time.time() - last_alert_time[key] > 30:
                                alert(opp)
                                last_alert_time[key] = time.time()
                                opportunities_found += 1
                
                if opportunities_found == 0:
                    print(f"No opportunities. {len(markets)} markets checked.")
                else:
                    print(f"{opportunities_found} opportunities found!")
                
                await asyncio.sleep(SCAN_INTERVAL_SECONDS)
                
            except KeyboardInterrupt:
                print("\n\nScanner stopped by user.")
                break
            except Exception as e:
                print(f"Error: {e}")
                await asyncio.sleep(5)


# ============================================================
# SIMPLE VERSION (if API is complex)
# ============================================================

async def simple_scan():
    """Simpler version that just monitors known endpoints."""
    print("=" * 70)
    print("SIMPLE POLYMARKET SCANNER")
    print("=" * 70)
    print("\nMonitoring for BTC 15m opportunities...")
    print("Press Ctrl+C to stop\n")
    
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                now = datetime.now()
                print(f"[{now.strftime('%H:%M:%S')}] Checking...", end=" ")
                
                # Try to get BTC markets
                url = f"{GAMMA_API}/markets"
                params = {
                    "tag": "crypto",
                    "closed": "false",
                    "limit": 50
                }
                
                data = await fetch_json(session, url, params)
                
                if data:
                    btc_markets = []
                    for m in data:
                        q = m.get("question", "").lower()
                        if "btc" in q or "bitcoin" in q:
                            btc_markets.append(m)
                    
                    print(f"Found {len(btc_markets)} BTC markets")
                    
                    for m in btc_markets[:5]:  # Show first 5
                        q = m.get("question", "")[:60]
                        
                        # Get tokens and prices
                        tokens = m.get("tokens", [])
                        if len(tokens) >= 2:
                            p0 = float(tokens[0].get("price", 0.5)) * 100
                            p1 = float(tokens[1].get("price", 0.5)) * 100
                            combined = p0 + p1
                            
                            if combined <= FULLSET_MAX_COST:
                                alert(Opportunity(
                                    market=q,
                                    opp_type="FULLSET",
                                    up_ask=int(p0),
                                    down_ask=int(p1),
                                    combined=int(combined),
                                    edge=int(100 - combined),
                                    time_remaining=0,
                                    timestamp=now
                                ))
                            else:
                                print(f"  {q}: {p0:.0f}c + {p1:.0f}c = {combined:.0f}c")
                else:
                    print("No data received")
                
                await asyncio.sleep(SCAN_INTERVAL_SECONDS)
                
            except KeyboardInterrupt:
                print("\n\nStopped.")
                break
            except Exception as e:
                print(f"Error: {e}")
                await asyncio.sleep(5)


# ============================================================
# ENTRY POINT
# ============================================================

def main():
    import sys
    sys.stdout.reconfigure(encoding='utf-8')
    
    print("""
======================================================================
            POLYMARKET OPPORTUNITY SCANNER                      
======================================================================

  Monitors BTC 15-minute markets for:
  - Full-set opportunities (combined < 97c)
  - High-probability entries (98c+ with <2min left)
  
  Will BEEP when opportunity is found!

======================================================================
    """)
    
    if len(sys.argv) > 1 and sys.argv[1] == "--simple":
        asyncio.run(simple_scan())
    else:
        asyncio.run(scan_markets())


if __name__ == "__main__":
    main()

