"""
Direct Scanner for BTC 15-min Up/Down Markets

Uses direct event search based on the known Polymarket URL pattern:
https://polymarket.com/event/btc-updown-15m-{timestamp}

This bypasses the generic market discovery to directly find these specific markets.
"""

import logging
import time
import json
import requests
from typing import Optional, List, Dict, Tuple
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta

from .config import ArbConfig, load_config

logger = logging.getLogger(__name__)


@dataclass 
class BTCMarketData:
    """Live BTC 15-min market data."""
    market_id: str
    event_slug: str
    window_start: datetime
    window_end: datetime
    
    # Token IDs
    up_token_id: str  # YES = Up
    down_token_id: str  # NO = Down
    
    # Current prices (from orderbook)
    ask_up: float  # Best ask for "Up"
    ask_down: float  # Best ask for "Down"
    size_up: float
    size_down: float
    
    # Calculated
    @property
    def sum_asks(self) -> float:
        return self.ask_up + self.ask_down
    
    @property
    def edge(self) -> float:
        """Theoretical edge if sum < 1."""
        return 1.0 - self.sum_asks
    
    @property
    def has_arb(self) -> bool:
        """True if sum of asks < 1 (potential arb)."""
        return self.sum_asks < 1.0
    
    @property
    def seconds_remaining(self) -> float:
        now = datetime.now(timezone.utc)
        return max(0, (self.window_end - now).total_seconds())


