"""
API Doctor - Polymarket API Validation Script

Validates:
1. Discovery of BTC 15m Up/Down markets
2. Price fetching at target frequency
3. Window timing alignment

Run standalone to debug API issues before running paper_mode.
"""

from __future__ import annotations

import argparse
import asyncio
import aiohttp
import time
import sys
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass


GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_HOST = "https://clob.polymarket.com"

# Short timeouts to avoid blocking
CONNECT_TIMEOUT = 2.0
READ_TIMEOUT = 2.0


@dataclass
class MarketInfo:
    """Discovered market info."""
    market_id: str
    condition_id: str
    slug: str
    question: str
    up_token_id: str
    down_token_id: str
    outcomes: List[str]
    closed: bool
    active: bool


@dataclass
class PriceSample:
    """Single price sample with bid/ask data."""
    ts: float
    up_bid: int
    up_ask: int
    up_mid: int
    down_bid: int
    down_ask: int
    down_mid: int
    dt: float  # Time since last sample
    source: str  # "BOOK" or "MID"


class APIDoctor:
    """Validates Polymarket API connectivity and endpoints."""
    
    def __init__(self, poll_interval: float = 1.0):
        self.poll_interval = poll_interval
        self.session: Optional[aiohttp.ClientSession] = None
        self.timeout = aiohttp.ClientTimeout(
            connect=CONNECT_TIMEOUT,
            sock_read=READ_TIMEOUT,
        )
    
    async def connect(self) -> None:
        """Initialize HTTP session with short timeouts."""
        if self.session is None:
            self.session = aiohttp.ClientSession(timeout=self.timeout)
    
    async def close(self) -> None:
        """Close HTTP session."""
        if self.session:
            await self.session.close()
            self.session = None
    
    def print_header(self, title: str) -> None:
        """Print section header."""
        print(f"\n{'='*60}")
        print(f"  {title}")
        print(f"{'='*60}\n")
    
    async def check_discovery(self) -> Optional[MarketInfo]:
        """
        PART 1: Discovery check
        
        Find BTC 15m Up/Down market by:
        1. Known slug patterns
        2. Keyword search in active markets
        """
        self.print_header("DISCOVERY CHECK")
        
        await self.connect()
        
        # Strategy 1: Try known slug patterns
        ts = int(time.time())
        start = ts - (ts % 900)
        
        known_slugs = [
            f"btc-updown-15m-{start}",
            f"will-btc-go-up-or-down-15m-{start}",
            f"btc-15m-{start}",
            "btc-15-minute-up-or-down",
            "bitcoin-15-minute",
        ]
        
        print("Trying known slug patterns...")
        for slug in known_slugs:
            market = await self._try_slug(slug)
            if market:
                self._print_market(market)
                return market
            print(f"  - {slug}: NOT FOUND")
        
        # Strategy 2: Search all active markets
        print("\nSearching all active markets...")
        markets = await self._fetch_active_markets()
        
        if not markets:
            print("  ERROR: Could not fetch any markets from API")
            print("  Check network connectivity to gamma-api.polymarket.com")
            return None
        
        print(f"  Total active markets: {len(markets)}")
        
        # Filter for BTC 15m candidates
        candidates = []
        for m in markets:
            q = m.get("question", "").lower()
            s = m.get("slug", "").lower()
            
            is_btc = "btc" in q or "btc" in s or "bitcoin" in q
            is_15m = "15" in q or "15m" in s or "15 min" in q
            is_updown = "up" in q or "down" in q
            
            if is_btc:
                score = sum([is_btc, is_15m, is_updown])
                candidates.append((score, m))
        
        candidates.sort(key=lambda x: -x[0])
        
        print(f"  BTC-related candidates: {len(candidates)}")
        
        if not candidates:
            print("\n  No BTC markets found. Top 10 active markets:")
            for m in markets[:10]:
                print(f"    - {m.get('slug', '?')[:50]}")
            return None
        
        # Try to find one with valid UP/DOWN tokens
        for score, m in candidates:
            market = self._parse_market(m)
            if market and market.up_token_id and market.down_token_id:
                print(f"\n  FOUND matching market!")
                self._print_market(market)
                return market
        
        # Show closest matches
        print("\n  No market with valid UP/DOWN tokens. Top 10 closest:")
        for score, m in candidates[:10]:
            q = m.get("question", "?")[:60]
            s = m.get("slug", "?")[:40]
            print(f"    [{score}] {s}")
            print(f"        Q: {q}")
        
        return None
    
    async def _try_slug(self, slug: str) -> Optional[MarketInfo]:
        """Try to fetch market by exact slug."""
        try:
            async with self.session.get(
                f"{GAMMA_API}/markets",
                params={"slug": slug},
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data and len(data) > 0:
                        return self._parse_market(data[0])
        except Exception as e:
            print(f"  - {slug}: ERROR - {type(e).__name__}: {str(e)[:50]}")
        return None
    
    async def _fetch_active_markets(self) -> List[Dict]:
        """Fetch all active markets."""
        try:
            async with self.session.get(
                f"{GAMMA_API}/markets",
                params={"closed": "false", "active": "true", "limit": "200"},
            ) as resp:
                if resp.status == 200:
                    return await resp.json()
                else:
                    print(f"  API returned status {resp.status}")
        except Exception as e:
            print(f"  ERROR fetching markets: {type(e).__name__}: {str(e)[:80]}")
        return []
    
    def _parse_market(self, m: Dict) -> Optional[MarketInfo]:
        """Parse market dict into MarketInfo."""
        try:
            import json
            
            toks = m.get("clobTokenIds", [])
            outs = m.get("outcomes", [])
            
            if isinstance(toks, str):
                toks = json.loads(toks)
            if isinstance(outs, str):
                outs = json.loads(outs)
            
            if len(toks) < 2 or len(outs) < 2:
                return None
            
            # Map outcomes to tokens
            token_map = {o.lower(): t for o, t in zip(outs, toks)}
            
            up_token = token_map.get("up") or token_map.get("yes")
            down_token = token_map.get("down") or token_map.get("no")
            
            return MarketInfo(
                market_id=m.get("id", ""),
                condition_id=m.get("conditionId", ""),
                slug=m.get("slug", ""),
                question=m.get("question", ""),
                up_token_id=up_token or "",
                down_token_id=down_token or "",
                outcomes=outs,
                closed=m.get("closed", False),
                active=m.get("active", True),
            )
        except:
            return None
    
    def _print_market(self, market: MarketInfo) -> None:
        """Print market details."""
        print(f"\n  Market ID:     {market.market_id}")
        print(f"  Condition ID:  {market.condition_id}")
        print(f"  Slug:          {market.slug}")
        print(f"  Question:      {market.question[:80]}")
        print(f"  Outcomes:      {market.outcomes}")
        print(f"  UP Token:      {market.up_token_id[:40]}...")
        print(f"  DOWN Token:    {market.down_token_id[:40]}...")
        print(f"  Closed:        {market.closed}")
        print(f"  Active:        {market.active}")
    
    async def check_prices(self, market: MarketInfo, seconds: int = 10) -> bool:
        """
        PART 2: Price check with BID/ASK
        
        Fetch orderbook bid/ask for specified duration and measure actual poll cadence.
        """
        self.print_header("PRICE CHECK (BID/ASK)")
        
        print(f"Fetching orderbook prices for {seconds}s at target {self.poll_interval}s interval...")
        print(f"UP Token:   {market.up_token_id[:30]}...")
        print(f"DOWN Token: {market.down_token_id[:30]}...")
        print()
        
        samples: List[PriceSample] = []
        start_time = time.time()
        last_sample_time = start_time
        tick = 0
        rate_limit_count = 0
        book_success_count = 0
        mid_fallback_count = 0
        
        print(f"{'Tick':>4} | {'Time':>12} | {'UP bid':>6} | {'UP ask':>6} | {'sp':>3} | "
              f"{'DN bid':>6} | {'DN ask':>6} | {'sp':>3} | {'dt':>5} | {'Src'}")
        print("-" * 95)
        
        while time.time() - start_time < seconds:
            tick_start = time.time()
            target_next = start_time + (tick + 1) * self.poll_interval
            
            # Fetch orderbook prices
            result = await self._fetch_book_pair(
                market.up_token_id, market.down_token_id
            )
            
            now = time.time()
            dt = now - last_sample_time
            
            sample = PriceSample(
                ts=now,
                up_bid=result['up_bid'],
                up_ask=result['up_ask'],
                up_mid=result['up_mid'],
                down_bid=result['down_bid'],
                down_ask=result['down_ask'],
                down_mid=result['down_mid'],
                dt=dt,
                source=result['source'],
            )
            samples.append(sample)
            
            # Track source types
            if result['source'] == "BOOK":
                book_success_count += 1
            elif result['source'] == "MID":
                mid_fallback_count += 1
            elif result['source'] == "429":
                rate_limit_count += 1
            
            # Print sample
            ts_str = datetime.fromtimestamp(now).strftime("%H:%M:%S.%f")[:-3]
            up_sp = result['up_ask'] - result['up_bid']
            dn_sp = result['down_ask'] - result['down_bid']
            print(f"{tick:4d} | {ts_str} | {result['up_bid']:5d}c | {result['up_ask']:5d}c | {up_sp:2d}c | "
                  f"{result['down_bid']:5d}c | {result['down_ask']:5d}c | {dn_sp:2d}c | {dt:4.2f}s | {result['source']}")
            
            last_sample_time = now
            tick += 1
            
            # Precise sleep to next target
            sleep_time = max(0, target_next - time.time())
            if sleep_time > 0:
                await asyncio.sleep(sleep_time)
        
        # Print summary
        print()
        print("-" * 95)
        
        if len(samples) > 1:
            dts = [s.dt for s in samples[1:]]  # Skip first (no previous)
            avg_dt = sum(dts) / len(dts)
            min_dt = min(dts)
            max_dt = max(dts)
            
            # Calculate average spreads
            up_spreads = [s.up_ask - s.up_bid for s in samples if s.up_ask > 0 and s.up_bid > 0]
            dn_spreads = [s.down_ask - s.down_bid for s in samples if s.down_ask > 0 and s.down_bid > 0]
            avg_up_sp = sum(up_spreads) / len(up_spreads) if up_spreads else 0
            avg_dn_sp = sum(dn_spreads) / len(dn_spreads) if dn_spreads else 0
            
            print(f"Samples collected:  {len(samples)}")
            print(f"Book success:       {book_success_count}/{len(samples)} ({100*book_success_count/len(samples):.0f}%)")
            print(f"Mid fallbacks:      {mid_fallback_count}")
            print(f"Rate limits (429):  {rate_limit_count}")
            print(f"")
            print(f"Avg interval:       {avg_dt:.3f}s (target: {self.poll_interval}s)")
            print(f"Min interval:       {min_dt:.3f}s")
            print(f"Max interval:       {max_dt:.3f}s")
            print(f"")
            print(f"Avg UP spread:      {avg_up_sp:.1f}c")
            print(f"Avg DOWN spread:    {avg_dn_sp:.1f}c")
            
            if avg_dt > self.poll_interval * 1.5:
                print(f"\n  WARNING: Polling {avg_dt/self.poll_interval:.1f}x slower than target!")
                print(f"  Likely cause: API latency or rate limiting")
            
            if rate_limit_count > 0:
                print(f"\n  SUGGESTION: Increase poll interval to avoid 429s")
                print(f"  Try: --poll 2.0 or --poll 3.0")
            
            if book_success_count == 0:
                print(f"\n  WARNING: No orderbook data fetched! Bid/ask not available.")
                print(f"  Paper mode will fall back to synthetic spreads from midpoint.")
                return False
            
            if book_success_count == len(samples):
                print(f"\n  ORDERBOOK MODE: FULLY AVAILABLE")
                print(f"  Entry triggers will use ASK, exits will use BID (realistic simulation)")
            
            return True
        
        return False
    
    async def _fetch_book(self, token_id: str) -> Dict:
        """Fetch orderbook for a single token."""
        try:
            async with self.session.get(
                f"{CLOB_HOST}/book",
                params={"token_id": token_id},
            ) as resp:
                if resp.status == 429:
                    return {"status": "429"}
                if resp.status == 200:
                    data = await resp.json()
                    
                    bids = data.get("bids", [])
                    asks = data.get("asks", [])
                    
                    best_bid = 0
                    if bids:
                        best_bid = int(float(bids[0].get("price", 0)) * 100)
                    
                    best_ask = 0
                    if asks:
                        best_ask = int(float(asks[0].get("price", 0)) * 100)
                    
                    mid = (best_bid + best_ask) // 2 if (best_bid and best_ask) else 0
                    
                    return {
                        "status": "OK",
                        "bid": best_bid,
                        "ask": best_ask,
                        "mid": mid,
                    }
                else:
                    return {"status": f"HTTP{resp.status}"}
        except Exception as e:
            return {"status": f"ERR:{type(e).__name__}"}
    
    async def _fetch_book_pair(
        self, up_token: str, down_token: str
    ) -> Dict:
        """Fetch orderbook bid/ask for UP and DOWN tokens."""
        # Fetch both books in parallel
        up_result, down_result = await asyncio.gather(
            self._fetch_book(up_token),
            self._fetch_book(down_token),
        )
        
        # Check for rate limiting
        if up_result.get("status") == "429" or down_result.get("status") == "429":
            return {
                "up_bid": 0, "up_ask": 0, "up_mid": 0,
                "down_bid": 0, "down_ask": 0, "down_mid": 0,
                "source": "429"
            }
        
        # Check if we got book data
        has_up_book = up_result.get("status") == "OK" and up_result.get("bid", 0) > 0
        has_down_book = down_result.get("status") == "OK" and down_result.get("bid", 0) > 0
        
        if has_up_book and has_down_book:
            return {
                "up_bid": up_result["bid"],
                "up_ask": up_result["ask"],
                "up_mid": up_result["mid"],
                "down_bid": down_result["bid"],
                "down_ask": down_result["ask"],
                "down_mid": down_result["mid"],
                "source": "BOOK"
            }
        
        # Fallback to midpoint
        up_mid = await self._fetch_midpoint(up_token)
        down_mid = await self._fetch_midpoint(down_token)
        
        # Synthetic spread (assume 2c)
        return {
            "up_bid": max(0, up_mid - 1),
            "up_ask": min(100, up_mid + 1),
            "up_mid": up_mid,
            "down_bid": max(0, down_mid - 1),
            "down_ask": min(100, down_mid + 1),
            "down_mid": down_mid,
            "source": "MID"
        }
    
    async def _fetch_midpoint(self, token_id: str) -> int:
        """Fetch midpoint for a token (fallback)."""
        try:
            async with self.session.get(
                f"{CLOB_HOST}/midpoint",
                params={"token_id": token_id},
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return int(float(data.get("mid", 0)) * 100)
        except:
            pass
        return 0
    
    async def check_window_timing(self) -> None:
        """
        PART 3: Window timing check
        
        Print current time and computed 15m boundaries.
        """
        self.print_header("WINDOW TIMING CHECK")
        
        now = time.time()
        now_utc = datetime.fromtimestamp(now, tz=timezone.utc)
        
        # Compute 15m boundaries
        start = int(now) - (int(now) % 900)
        end = start + 900
        secs_left = end - int(now)
        
        start_dt = datetime.fromtimestamp(start, tz=timezone.utc)
        end_dt = datetime.fromtimestamp(end, tz=timezone.utc)
        
        print(f"Current time (UTC):    {now_utc.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"Window start:          {start_dt.strftime('%Y-%m-%d %H:%M:%S')} ({start})")
        print(f"Window end:            {end_dt.strftime('%Y-%m-%d %H:%M:%S')} ({end})")
        print(f"Seconds remaining:     {secs_left}s ({secs_left//60}:{secs_left%60:02d})")
        print(f"Constructed slug:      btc-updown-15m-{start}")
    
    async def run(self, seconds: int = 10) -> int:
        """
        Run all checks.
        
        Returns:
            0 if all checks pass
            1 if discovery fails
            2 if price fetch fails
        """
        print("\n" + "="*60)
        print("  POLYMARKET API DOCTOR")
        print("="*60)
        print(f"\nTarget poll interval: {self.poll_interval}s")
        print(f"Price stream duration: {seconds}s")
        
        try:
            await self.connect()
            
            # Check 1: Discovery
            market = await self.check_discovery()
            if not market:
                print("\n" + "="*60)
                print("  RESULT: DISCOVERY FAILED")
                print("="*60)
                print("\nPossible causes:")
                print("  1. No BTC 15m market currently active")
                print("  2. Market uses different naming convention")
                print("  3. Network connectivity issue")
                print("\nNext steps:")
                print("  - Check Polymarket.com for active BTC 15m markets")
                print("  - Verify network can reach gamma-api.polymarket.com")
                return 1
            
            # Check 2: Prices
            await self.check_window_timing()
            price_ok = await self.check_prices(market, seconds)
            
            if not price_ok:
                print("\n" + "="*60)
                print("  RESULT: PRICE FETCH FAILED")
                print("="*60)
                return 2
            
            # Success
            print("\n" + "="*60)
            print("  RESULT: ALL CHECKS PASSED")
            print("="*60)
            print("\nDiscovered market tokens can be used for paper_mode.")
            print(f"Recommended poll interval: {self.poll_interval}s")
            return 0
        
        finally:
            await self.close()


def main():
    parser = argparse.ArgumentParser(
        description="Polymarket API Doctor - Validate connectivity"
    )
    parser.add_argument(
        "--poll", type=float, default=1.0,
        help="Target poll interval in seconds (default: 1.0)"
    )
    parser.add_argument(
        "--seconds", type=int, default=10,
        help="Duration for price stream test (default: 10)"
    )
    
    args = parser.parse_args()
    
    doctor = APIDoctor(poll_interval=args.poll)
    exit_code = asyncio.run(doctor.run(seconds=args.seconds))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()

