"""
POLYMARKET UNIVERSAL OPPORTUNITY SCANNER

Scans ALL markets on Polymarket for:
1. Full-set arbitrage (YES + NO < $1.00)
2. Markets resolving soon with high probability outcomes

Run: python polymarket_scanner.py
"""
import asyncio
import aiohttp
import time
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass
from typing import Optional, List, Dict, Tuple
import sys
import json

# Fix Windows encoding
sys.stdout.reconfigure(encoding='utf-8')

# ============================================================
# CONFIGURATION
# ============================================================

# Alert thresholds
FULLSET_MAX_COST_CENTS = 98  # Alert if YES_ask + NO_ask <= 98c ($0.98)
MIN_EDGE_CENTS = 2  # Minimum edge to alert (2c = $0.02)

# Scanning settings
SCAN_INTERVAL_SECONDS = 5
API_TIMEOUT = 15

# API endpoints
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

# ============================================================
# DATA STRUCTURES
# ============================================================

@dataclass
class Market:
    condition_id: str
    question: str
    slug: str
    end_date: Optional[datetime]
    outcome_yes: str
    outcome_no: str
    yes_price: float  # 0-1
    no_price: float   # 0-1
    volume: float
    liquidity: float


@dataclass
class Opportunity:
    market_question: str
    slug: str
    yes_price: int  # cents
    no_price: int   # cents
    combined: int   # cents
    edge: int       # cents (100 - combined)
    volume: float
    end_date: str


# ============================================================
# API FUNCTIONS
# ============================================================

async def fetch_json(session: aiohttp.ClientSession, url: str, params: dict = None) -> Optional[dict]:
    """Fetch JSON from URL with error handling."""
    try:
        async with session.get(url, params=params, timeout=API_TIMEOUT) as resp:
            if resp.status == 200:
                return await resp.json()
            elif resp.status == 429:
                print("  [Rate limited, waiting 10s...]")
                await asyncio.sleep(10)
                return None
            else:
                return None
    except asyncio.TimeoutError:
        print("  [Timeout]")
        return None
    except Exception as e:
        return None


async def get_all_markets(session: aiohttp.ClientSession) -> List[Market]:
    """Fetch all active markets from Polymarket."""
    markets = []
    offset = 0
    limit = 100
    
    while True:
        url = f"{GAMMA_API}/markets"
        params = {
            "closed": "false",
            "limit": limit,
            "offset": offset,
            "active": "true"
        }
        
        data = await fetch_json(session, url, params)
        
        if not data or len(data) == 0:
            break
        
        for m in data:
            try:
                tokens = m.get("tokens", [])
                if len(tokens) < 2:
                    continue
                
                # Parse end date
                end_date = None
                end_str = m.get("endDate") or m.get("end_date_iso")
                if end_str:
                    try:
                        end_date = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                    except:
                        pass
                
                # Get prices
                yes_price = float(tokens[0].get("price", 0.5))
                no_price = float(tokens[1].get("price", 0.5))
                
                market = Market(
                    condition_id=m.get("conditionId", ""),
                    question=m.get("question", "")[:100],
                    slug=m.get("slug", ""),
                    end_date=end_date,
                    outcome_yes=tokens[0].get("outcome", "Yes"),
                    outcome_no=tokens[1].get("outcome", "No"),
                    yes_price=yes_price,
                    no_price=no_price,
                    volume=float(m.get("volume", 0) or 0),
                    liquidity=float(m.get("liquidity", 0) or 0)
                )
                markets.append(market)
                
            except Exception as e:
                continue
        
        offset += limit
        
        # Safety limit
        if offset > 1000:
            break
        
        await asyncio.sleep(0.5)  # Rate limit protection
    
    return markets


def find_opportunities(markets: List[Market]) -> List[Opportunity]:
    """Find full-set arbitrage opportunities."""
    opportunities = []
    
    for m in markets:
        # Convert to cents
        yes_cents = int(m.yes_price * 100)
        no_cents = int(m.no_price * 100)
        
        # Add spread estimate (real ask is usually 1-2c higher than mid)
        yes_ask = min(yes_cents + 1, 99)
        no_ask = min(no_cents + 1, 99)
        
        combined = yes_ask + no_ask
        edge = 100 - combined
        
        if combined <= FULLSET_MAX_COST_CENTS and edge >= MIN_EDGE_CENTS:
            end_str = m.end_date.strftime("%Y-%m-%d %H:%M") if m.end_date else "Unknown"
            
            opportunities.append(Opportunity(
                market_question=m.question,
                slug=m.slug,
                yes_price=yes_ask,
                no_price=no_ask,
                combined=combined,
                edge=edge,
                volume=m.volume,
                end_date=end_str
            ))
    
    # Sort by edge (best first)
    opportunities.sort(key=lambda x: x.edge, reverse=True)
    
    return opportunities


