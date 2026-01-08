"""
Rewards API - Fetch real Polymarket reward economics

Converts "score/hour" into "expected $/day net of adverse selection"

Key metrics:
- pool_usdc_day: Total rewards allocated to market per day
- total_market_score: Sum of all participants' scores
- your_share = your_score / total_market_score
- expected_yield = pool_usdc_day * your_share
- net_est = expected_yield - markout_loss - inventory_slippage
"""

import logging
import time
import json
import requests
import statistics
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional, List, Dict, Tuple
from dataclasses import dataclass, field
from collections import defaultdict

logger = logging.getLogger(__name__)

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

# Polymarket reward constants (from docs, subject to change)
# https://docs.polymarket.com/#liquidity-rewards
DEFAULT_MAX_SPREAD = 0.02  # 2% max spread for rewards
DEFAULT_MIN_SIZE = 5.0  # $5 minimum size
TWO_SIDED_BONUS = 1.5  # Two-sided quoting gets 1.5x score
NEAR_EXTREME_PENALTY = 0.5  # Near 0/100 gets 0.5x unless two-sided


@dataclass
class MarketRewardConfig:
    """Reward configuration for a single market."""
    slug: str
    condition_id: str
    token_ids: List[str]
    question: str
    
    # Reward params (fetched or estimated)
    pool_usdc_day: float = 0.0  # Total daily rewards for this market
    max_spread: float = DEFAULT_MAX_SPREAD
    min_size: float = DEFAULT_MIN_SIZE
    
    # Estimated competition
    total_score_est: float = 0.0  # Estimated total market score
    your_score_per_hour: float = 0.0
    
    # Book stats
    spread_p50: float = 0.0
    spread_p90: float = 0.0
    liquidity_top_level: float = 0.0
    
    # Toxicity estimates
    markout_avg_bps: float = 0.0  # Average adverse selection in bps
    fill_rate_per_hour: float = 0.0
    
    @property
    def your_share_est(self) -> float:
        """Estimated share of rewards (0-1)."""
        if self.total_score_est <= 0:
            # If we can't estimate competition, assume we'd capture 1-5%
            return 0.02  # Conservative 2% share
        return self.your_score_per_hour / self.total_score_est
    
    @property
    def yield_usdc_day(self) -> float:
        """Estimated daily yield from rewards."""
        return self.pool_usdc_day * self.your_share_est
    
    @property
    def markout_loss_day(self) -> float:
        """Estimated daily loss from adverse selection."""
        # Rough: fills per day * avg notional * markout_bps
        fills_per_day = self.fill_rate_per_hour * 24
        avg_fill_notional = self.min_size  # Assume min size fills
        return fills_per_day * avg_fill_notional * (self.markout_avg_bps / 10000)
    
    @property
    def net_est_usdc_day(self) -> float:
        """Net estimated daily P&L."""
        return self.yield_usdc_day - self.markout_loss_day
    
    def to_dict(self) -> dict:
        return {
            "slug": self.slug,
            "pool_usdc_day": self.pool_usdc_day,
            "max_spread": self.max_spread,
            "min_size": self.min_size,
            "total_score_est": self.total_score_est,
            "your_score_per_hour": self.your_score_per_hour,
            "your_share_est": self.your_share_est,
            "yield_usdc_day": self.yield_usdc_day,
            "markout_avg_bps": self.markout_avg_bps,
            "markout_loss_day": self.markout_loss_day,
            "net_est_usdc_day": self.net_est_usdc_day,
            "spread_p50": self.spread_p50,
            "liquidity_top_level": self.liquidity_top_level,
        }


@dataclass
class MarkoutRecord:
    """Tracking for post-fill price movement (toxicity)."""
    fill_ts: float
    fill_price: float
    side: str  # "buy" or "sell"
    size: float
    mid_at_fill: float
    
    # Recorded later
    mid_1s: float = 0.0
    mid_5s: float = 0.0
    mid_30s: float = 0.0
    mid_120s: float = 0.0
    
    @property
    def markout_1s_bps(self) -> float:
        """Markout in bps after 1 second."""
        if self.mid_1s == 0 or self.mid_at_fill == 0:
            return 0
        move = (self.mid_1s - self.fill_price) / self.fill_price
        # If we bought and price went down, that's adverse
        sign = 1 if self.side == "buy" else -1
        return move * sign * 10000
    
    @property
    def markout_5s_bps(self) -> float:
        if self.mid_5s == 0 or self.mid_at_fill == 0:
            return 0
        move = (self.mid_5s - self.fill_price) / self.fill_price
        sign = 1 if self.side == "buy" else -1
        return move * sign * 10000
    
    @property
    def markout_30s_bps(self) -> float:
        if self.mid_30s == 0 or self.mid_at_fill == 0:
            return 0
        move = (self.mid_30s - self.fill_price) / self.fill_price
        sign = 1 if self.side == "buy" else -1
        return move * sign * 10000
    
    def to_dict(self) -> dict:
        return {
            "fill_ts": self.fill_ts,
            "fill_price": self.fill_price,
            "side": self.side,
            "size": self.size,
            "mid_at_fill": self.mid_at_fill,
            "mid_1s": self.mid_1s,
            "mid_5s": self.mid_5s,
            "mid_30s": self.mid_30s,
            "mid_120s": self.mid_120s,
            "markout_1s_bps": self.markout_1s_bps,
            "markout_5s_bps": self.markout_5s_bps,
            "markout_30s_bps": self.markout_30s_bps,
        }


