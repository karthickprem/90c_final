"""
Multi-Market Market Maker with Markout Tracking

Runs MM on multiple markets simultaneously, tracking:
- Reward score earned per market
- Markout toxicity (mid movement after fills)
- Net P&L = rewards - markout_loss - inventory_slippage

This produces the TRUE profitability metric: net_est_usdc_day
"""

import logging
import time
import json
import threading
import requests
import statistics
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional, List, Dict, Tuple
from dataclasses import dataclass, field
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

from .rewards_api import RewardsAPI, MarketRewardConfig, MarkoutTracker, MarkoutRecord

logger = logging.getLogger(__name__)

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"


@dataclass
class MMState:
    """State for a single market's MM."""
    config: MarketRewardConfig
    
    # Inventory
    qty_outcome: Dict[int, float] = field(default_factory=dict)  # outcome_idx -> qty
    cost_outcome: Dict[int, float] = field(default_factory=dict)
    
    # Scores and metrics
    score_earned: float = 0.0
    time_quoted_s: float = 0.0
    ticks: int = 0
    
    # Fills
    fills: List[dict] = field(default_factory=list)
    fill_count: int = 0
    fill_volume: float = 0.0
    
    # Markout
    markout_tracker: MarkoutTracker = field(default_factory=MarkoutTracker)
    
    # Error tracking
    errors: int = 0
    last_error: str = ""
    
    @property
    def net_position(self) -> float:
        """Net position across all outcomes (should stay near 0 for MM)."""
        return sum(self.qty_outcome.values())
    
    @property
    def total_exposure(self) -> float:
        return sum(self.cost_outcome.values())


@dataclass
class MMConfig:
    """Configuration for multi-market MM."""
    # Quoting params
    target_spread: float = 0.015  # 1.5% target spread
    quote_size: float = 10.0  # $10 per quote
    max_spread: float = 0.02  # Max spread for reward eligibility
    
    # Inventory limits
    max_inventory_per_market: float = 100.0
    max_global_inventory: float = 500.0
    
    # Risk controls
    volatility_cancel_threshold: float = 0.03  # 3% move = cancel
    max_markets: int = 10
    
    # Timing
    quote_interval_ms: int = 500
    duration_minutes: float = 60


