"""
Reward-Aware Market Maker (Variant R)

The REAL edge on Polymarket:
1. Liquidity Rewards - paid daily for posting resting limit orders
2. Spread capture - occasional fills at favorable prices
3. NOT pair-arb (that's structurally dead on efficient markets)

Key concepts from Polymarket docs:
- Rewards paid for quotes within max_incentive_spread of midpoint
- Two-sided quoting required (single-sided penalized)
- Size above min threshold counts more
- Near extremes (0-5c, 95c-100c) requires two-sided to count
"""

import logging
import time
import json
import math
import requests
import statistics
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional, List, Dict, Tuple
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"


@dataclass
class RewardConfig:
    """Reward configuration for a market."""
    market_slug: str
    rewards_daily_rate: float = 0.0  # USD per day allocated to this market
    max_spread: float = 0.02  # 2% max spread for rewards
    min_size: float = 5.0  # Minimum size for rewards
    is_eligible: bool = True


@dataclass
class QuoteScore:
    """Score for a posted quote (reward eligibility)."""
    side: str  # "bid" or "ask"
    price: float
    size: float
    distance_from_mid: float  # How far from midpoint
    within_max_spread: bool
    above_min_size: bool
    score: float  # Estimated relative reward score


@dataclass  
class InventoryState:
    """Current inventory position."""
    qty_yes: float = 0.0
    qty_no: float = 0.0
    cost_yes: float = 0.0
    cost_no: float = 0.0
    
    @property
    def net_position(self) -> float:
        """Positive = long YES, negative = long NO."""
        return self.qty_yes - self.qty_no
    
    @property
    def total_exposure(self) -> float:
        return self.cost_yes + self.cost_no
    
    @property
    def skew(self) -> float:
        """Normalized skew: -1 (all NO) to +1 (all YES)."""
        total = self.qty_yes + self.qty_no
        if total == 0:
            return 0
        return (self.qty_yes - self.qty_no) / total


@dataclass
class MMMetrics:
    """Metrics for reward-aware market making."""
    quotes_posted: int = 0
    time_quoted_seconds: float = 0.0
    fills_yes: int = 0
    fills_no: int = 0
    fill_volume: float = 0.0
    estimated_reward_score: float = 0.0
    trading_pnl: float = 0.0
    
    # Adverse selection tracking
    fills_that_moved_against: int = 0
    avg_adverse_move: float = 0.0