# ============================================================
# DISPLAY
# ============================================================

def display_opportunities(opportunities: List[Opportunity], scan_time: datetime):
    """Display found opportunities."""
    
    # Clear screen (Windows)
    print("\033[2J\033[H", end="")
    
    print("=" * 80)
    print("POLYMARKET OPPORTUNITY SCANNER")
    print(f"Last scan: {scan_time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Looking for: Combined cost <= {FULLSET_MAX_COST_CENTS}c (edge >= {MIN_EDGE_CENTS}c)")
    print("=" * 80)
    
    if not opportunities:
        print("\nNo opportunities found at this time.")
        print("\nThis is normal - arbitrage opportunities are rare and get taken quickly.")
        print("The scanner will beep when one appears!")
    else:
        print(f"\n*** FOUND {len(opportunities)} OPPORTUNITIES! ***\n")
        
        # Beep!
        try:
            import winsound
            winsound.Beep(1000, 300)
            winsound.Beep(1500, 300)
        except:
            print("\a")
        
        for i, opp in enumerate(opportunities[:10], 1):
            print("-" * 80)
            print(f"#{i} | Edge: {opp.edge}c | Combined: {opp.combined}c")
            print(f"   Question: {opp.market_question}")
            print(f"   YES: {opp.yes_price}c | NO: {opp.no_price}c")
            print(f"   Volume: ${opp.volume:,.0f} | Ends: {opp.end_date}")
            print(f"   Link: https://polymarket.com/event/{opp.slug}")
            
            # Calculate profit estimate
            gross = opp.edge / 100 * 10 * 2  # $10 per leg
            fee_est = 0.60  # Approximate fee
            net = gross - fee_est
            print(f"   Est. profit at $10/leg: ${net:.2f}")
    
    print("\n" + "=" * 80)
    print("Press Ctrl+C to stop. Refreshing every 5 seconds...")
    print("=" * 80)


# ============================================================
# MAIN LOOP
# ============================================================

async def run_scanner():
    """Main scanning loop."""
    
    print("Starting Polymarket scanner...")
    print("Fetching markets (this may take a moment)...\n")
    
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                scan_time = datetime.now()
                
                # Get all markets
                markets = await get_all_markets(session)
                
                if not markets:
                    print(f"[{scan_time.strftime('%H:%M:%S')}] No markets fetched. Retrying...")
                    await asyncio.sleep(SCAN_INTERVAL_SECONDS)
                    continue
                
                # Find opportunities
                opportunities = find_opportunities(markets)
                
                # Display
                display_opportunities(opportunities, scan_time)
                
                # Log to file
                if opportunities:
                    with open("opportunities_log.txt", "a", encoding="utf-8") as f:
                        f.write(f"\n[{scan_time}] Found {len(opportunities)} opportunities:\n")
                        for opp in opportunities:
                            f.write(f"  {opp.edge}c edge: {opp.market_question[:50]}...\n")
                
                await asyncio.sleep(SCAN_INTERVAL_SECONDS)
                
            except KeyboardInterrupt:
                print("\n\nScanner stopped.")
                break
            except Exception as e:
                print(f"Error: {e}")
                await asyncio.sleep(5)


# ============================================================
# ENTRY POINT
# ============================================================

def main():
    print("""
======================================================================
            POLYMARKET UNIVERSAL OPPORTUNITY SCANNER                      
======================================================================

Scans ALL active markets on Polymarket for:

  - Full-set arbitrage: When YES + NO prices < $1.00
    (Buy both sides, guaranteed profit at settlement)

  - Current threshold: Combined <= 98c (2c+ edge)

Will BEEP when opportunity is found!

======================================================================
    """)
    
    try:
        asyncio.run(run_scanner())
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()