class MultiMarketMM:
    """
    Runs market making on multiple markets with full accounting.
    """
    
    def __init__(self, config: MMConfig = None, output_dir: str = "pm_results_v4"):
        self.config = config or MMConfig()
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(exist_ok=True)
        
        self.session = requests.Session()
        self.rewards_api = RewardsAPI(output_dir)
        
        # State per market
        self.states: Dict[str, MMState] = {}
        
        # Global tracking
        self.global_score = 0.0
        self.global_fills = 0
        self.global_volume = 0.0
        self.start_time: Optional[float] = None
        
        # Logging
        ts_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        self.log_path = self.output_dir / f"multi_mm_{ts_str}.jsonl"
        self.log_file = None
        
        # Thread safety
        self.lock = threading.Lock()
    
    def _log(self, event: str, data: dict):
        """Thread-safe logging."""
        if self.log_file:
            with self.lock:
                record = {"ts": time.time(), "event": event, **data}
                self.log_file.write(json.dumps(record) + "\n")
                self.log_file.flush()
    
    def _fetch_book(self, token_id: str) -> Optional[dict]:
        """Fetch orderbook."""
        try:
            url = f"{CLOB_API}/book?token_id={token_id}"
            resp = self.session.get(url, timeout=5)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.debug(f"Error fetching book: {e}")
            return None
    
    def _get_best_prices(self, book: dict) -> Tuple[float, float, float, float]:
        """Get best bid/ask and their sizes."""
        bids = book.get("bids", [])
        asks = book.get("asks", [])
        
        best_bid = float(bids[0]["price"]) if bids else 0
        best_ask = float(asks[0]["price"]) if asks else 1
        bid_size = float(bids[0]["size"]) if bids else 0
        ask_size = float(asks[0]["size"]) if asks else 0
        
        return best_bid, best_ask, bid_size, ask_size
    
    def _compute_quotes(self, mid: float, state: MMState) -> Tuple[float, float]:
        """Compute bid/ask based on mid and inventory skew."""
        half_spread = self.config.target_spread / 2
        
        # Base quotes
        base_bid = mid - half_spread
        base_ask = mid + half_spread
        
        # Skew based on inventory
        total_qty = sum(state.qty_outcome.values())
        if total_qty != 0:
            max_qty = sum(abs(q) for q in state.qty_outcome.values())
            if max_qty > 0:
                skew = total_qty / max_qty
                skew_adj = skew * 0.3 * half_spread
                base_bid -= skew_adj
                base_ask -= skew_adj
        
        return max(0.01, min(0.99, base_bid)), max(0.01, min(0.99, base_ask))
    
    def _compute_score(self, bid: float, ask: float, mid: float, size: float) -> float:
        """Compute reward score for posted quotes."""
        spread = ask - bid
        if spread > self.config.max_spread:
            return 0
        
        # Score = size * (1 - spread/max_spread) * two_sided_bonus
        score = size * (1 - spread / self.config.max_spread) * 1.5
        return score
    
    def _check_fills(
        self,
        state: MMState,
        our_bid: float,
        our_ask: float,
        book: dict,
        outcome_idx: int,
    ) -> List[dict]:
        """Check for paper fills."""
        fills = []
        
        bids = book.get("bids", [])
        asks = book.get("asks", [])
        
        if not bids or not asks:
            return fills
        
        best_bid = float(bids[0]["price"])
        best_ask = float(asks[0]["price"])
        mid = (best_bid + best_ask) / 2
        
        # If best ask <= our bid, we buy
        if best_ask <= our_bid and best_ask > 0:
            fills.append({
                "side": "buy",
                "outcome": outcome_idx,
                "price": our_bid,
                "size": self.config.quote_size,
                "mid_at_fill": mid,
            })
        
        # If best bid >= our ask, we sell
        if best_bid >= our_ask and best_bid < 1:
            fills.append({
                "side": "sell",
                "outcome": outcome_idx,
                "price": our_ask,
                "size": self.config.quote_size,
                "mid_at_fill": mid,
            })
        
        return fills
    
    def _apply_fill(self, state: MMState, fill: dict):
        """Apply fill to state and record for markout."""
        outcome = fill["outcome"]
        side = fill["side"]
        price = fill["price"]
        size = fill["size"]
        mid = fill["mid_at_fill"]
        
        if outcome not in state.qty_outcome:
            state.qty_outcome[outcome] = 0
            state.cost_outcome[outcome] = 0
        
        if side == "buy":
            state.qty_outcome[outcome] += size
            state.cost_outcome[outcome] += size * price
        else:
            state.qty_outcome[outcome] -= size
            state.cost_outcome[outcome] -= size * price
        
        state.fill_count += 1
        state.fill_volume += size * price
        state.fills.append(fill)
        
        # Record for markout tracking
        state.markout_tracker.record_fill(
            fill_price=price,
            side=side,
            size=size,
            mid_at_fill=mid,
        )
        
        self._log("FILL", {
            "slug": state.config.slug,
            **fill,
        })
    
    def _run_market(self, state: MMState, deadline: float):
        """Run MM loop for a single market."""
        token_ids = state.config.token_ids
        last_mids: Dict[int, float] = {}
        
        while time.time() < deadline:
            try:
                all_books = {}
                for i, token_id in enumerate(token_ids):
                    book = self._fetch_book(token_id)
                    if book:
                        all_books[i] = book
                
                if len(all_books) < 2:
                    time.sleep(0.5)
                    continue
                
                # Process each outcome
                for outcome_idx, book in all_books.items():
                    best_bid, best_ask, _, _ = self._get_best_prices(book)
                    
                    if best_bid <= 0 or best_ask >= 1:
                        continue
                    
                    mid = (best_bid + best_ask) / 2
                    
                    # Check volatility
                    if outcome_idx in last_mids:
                        move = abs(mid - last_mids[outcome_idx])
                        if move > self.config.volatility_cancel_threshold:
                            state.errors += 1
                            state.last_error = f"Volatility spike: {move:.2%}"
                            continue
                    
                    last_mids[outcome_idx] = mid
                    
                    # Update markout tracker
                    state.markout_tracker.update_mid(mid)
                    
                    # Compute quotes
                    our_bid, our_ask = self._compute_quotes(mid, state)
                    
                    # Compute score
                    score = self._compute_score(
                        our_bid, our_ask, mid, 
                        self.config.quote_size * 2  # Both sides
                    )
                    state.score_earned += score
                    
                    # Check fills
                    fills = self._check_fills(state, our_bid, our_ask, book, outcome_idx)
                    for fill in fills:
                        self._apply_fill(state, fill)
                
                state.ticks += 1
                state.time_quoted_s += self.config.quote_interval_ms / 1000
                
            except Exception as e:
                state.errors += 1
                state.last_error = str(e)
            
            time.sleep(self.config.quote_interval_ms / 1000)
    
    def run(self, markets: List[MarketRewardConfig], duration_minutes: float = None):
        """
        Run MM on multiple markets.
        """
        duration = duration_minutes or self.config.duration_minutes
        
        print("\n" + "=" * 80)
        print("MULTI-MARKET MAKER (Paper)")
        print("=" * 80)
        print(f"Markets: {len(markets)}")
        print(f"Duration: {duration} minutes")
        print(f"Quote size: ${self.config.quote_size}")
        print(f"Target spread: {self.config.target_spread*100:.1f}%")
        print("=" * 80)
        
        # Initialize states
        for config in markets[:self.config.max_markets]:
            self.states[config.slug] = MMState(config=config)
        
        self.log_file = open(self.log_path, "w", encoding="utf-8")
        self.start_time = time.time()
        deadline = self.start_time + duration * 60
        
        try:
            # Run each market in a thread
            with ThreadPoolExecutor(max_workers=len(self.states)) as executor:
                futures = {
                    executor.submit(self._run_market, state, deadline): slug
                    for slug, state in self.states.items()
                }
                
                # Monitor progress
                while time.time() < deadline:
                    elapsed = (time.time() - self.start_time) / 60
                    total_fills = sum(s.fill_count for s in self.states.values())
                    total_score = sum(s.score_earned for s in self.states.values())
                    
                    print(f"\r[{elapsed:.1f}m] Fills: {total_fills} | Score: {total_score:.0f} | "
                          f"Markets active: {len(self.states)}", end="")
                    
                    time.sleep(10)
                
                print()  # Newline after progress
                
                # Wait for threads
                for future in as_completed(futures):
                    slug = futures[future]
                    try:
                        future.result()
                    except Exception as e:
                        logger.error(f"Market {slug} error: {e}")
        
        except KeyboardInterrupt:
            logger.info("Interrupted")
        finally:
            self.log_file.close()
        
        self._print_summary()
        self._save_results()
    
    def _print_summary(self):
        """Print comprehensive summary."""
        elapsed_hours = (time.time() - self.start_time) / 3600 if self.start_time else 0
        
        print("\n" + "=" * 100)
        print("MULTI-MARKET MM SUMMARY")
        print("=" * 100)
        
        print(f"\n{'Market':<40} {'Fills':<8} {'Score':<10} {'Yield/day':<12} "
              f"{'Markout':<10} {'Net/day':<12}")
        print("-" * 100)
        
        total_yield = 0
        total_markout = 0
        total_fills = 0
        
        for slug, state in sorted(self.states.items(), 
                                   key=lambda x: x[1].score_earned, 
                                   reverse=True):
            # Compute yields
            score_per_hour = state.score_earned / elapsed_hours if elapsed_hours > 0 else 0
            
            # Estimate yield using score share
            if state.config.total_score_est > 0:
                share = score_per_hour / state.config.total_score_est
            else:
                share = 0.01  # Assume 1% share if unknown
            
            yield_per_day = state.config.pool_usdc_day * share
            
            # Get markout stats
            markout_stats = state.markout_tracker.get_markout_stats()
            markout_avg_bps = markout_stats.get("markout_30s_avg_bps", 0)
            
            # Estimate daily markout loss
            fills_per_day = (state.fill_count / elapsed_hours * 24) if elapsed_hours > 0 else 0
            markout_loss_day = fills_per_day * self.config.quote_size * (markout_avg_bps / 10000)
            
            net_per_day = yield_per_day - markout_loss_day
            
            total_yield += yield_per_day
            total_markout += markout_loss_day
            total_fills += state.fill_count
            
            print(f"{slug[:40]:<40} {state.fill_count:<8} {state.score_earned:<10.0f} "
                  f"${yield_per_day:<10.2f} ${markout_loss_day:<8.2f} ${net_per_day:<10.2f}")
        
        print("-" * 100)
        print(f"{'TOTAL':<40} {total_fills:<8} {sum(s.score_earned for s in self.states.values()):<10.0f} "
              f"${total_yield:<10.2f} ${total_markout:<8.2f} ${total_yield - total_markout:<10.2f}")
        
        print("\n--- Markout Analysis ---")
        all_markouts = []
        for state in self.states.values():
            all_markouts.extend(state.markout_tracker.completed_markouts)
        
        if all_markouts:
            markouts_30s = [m.markout_30s_bps for m in all_markouts]
            print(f"Total fills with markout data: {len(all_markouts)}")
            print(f"Markout 30s avg: {statistics.mean(markouts_30s):.1f} bps")
            print(f"Markout 30s median: {statistics.median(markouts_30s):.1f} bps")
            if len(markouts_30s) > 1:
                print(f"Markout 30s p90: {sorted(markouts_30s)[int(len(markouts_30s)*0.9)]:.1f} bps")
        else:
            print("No fills with markout data yet")
        
        print(f"\n⚠️  Net estimate: ${total_yield - total_markout:.2f}/day")
        print(f"    (requires more fills and time for accuracy)")
    
    def _save_results(self):
        """Save detailed results."""
        ts_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        path = self.output_dir / f"multi_mm_results_{ts_str}.json"
        
        elapsed_hours = (time.time() - self.start_time) / 3600 if self.start_time else 0
        
        results = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "duration_hours": elapsed_hours,
            "config": {
                "target_spread": self.config.target_spread,
                "quote_size": self.config.quote_size,
                "max_spread": self.config.max_spread,
            },
            "markets": {},
        }
        
        for slug, state in self.states.items():
            markout_stats = state.markout_tracker.get_markout_stats()
            results["markets"][slug] = {
                "ticks": state.ticks,
                "fills": state.fill_count,
                "fill_volume": state.fill_volume,
                "score_earned": state.score_earned,
                "inventory": dict(state.qty_outcome),
                "markout": markout_stats,
                "errors": state.errors,
            }
        
        with open(path, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2)
        
        print(f"\nResults saved: {path}")