class RewardAwareMM:
    """
    Reward-aware market maker.
    
    Goal: Maximize liquidity reward yield while managing inventory risk.
    NOT trying to complete pairs - just providing liquidity.
    """
    
    def __init__(
        self,
        # Quote parameters
        target_spread: float = 0.01,  # 1% spread around mid
        quote_size: float = 10.0,
        max_spread: float = 0.02,  # Max spread for rewards eligibility
        
        # Inventory management
        max_inventory: float = 100.0,  # Max position one side
        skew_factor: float = 0.5,  # How much to skew quotes based on inventory
        
        # Risk controls
        stop_seconds_before_end: float = 30,  # Stop quoting near window end
        volatility_cancel_threshold: float = 0.05,  # Cancel if price moves 5%
        
        # Output
        output_dir: str = "pm_results_v4",
    ):
        self.target_spread = target_spread
        self.quote_size = quote_size
        self.max_spread = max_spread
        self.max_inventory = max_inventory
        self.skew_factor = skew_factor
        self.stop_seconds_before_end = stop_seconds_before_end
        self.volatility_cancel_threshold = volatility_cancel_threshold
        
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(exist_ok=True)
        
        self.session = requests.Session()
        
        # State
        self.inventory = InventoryState()
        self.metrics = MMMetrics()
        self.last_mid: Optional[float] = None
        self.quote_start_ts: Optional[float] = None
        
        # Log
        ts_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        self.log_path = self.output_dir / f"rewards_mm_{ts_str}.jsonl"
        self.log_file = None
    
    def _log(self, event: str, data: dict):
        if self.log_file:
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
    
    def _get_best_prices(self, book: dict) -> Tuple[float, float]:
        """Get best bid and ask from book."""
        bids = book.get("bids", [])
        asks = book.get("asks", [])
        
        best_bid = float(bids[0]["price"]) if bids else 0
        best_ask = float(asks[0]["price"]) if asks else 1
        
        return best_bid, best_ask
    
    def _compute_quotes(self, mid: float) -> Tuple[float, float]:
        """
        Compute bid and ask prices based on mid and inventory.
        
        Skews quotes based on current position to reduce inventory.
        """
        half_spread = self.target_spread / 2
        
        # Base quotes
        base_bid = mid - half_spread
        base_ask = mid + half_spread
        
        # Skew based on inventory
        # If long YES (skew > 0), lower bid, raise ask (discourage more YES)
        # If long NO (skew < 0), raise bid, lower ask (discourage more NO)
        skew = self.inventory.skew
        skew_adjustment = skew * self.skew_factor * half_spread
        
        bid = base_bid - skew_adjustment
        ask = base_ask - skew_adjustment  # Same direction - pushes both quotes
        
        # Clamp to valid range
        bid = max(0.01, min(0.99, bid))
        ask = max(0.01, min(0.99, ask))
        
        return bid, ask
    
    def _check_reward_eligibility(self, bid: float, ask: float, mid: float) -> dict:
        """Check if quotes are eligible for rewards."""
        spread = ask - bid
        bid_distance = mid - bid
        ask_distance = ask - mid
        
        within_spread = spread <= self.max_spread
        two_sided = bid > 0 and ask < 1
        
        # Simple score approximation
        # Real formula is more complex but this captures key factors
        if within_spread and two_sided:
            # Score inversely proportional to spread
            score = (self.max_spread - spread) / self.max_spread
        else:
            score = 0
        
        return {
            "spread": spread,
            "within_max_spread": within_spread,
            "two_sided": two_sided,
            "bid_distance": bid_distance,
            "ask_distance": ask_distance,
            "estimated_score": score,
        }
    
    def _simulate_fills(self, bid: float, ask: float, book_yes: dict, book_no: dict) -> List[dict]:
        """
        Simulate fills if market crosses our quotes.
        
        In paper mode, we assume fill if best ask <= our bid or best bid >= our ask.
        """
        fills = []
        
        # YES side
        best_ask_yes = float(book_yes.get("asks", [{"price": 1}])[0].get("price", 1))
        best_bid_yes = float(book_yes.get("bids", [{"price": 0}])[0].get("price", 0))
        
        # If best ask <= our bid, we get filled on YES (buying)
        if best_ask_yes <= bid:
            fills.append({
                "side": "yes",
                "direction": "buy",
                "price": bid,
                "size": self.quote_size,
            })
        
        # If best bid >= our ask, we get filled on YES (selling)
        if best_bid_yes >= ask:
            fills.append({
                "side": "yes",
                "direction": "sell",
                "price": ask,
                "size": self.quote_size,
            })
        
        return fills
    
    def _apply_fill(self, fill: dict):
        """Apply a fill to inventory and metrics."""
        side = fill["side"]
        direction = fill["direction"]
        price = fill["price"]
        size = fill["size"]
        
        if direction == "buy":
            if side == "yes":
                self.inventory.qty_yes += size
                self.inventory.cost_yes += size * price
                self.metrics.fills_yes += 1
            else:
                self.inventory.qty_no += size
                self.inventory.cost_no += size * price
                self.metrics.fills_no += 1
        else:  # sell
            if side == "yes":
                # Reduce position
                avg_cost = self.inventory.cost_yes / self.inventory.qty_yes if self.inventory.qty_yes > 0 else 0
                realized_pnl = (price - avg_cost) * min(size, self.inventory.qty_yes)
                self.metrics.trading_pnl += realized_pnl
                self.inventory.qty_yes = max(0, self.inventory.qty_yes - size)
            else:
                avg_cost = self.inventory.cost_no / self.inventory.qty_no if self.inventory.qty_no > 0 else 0
                realized_pnl = (price - avg_cost) * min(size, self.inventory.qty_no)
                self.metrics.trading_pnl += realized_pnl
                self.inventory.qty_no = max(0, self.inventory.qty_no - size)
        
        self.metrics.fill_volume += size * price
        
        self._log("FILL", fill)
    
    def run_paper_mm(self, token_id_yes: str, token_id_no: str, duration_minutes: float = 15):
        """
        Run paper market making simulation.
        
        Note: This is paper only - no actual order placement.
        Real implementation would use CLOB order APIs.
        """
        print("\n" + "=" * 70)
        print("REWARD-AWARE MARKET MAKER (Paper)")
        print("=" * 70)
        print(f"Target spread: {self.target_spread*100:.1f}%")
        print(f"Quote size: {self.quote_size}")
        print(f"Max inventory: {self.max_inventory}")
        print(f"Duration: {duration_minutes} minutes")
        print("=" * 70)
        
        self.log_file = open(self.log_path, "w", encoding="utf-8")
        
        start = time.time()
        deadline = start + duration_minutes * 60
        tick_count = 0
        
        try:
            while time.time() < deadline:
                tick_count += 1
                
                # Fetch books
                book_yes = self._fetch_book(token_id_yes)
                book_no = self._fetch_book(token_id_no)
                
                if not book_yes or not book_no:
                    time.sleep(0.5)
                    continue
                
                # Get best prices
                bid_yes, ask_yes = self._get_best_prices(book_yes)
                bid_no, ask_no = self._get_best_prices(book_no)
                
                if bid_yes == 0 or ask_yes == 1:
                    time.sleep(0.5)
                    continue
                
                # Compute mid
                mid = (bid_yes + ask_yes) / 2
                
                # Check for volatility spike
                if self.last_mid is not None:
                    move = abs(mid - self.last_mid)
                    if move > self.volatility_cancel_threshold:
                        logger.warning(f"Volatility spike: {move*100:.1f}% - canceling quotes")
                        self._log("VOLATILITY_CANCEL", {"move": move, "mid": mid})
                        self.last_mid = mid
                        time.sleep(1)
                        continue
                
                self.last_mid = mid
                
                # Compute our quotes
                our_bid, our_ask = self._compute_quotes(mid)
                
                # Check reward eligibility
                reward_info = self._check_reward_eligibility(our_bid, our_ask, mid)
                self.metrics.estimated_reward_score += reward_info["estimated_score"]
                
                # Simulate fills
                fills = self._simulate_fills(our_bid, our_ask, book_yes, book_no)
                for fill in fills:
                    self._apply_fill(fill)
                
                # Track quoting time
                if self.quote_start_ts is None:
                    self.quote_start_ts = time.time()
                self.metrics.time_quoted_seconds = time.time() - self.quote_start_ts
                self.metrics.quotes_posted = tick_count
                
                # Log periodically
                if tick_count % 50 == 0:
                    elapsed = (time.time() - start) / 60
                    print(f"[{elapsed:.1f}m] Mid: {mid:.4f} | Bid/Ask: {our_bid:.4f}/{our_ask:.4f} | "
                          f"Inventory: {self.inventory.net_position:.0f} | Fills: {self.metrics.fills_yes+self.metrics.fills_no}")
                    
                    self._log("STATUS", {
                        "mid": mid,
                        "our_bid": our_bid,
                        "our_ask": our_ask,
                        "inventory": self.inventory.net_position,
                        "reward_score": reward_info["estimated_score"],
                    })
                
                time.sleep(0.3)
        
        except KeyboardInterrupt:
            logger.info("Interrupted")
        finally:
            self.log_file.close()
        
        self._print_summary()
    
    def _print_summary(self):
        """Print summary."""
        print("\n" + "=" * 70)
        print("REWARD MM SUMMARY")
        print("=" * 70)
        print(f"Time quoted: {self.metrics.time_quoted_seconds/60:.1f} minutes")
        print(f"Ticks: {self.metrics.quotes_posted}")
        print(f"Fills YES: {self.metrics.fills_yes}")
        print(f"Fills NO: {self.metrics.fills_no}")
        print(f"Fill volume: ${self.metrics.fill_volume:.2f}")
        print(f"Trading P&L: ${self.metrics.trading_pnl:.2f}")
        print(f"Estimated reward score: {self.metrics.estimated_reward_score:.1f}")
        print(f"\nFinal inventory: YES={self.inventory.qty_yes:.0f}, NO={self.inventory.qty_no:.0f}")
        print(f"Net position: {self.inventory.net_position:.0f}")
        print(f"Total exposure: ${self.inventory.total_exposure:.2f}")
        
        # Estimate rewards (very rough)
        # Real formula depends on total market scores
        print("\n--- Reward Estimation (rough) ---")
        if self.metrics.time_quoted_seconds > 0:
            score_per_hour = self.metrics.estimated_reward_score / (self.metrics.time_quoted_seconds / 3600)
            print(f"Score/hour: {score_per_hour:.1f}")