class RewardsAPI:
    """
    Fetches and estimates Polymarket reward economics.
    """
    
    def __init__(self, output_dir: str = "pm_results_v4"):
        self.session = requests.Session()
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(exist_ok=True)
        
        self.markets: Dict[str, MarketRewardConfig] = {}
    
    def _get(self, url: str, params: dict = None) -> Optional[dict]:
        """Safe GET request."""
        try:
            resp = self.session.get(url, params=params, timeout=10)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.debug(f"GET {url} failed: {e}")
            return None
    
    def fetch_rewards_config(self) -> List[dict]:
        """
        Fetch rewards configuration from Polymarket.
        
        Note: The actual rewards API endpoint may require authentication
        or may not be publicly documented. This attempts known endpoints.
        """
        configs = []
        
        # Try the rewards endpoint (may not be public)
        rewards_data = self._get(f"{CLOB_API}/rewards")
        if rewards_data:
            logger.info("Found rewards endpoint data")
            return rewards_data if isinstance(rewards_data, list) else [rewards_data]
        
        # Try the markets endpoint with rewards info
        markets_data = self._get(f"{GAMMA_API}/markets?active=true&closed=false&limit=200")
        if markets_data:
            for m in markets_data:
                # Check for reward-related fields
                config = {
                    "slug": m.get("slug"),
                    "condition_id": m.get("conditionId"),
                    "rewards_daily_rate": m.get("rewardsDailyRate", 0),
                    "rewards_max_spread": m.get("rewardsMaxSpread", DEFAULT_MAX_SPREAD),
                    "rewards_min_size": m.get("rewardsMinSize", DEFAULT_MIN_SIZE),
                    "volume": float(m.get("volume", 0) or 0),
                    "liquidity": float(m.get("liquidity", 0) or 0),
                }
                if config["volume"] > 0:
                    configs.append(config)
        
        return configs
    
    def estimate_pool_size(self, market_volume: float, market_liquidity: float) -> float:
        """
        Estimate daily reward pool for a market.
        
        This is a rough heuristic based on Polymarket's stated allocation model.
        Actual pools vary by market and are set by Polymarket.
        
        From docs: rewards are distributed proportionally to liquidity provision.
        Higher volume/liquidity markets typically get larger pools.
        """
        # Base estimate: larger markets get more rewards
        # This is a placeholder - real pools are set by Polymarket
        
        if market_liquidity >= 1_000_000:
            return 500  # Top tier: ~$500/day
        elif market_liquidity >= 100_000:
            return 100  # Mid tier: ~$100/day
        elif market_liquidity >= 10_000:
            return 25   # Lower tier: ~$25/day
        else:
            return 5    # Minimal: ~$5/day
    
    def estimate_total_score_from_book(
        self,
        token_ids: List[str],
        sample_count: int = 20,
    ) -> Tuple[float, float, float]:
        """
        Estimate total market score by sampling visible liquidity.
        
        Logic:
        - Fetch book for each outcome
        - Compute "score contribution" of visible orders that meet eligibility
        - Sum across both sides
        
        Returns: (total_score_est, spread_p50, top_liquidity)
        """
        spreads = []
        scores = []
        liquidities = []
        
        for _ in range(sample_count):
            total_score = 0
            for token_id in token_ids:
                book = self._get(f"{CLOB_API}/book", {"token_id": token_id})
                if not book:
                    continue
                
                bids = book.get("bids", [])
                asks = book.get("asks", [])
                
                if not bids or not asks:
                    continue
                
                best_bid = float(bids[0].get("price", 0))
                best_ask = float(asks[0].get("price", 0))
                
                if best_bid <= 0 or best_ask <= 0:
                    continue
                
                spread = best_ask - best_bid
                spreads.append(spread)
                
                mid = (best_bid + best_ask) / 2
                
                # Score visible liquidity
                for level in bids[:5]:  # Top 5 levels
                    price = float(level.get("price", 0))
                    size = float(level.get("size", 0))
                    
                    if size < DEFAULT_MIN_SIZE:
                        continue
                    
                    distance_from_mid = abs(mid - price)
                    if distance_from_mid > DEFAULT_MAX_SPREAD / 2:
                        continue  # Outside reward zone
                    
                    # Score = size * (1 - distance/max_distance)
                    score = size * (1 - distance_from_mid / (DEFAULT_MAX_SPREAD / 2))
                    total_score += score
                    liquidities.append(size)
                
                for level in asks[:5]:
                    price = float(level.get("price", 0))
                    size = float(level.get("size", 0))
                    
                    if size < DEFAULT_MIN_SIZE:
                        continue
                    
                    distance_from_mid = abs(price - mid)
                    if distance_from_mid > DEFAULT_MAX_SPREAD / 2:
                        continue
                    
                    score = size * (1 - distance_from_mid / (DEFAULT_MAX_SPREAD / 2))
                    total_score += score
                    liquidities.append(size)
            
            if total_score > 0:
                scores.append(total_score)
            
            time.sleep(0.1)
        
        avg_score = statistics.mean(scores) if scores else 0
        spread_p50 = statistics.median(spreads) if spreads else 0
        top_liq = statistics.mean(liquidities) if liquidities else 0
        
        return avg_score, spread_p50, top_liq
    
    def build_market_table(self, top_n: int = 50) -> List[MarketRewardConfig]:
        """
        Build ranked table of markets by estimated net yield.
        """
        logger.info("Fetching market list...")
        
        # Fetch all markets
        markets_raw = self._get(f"{GAMMA_API}/markets?active=true&closed=false&limit=200")
        if not markets_raw:
            logger.error("Failed to fetch markets")
            return []
        
        # Filter and parse
        markets = []
        for m in markets_raw:
            volume = float(m.get("volume", 0) or 0)
            liquidity = float(m.get("liquidity", 0) or 0)
            
            if volume < 5000 or liquidity < 1000:
                continue  # Skip tiny markets
            
            # Parse token IDs
            tokens_raw = m.get("clobTokenIds", [])
            if isinstance(tokens_raw, str):
                try:
                    token_ids = json.loads(tokens_raw)
                except:
                    continue
            else:
                token_ids = tokens_raw or []
            
            if len(token_ids) < 2:
                continue
            
            config = MarketRewardConfig(
                slug=m.get("slug", ""),
                condition_id=m.get("conditionId", ""),
                token_ids=token_ids,
                question=m.get("question", "")[:80],
                pool_usdc_day=self.estimate_pool_size(volume, liquidity),
            )
            markets.append(config)
        
        logger.info(f"Found {len(markets)} eligible markets")
        
        # Sample top markets to estimate scores
        markets.sort(key=lambda x: x.pool_usdc_day, reverse=True)
        
        for i, config in enumerate(markets[:top_n]):
            logger.info(f"[{i+1}/{min(top_n, len(markets))}] Sampling: {config.slug[:40]}...")
            
            # Estimate total score
            total_score, spread_p50, liq = self.estimate_total_score_from_book(
                config.token_ids,
                sample_count=10,
            )
            
            config.total_score_est = total_score
            config.spread_p50 = spread_p50
            config.liquidity_top_level = liq
            
            # Estimate your score (assuming competitive quoting)
            # Your score = your size * position factor
            # Assume you'd post min_size on both sides
            if spread_p50 > 0 and spread_p50 < DEFAULT_MAX_SPREAD:
                your_score = DEFAULT_MIN_SIZE * 2 * (1 - spread_p50 / DEFAULT_MAX_SPREAD) * TWO_SIDED_BONUS
            else:
                your_score = DEFAULT_MIN_SIZE * 0.5  # Penalty for wide spread
            
            config.your_score_per_hour = your_score
            
            # Estimate markout (placeholder - needs real data)
            # Higher liquidity markets tend to have less toxicity
            if liq > 100:
                config.markout_avg_bps = 5  # Low toxicity
            elif liq > 20:
                config.markout_avg_bps = 15  # Medium
            else:
                config.markout_avg_bps = 30  # High toxicity
            
            # Estimate fill rate
            if spread_p50 < 0.01:
                config.fill_rate_per_hour = 0.5  # Tight spread = more fills
            else:
                config.fill_rate_per_hour = 0.1  # Wide spread = fewer fills
            
            self.markets[config.slug] = config
        
        # Sort by net yield
        result = sorted(markets[:top_n], key=lambda x: x.net_est_usdc_day, reverse=True)
        return result
    
    def print_market_table(self, markets: List[MarketRewardConfig]):
        """Print formatted market table."""
        print("\n" + "=" * 100)
        print("MARKET REWARD ECONOMICS - Ranked by Net Est. $/day")
        print("=" * 100)
        
        print(f"\n{'Rank':<5} {'Net $/day':<12} {'Yield $/day':<12} {'Markout Loss':<12} "
              f"{'Share %':<10} {'Pool $/day':<12} {'Slug':<35}")
        print("-" * 100)
        
        for i, m in enumerate(markets[:30], 1):
            print(f"{i:<5} ${m.net_est_usdc_day:<10.2f} ${m.yield_usdc_day:<10.2f} "
                  f"${m.markout_loss_day:<10.2f} {m.your_share_est*100:<9.2f}% "
                  f"${m.pool_usdc_day:<10.0f} {m.slug[:35]}")
        
        print("\n" + "-" * 100)
        print("Legend:")
        print("  Net $/day = Yield - Markout Loss (estimated)")
        print("  Share % = your_score / total_market_score")
        print("  Markout = adverse selection loss on fills")
        print("\n[!] These are ESTIMATES. Real yields depend on actual fills and markout.")
    
    def save_results(self, markets: List[MarketRewardConfig]):
        """Save results to JSON."""
        ts_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        path = self.output_dir / f"rewards_economics_{ts_str}.json"
        
        data = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "markets_analyzed": len(markets),
            "markets": [m.to_dict() for m in markets],
        }
        
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        
        print(f"\nResults saved: {path}")