def main():
    import argparse
    
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%H:%M:%S"
    )
    
    parser = argparse.ArgumentParser(description="Multi-Market MM")
    parser.add_argument("--run-mm", action="store_true", help="Run multi-market MM")
    parser.add_argument("--markets", type=int, default=5, help="Number of markets")
    parser.add_argument("--duration", type=float, default=60, help="Duration in minutes")
    parser.add_argument("--budget", type=float, default=100, help="Quote budget per market")
    
    args = parser.parse_args()
    
    if args.run_mm:
        # First build market table
        api = RewardsAPI()
        print("Building market table...")
        markets = api.build_market_table(top_n=args.markets * 2)
        
        if not markets:
            print("No markets found")
            return
        
        # Select top markets by net yield
        top_markets = sorted(markets, key=lambda x: x.net_est_usdc_day, reverse=True)[:args.markets]
        
        print(f"\nSelected {len(top_markets)} markets:")
        for m in top_markets:
            print(f"  {m.slug[:50]} (est net: ${m.net_est_usdc_day:.2f}/day)")
        
        # Run MM
        config = MMConfig(
            quote_size=args.budget / len(top_markets),
            max_markets=args.markets,
            duration_minutes=args.duration,
        )
        
        mm = MultiMarketMM(config=config)
        mm.run(top_markets, duration_minutes=args.duration)
    
    else:
        print("Usage:")
        print("  Run MM: python -m pm_15m_arb.multi_market_mm --run-mm --markets 5 --duration 120")


if __name__ == "__main__":
    main()