class LateWindowScanner:
    """
    Late-window probability mispricing scanner.
    
    Looks for situations where:
    - Window is near end (e.g., <2 minutes)
    - True probability is very high (e.g., >95% based on price distance)
    - But contract trades at discount (e.g., 85-92c)
    
    This is NOT arb - it's probability trading with EV.
    """
    
    def __init__(
        self,
        min_ev_cents: float = 0.5,  # Minimum EV in cents to enter
        min_probability: float = 0.90,  # Only trade if estimated p > 90%
        max_price: float = 0.95,  # Don't pay more than 95c
        output_dir: str = "pm_results_v4",
    ):
        self.min_ev_cents = min_ev_cents
        self.min_probability = min_probability
        self.max_price = max_price
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(exist_ok=True)
        
        self.session = requests.Session()
        self.signals: List[dict] = []
        
        ts_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        self.log_path = self.output_dir / f"late_window_{ts_str}.jsonl"
    
    def _estimate_probability(
        self,
        current_price: float,
        price_to_beat: float,
        seconds_remaining: float,
        volatility: float = 0.01,  # Estimated per-second volatility
    ) -> float:
        """
        Estimate true probability of winning based on:
        - Current BTC price vs price to beat
        - Time remaining
        - Volatility
        
        Simple model: probability that price stays above/below threshold.
        More sophisticated would use actual options pricing.
        """
        if seconds_remaining <= 0:
            return 1.0 if current_price > price_to_beat else 0.0
        
        # Rough approximation using distance and time
        # This is a placeholder - real implementation needs actual BTC price data
        distance_pct = abs(current_price - price_to_beat) / price_to_beat
        
        # More time = more uncertainty = lower confidence
        # More distance = higher confidence
        time_factor = min(1.0, seconds_remaining / 300)  # Normalize to 5 min
        
        if current_price > price_to_beat:
            # Currently above - estimate probability of staying above
            base_prob = 0.5 + (distance_pct / volatility) * 0.1
        else:
            # Currently below
            base_prob = 0.5 - (distance_pct / volatility) * 0.1
        
        # Adjust for time - less time = more certain
        adjusted_prob = base_prob + (1 - time_factor) * (1 - base_prob) * 0.5
        
        return max(0.01, min(0.99, adjusted_prob))
    
    def _check_signal(
        self,
        ask_price: float,
        estimated_prob: float,
    ) -> Optional[dict]:
        """Check if there's a mispricing signal."""
        if estimated_prob < self.min_probability:
            return None
        
        if ask_price > self.max_price:
            return None
        
        # EV = probability * $1 - price
        ev = estimated_prob - ask_price
        ev_cents = ev * 100
        
        if ev_cents < self.min_ev_cents:
            return None
        
        return {
            "ask_price": ask_price,
            "estimated_prob": estimated_prob,
            "ev": ev,
            "ev_cents": ev_cents,
        }
    
    def scan_window(
        self,
        token_id: str,
        seconds_remaining: float,
        current_btc_price: float = 0,  # Would need real data
        price_to_beat: float = 0,
    ):
        """Scan a window for late-window mispricing."""
        try:
            url = f"{CLOB_API}/book?token_id={token_id}"
            resp = self.session.get(url, timeout=5)
            resp.raise_for_status()
            book = resp.json()
            
            asks = book.get("asks", [])
            if not asks:
                return None
            
            ask_price = float(asks[0]["price"])
            
            # Estimate probability (simplified - needs real BTC data)
            # For now, use the contract price itself as a proxy
            estimated_prob = self._estimate_probability(
                current_btc_price or ask_price,
                price_to_beat or 0.5,
                seconds_remaining,
            )
            
            signal = self._check_signal(ask_price, estimated_prob)
            if signal:
                signal["token_id"] = token_id
                signal["seconds_remaining"] = seconds_remaining
                self.signals.append(signal)
                
                print(f"🎯 SIGNAL: ask={ask_price:.4f}, prob={estimated_prob*100:.1f}%, "
                      f"EV={signal['ev_cents']:.2f}c, time={seconds_remaining:.0f}s")
            
            return signal
            
        except Exception as e:
            logger.debug(f"Error scanning: {e}")
            return None