class MarkoutTracker:
    """
    Tracks fill toxicity via markout analysis.
    
    After each fill, records mid price at 1s, 5s, 30s, 120s
    to measure adverse selection.
    """
    
    def __init__(self):
        self.pending_markouts: List[MarkoutRecord] = []
        self.completed_markouts: List[MarkoutRecord] = []
        self.mid_history: List[Tuple[float, float]] = []  # (ts, mid)
    
    def record_fill(self, fill_price: float, side: str, size: float, mid_at_fill: float):
        """Record a new fill for markout tracking."""
        record = MarkoutRecord(
            fill_ts=time.time(),
            fill_price=fill_price,
            side=side,
            size=size,
            mid_at_fill=mid_at_fill,
        )
        self.pending_markouts.append(record)
    
    def update_mid(self, mid: float):
        """Record current mid for markout computation."""
        now = time.time()
        self.mid_history.append((now, mid))
        
        # Clean old history (keep last 5 minutes)
        cutoff = now - 300
        self.mid_history = [(ts, m) for ts, m in self.mid_history if ts > cutoff]
        
        # Update pending markouts
        completed = []
        for record in self.pending_markouts:
            elapsed = now - record.fill_ts
            
            # Fill in markout times
            if elapsed >= 1 and record.mid_1s == 0:
                record.mid_1s = mid
            if elapsed >= 5 and record.mid_5s == 0:
                record.mid_5s = mid
            if elapsed >= 30 and record.mid_30s == 0:
                record.mid_30s = mid
            if elapsed >= 120 and record.mid_120s == 0:
                record.mid_120s = mid
                completed.append(record)
        
        # Move completed to final list
        for record in completed:
            self.pending_markouts.remove(record)
            self.completed_markouts.append(record)
    
    def get_markout_stats(self) -> dict:
        """Get aggregate markout statistics."""
        if not self.completed_markouts:
            return {"fills": 0, "markout_1s_avg_bps": 0, "markout_30s_avg_bps": 0}
        
        markouts_1s = [m.markout_1s_bps for m in self.completed_markouts]
        markouts_5s = [m.markout_5s_bps for m in self.completed_markouts]
        markouts_30s = [m.markout_30s_bps for m in self.completed_markouts]
        
        return {
            "fills": len(self.completed_markouts),
            "markout_1s_avg_bps": statistics.mean(markouts_1s) if markouts_1s else 0,
            "markout_1s_median_bps": statistics.median(markouts_1s) if markouts_1s else 0,
            "markout_5s_avg_bps": statistics.mean(markouts_5s) if markouts_5s else 0,
            "markout_30s_avg_bps": statistics.mean(markouts_30s) if markouts_30s else 0,
            "markout_30s_p90_bps": sorted(markouts_30s)[int(len(markouts_30s)*0.9)] if len(markouts_30s) > 1 else 0,
        }


def main():
    import argparse
    
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%H:%M:%S"
    )
    
    parser = argparse.ArgumentParser(description="Rewards API - Fetch economics")
    parser.add_argument("--build-table", action="store_true", help="Build ranked market table")
    parser.add_argument("--top-n", type=int, default=50, help="Number of markets to analyze")
    
    args = parser.parse_args()
    
    if args.build_table:
        api = RewardsAPI()
        markets = api.build_market_table(top_n=args.top_n)
        api.print_market_table(markets)
        api.save_results(markets)
    else:
        print("Usage:")
        print("  Build market table: python -m pm_15m_arb.rewards_api --build-table --top-n 50")


if __name__ == "__main__":
    main()