class DirectScanner:
    """
    Directly scans BTC 15-min markets on Polymarket.
    
    Uses the event search to find active btc-updown-15m events.
    """
    
    GAMMA_API = "https://gamma-api.polymarket.com"
    CLOB_API = "https://clob.polymarket.com"
    
    def __init__(self, config: ArbConfig = None):
        self.config = config or load_config()
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "PolymarketArbBot/1.0",
            "Accept": "application/json"
        })
    
    def _get(self, url: str, params: dict = None, timeout: float = 10) -> dict:
        """Make GET request."""
        try:
            resp = self.session.get(url, params=params, timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"API error: {e}")
            return {}
    
    def find_btc_15m_events(self) -> List[dict]:
        """
        Find BTC 15-min Up/Down events.
        
        Search for events with slug containing 'btc-updown-15m' or 
        title containing 'Bitcoin Up or Down'.
        """
        events = []
        
        # Method 1: Search by title
        try:
            url = f"{self.GAMMA_API}/events"
            params = {
                "limit": 50,
                "active": "true",
                "closed": "false",
                "order": "startDate",
                "ascending": "false",
            }
            
            all_events = self._get(url, params)
            
            if isinstance(all_events, list):
                for event in all_events:
                    title = (event.get("title") or "").lower()
                    slug = (event.get("slug") or "").lower()
                    
                    # Match BTC 15-min markets
                    if "btc-updown-15m" in slug or "btc-updown" in slug:
                        events.append(event)
                    elif "bitcoin up or down" in title:
                        events.append(event)
                    elif "bitcoin" in title and "15" in title and ("up" in title or "down" in title):
                        events.append(event)
        except Exception as e:
            logger.warning(f"Event search failed: {e}")
        
        # Method 2: Direct slug search 
        try:
            # Try a few recent timestamps (every 15 mins)
            now = datetime.now(timezone.utc)
            
            for offset_mins in range(0, 60, 15):
                ts = now - timedelta(minutes=offset_mins)
                # Round to 15-min boundary
                ts = ts.replace(minute=(ts.minute // 15) * 15, second=0, microsecond=0)
                epoch = int(ts.timestamp())
                
                slug = f"btc-updown-15m-{epoch}"
                url = f"{self.GAMMA_API}/events/{slug}"
                
                try:
                    event = self._get(url, timeout=5)
                    if event and event.get("id"):
                        if event not in events:
                            events.append(event)
                except:
                    pass
        except Exception as e:
            logger.debug(f"Direct slug search error: {e}")
        
        logger.info(f"Found {len(events)} BTC 15-min events")
        return events
    
    def get_orderbook(self, token_id: str) -> Tuple[float, float]:
        """
        Get best ask price and size for a token.
        
        Returns (best_ask_price, best_ask_size).
        """
        try:
            url = f"{self.CLOB_API}/book"
            data = self._get(url, params={"token_id": token_id})
            
            asks = data.get("asks") or []
            if asks:
                # Sort by price ascending
                asks.sort(key=lambda x: float(x.get("price", 999)))
                best = asks[0]
                return float(best.get("price", 0)), float(best.get("size", 0))
            
            return 0, 0
        except Exception as e:
            logger.warning(f"Orderbook fetch failed: {e}")
            return 0, 0
    
    def scan_market(self, event: dict) -> Optional[BTCMarketData]:
        """
        Scan a single BTC 15-min event for arb opportunity.
        """
        try:
            markets = event.get("markets") or []
            
            if not markets:
                return None
            
            # For BTC Up/Down, there should be one market with 2 outcomes
            market = markets[0] if len(markets) == 1 else None
            
            if not market:
                # Try to find the relevant market
                for m in markets:
                    question = (m.get("question") or "").lower()
                    if "up" in question or "down" in question:
                        market = m
                        break
            
            if not market:
                return None
            
            # Get token IDs
            token_ids_raw = market.get("clobTokenIds") or market.get("clob_token_ids") or []
            
            if isinstance(token_ids_raw, str):
                token_ids = json.loads(token_ids_raw)
            else:
                token_ids = token_ids_raw
            
            if len(token_ids) != 2:
                return None
            
            up_token = str(token_ids[0])  # First is typically YES/Up
            down_token = str(token_ids[1])  # Second is NO/Down
            
            # Get orderbook prices
            ask_up, size_up = self.get_orderbook(up_token)
            ask_down, size_down = self.get_orderbook(down_token)
            
            if ask_up <= 0 or ask_down <= 0:
                return None
            
            # Parse timing
            end_date_str = market.get("endDate") or event.get("endDate")
            start_date_str = market.get("startDate") or event.get("startDate")
            
            try:
                if end_date_str:
                    window_end = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
                else:
                    window_end = datetime.now(timezone.utc) + timedelta(minutes=15)
                
                if start_date_str:
                    window_start = datetime.fromisoformat(start_date_str.replace("Z", "+00:00"))
                else:
                    window_start = window_end - timedelta(minutes=15)
            except:
                window_end = datetime.now(timezone.utc) + timedelta(minutes=15)
                window_start = window_end - timedelta(minutes=15)
            
            return BTCMarketData(
                market_id=market.get("conditionId", ""),
                event_slug=event.get("slug", ""),
                window_start=window_start,
                window_end=window_end,
                up_token_id=up_token,
                down_token_id=down_token,
                ask_up=ask_up,
                ask_down=ask_down,
                size_up=size_up,
                size_down=size_down,
            )
        
        except Exception as e:
            logger.warning(f"Failed to scan market: {e}")
            return None
    
    def scan_all(self) -> List[BTCMarketData]:
        """
        Scan all BTC 15-min markets and return data.
        """
        events = self.find_btc_15m_events()
        results = []
        
        for event in events:
            data = self.scan_market(event)
            if data:
                results.append(data)
        
        # Sort by edge (best first)
        results.sort(key=lambda x: x.edge, reverse=True)
        
        return results
    
    def print_scan_results(self):
        """Print a formatted scan of current markets."""
        print("\n" + "="*70)
        print("BTC 15-min Up/Down Market Scanner")
        print("="*70)
        
        results = self.scan_all()
        
        if not results:
            print("\nNo active BTC 15-min markets found.")
            print("\nThis could mean:")
            print("1. Markets are between windows")
            print("2. API discovery needs adjustment")
            print("3. No active BTC 15-min markets on Polymarket right now")
            return
        
        print(f"\nFound {len(results)} markets:\n")
        
        for i, data in enumerate(results):
            status = "ARB!" if data.has_arb else "NO ARB"
            
            print(f"{i+1}. {data.event_slug}")
            print(f"   Up: {data.ask_up:.2f}¢  |  Down: {data.ask_down:.2f}¢")
            print(f"   Sum: {data.sum_asks:.4f}  |  Edge: {data.edge:.4f} ({data.edge*100:.2f}%)")
            print(f"   Depth: Up={data.size_up:.0f} Down={data.size_down:.0f}")
            print(f"   Time remaining: {data.seconds_remaining:.0f}s")
            print(f"   Status: [{status}]")
            print()
        
        # Summary
        arb_markets = [d for d in results if d.has_arb]
        print("-"*70)
        print(f"Total markets: {len(results)}")
        print(f"With potential arb (sum < 1): {len(arb_markets)}")
        
        if arb_markets:
            best = arb_markets[0]
            print(f"\nBEST OPPORTUNITY:")
            print(f"  {best.event_slug}")
            print(f"  Edge: {best.edge:.4f} ({best.edge*100:.2f}%)")
            print(f"  Up: {best.ask_up:.4f}  Down: {best.ask_down:.4f}")


def quick_scan():
    """Quick scan for BTC 15-min arb opportunities."""
    scanner = DirectScanner()
    scanner.print_scan_results()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    quick_scan()