def fetch_reward_eligible_markets() -> List[dict]:
    """
    Fetch markets eligible for liquidity rewards.
    
    Note: The actual rewards API may require authentication.
    This is a placeholder that fetches active markets.
    """
    try:
        url = f"{GAMMA_API}/markets?active=true&closed=false&limit=100"
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        markets = resp.json()
        
        # Filter for likely reward-eligible (active, has liquidity)
        eligible = []
        for m in markets:
            volume = float(m.get("volume", 0) or 0)
            liquidity = float(m.get("liquidity", 0) or 0)
            
            if volume > 10000 and liquidity > 1000:
                eligible.append({
                    "slug": m.get("slug"),
                    "question": m.get("question"),
                    "volume": volume,
                    "liquidity": liquidity,
                })
        
        return eligible
        
    except Exception as e:
        logger.error(f"Error fetching markets: {e}")
        return []


def main():
    import argparse
    
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%H:%M:%S"
    )
    
    parser = argparse.ArgumentParser(description="Reward-Aware Market Maker")
    parser.add_argument("--list-eligible", action="store_true", help="List reward-eligible markets")
    parser.add_argument("--run-mm", action="store_true", help="Run paper MM on BTC 15m")
    parser.add_argument("--duration", type=float, default=15, help="Duration in minutes")
    
    args = parser.parse_args()
    
    if args.list_eligible:
        print("\n" + "=" * 70)
        print("REWARD-ELIGIBLE MARKETS (estimated)")
        print("=" * 70)
        
        markets = fetch_reward_eligible_markets()
        markets.sort(key=lambda x: x["volume"], reverse=True)
        
        print(f"\nFound {len(markets)} likely eligible markets:\n")
        print(f"{'Volume':<15} {'Liquidity':<12} {'Slug':<50}")
        print("-" * 80)
        
        for m in markets[:30]:
            print(f"${m['volume']:>12,.0f} ${m['liquidity']:>9,.0f}  {m['slug'][:50]}")
    
    elif args.run_mm:
        # For demo, use a sample BTC 15m market
        # In real use, fetch current window tokens
        from .market_v2 import get_current_window_slug, MarketFetcher
        
        fetcher = MarketFetcher()
        slug = get_current_window_slug()
        window = fetcher.fetch_market_by_slug(slug)
        
        if window:
            print(f"Running MM on: {slug}")
            mm = RewardAwareMM()
            mm.run_paper_mm(
                token_id_yes=window.up_token_id,
                token_id_no=window.down_token_id,
                duration_minutes=args.duration,
            )
        else:
            print("Could not fetch market")
    
    else:
        print("Usage:")
        print("  List eligible:  python -m pm_15m_arb.rewards_mm --list-eligible")
        print("  Run paper MM:   python -m pm_15m_arb.rewards_mm --run-mm --duration 15")


if __name__ == "__main__":
    main()

