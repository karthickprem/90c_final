"""
Edge Radar - Find Where Edge Actually Exists

Scans ALL active Polymarket markets to find viable targets.
Computes per-market:
- Spread p50/p90
- Feasibility rate at multiple edge floors
- Invalid tick rate
- Market type (binary vs multi-outcome)
- Cross-market correlation opportunities

Ranks markets by "edge viability score" and outputs top targets.
"""

import logging
import time
import json
import requests
import statistics
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional, List, Dict, Tuple, Any
from dataclasses import dataclass, field
from collections import defaultdict

logger = logging.getLogger(__name__)

# Polymarket API endpoints
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"


@dataclass
class MarketInfo:
    """Market metadata from Gamma API."""
    slug: str
    question: str
    outcomes: List[str]
    token_ids: List[str]
    condition_id: str
    end_date: str
    active: bool
    volume: float = 0.0
    liquidity: float = 0.0
    
    @property
    def is_binary(self) -> bool:
        return len(self.outcomes) == 2
    
    @property
    def num_outcomes(self) -> int:
        return len(self.outcomes)


@dataclass
class MarketViability:
    """Viability metrics for a single market."""
    slug: str
    num_outcomes: int
    is_binary: bool
    
    # Tick quality
    valid_ticks: int = 0
    invalid_ticks: int = 0
    invalid_reasons: Dict[str, int] = field(default_factory=dict)
    
    # Spread metrics
    spreads: List[float] = field(default_factory=list)  # Per-outcome average spread
    
    # Feasibility (for binary only)
    ask_sums: List[float] = field(default_factory=list)
    bid_sums: List[float] = field(default_factory=list)
    feasibility_rates: Dict[float, float] = field(default_factory=dict)  # edge_floor -> rate
    
    # Multi-outcome specific
    multi_ask_sums: List[float] = field(default_factory=list)  # sum of all outcome asks
    multi_arb_opportunities: int = 0  # sum < 1 - edge
    
    # Raw price data for spike detection
    mid_prices: Dict[str, List[float]] = field(default_factory=dict)  # outcome -> list of mids
    
    @property
    def invalid_rate(self) -> float:
        total = self.valid_ticks + self.invalid_ticks
        return self.invalid_ticks / total if total > 0 else 1.0
    
    @property
    def spread_p50(self) -> float:
        return statistics.median(self.spreads) if self.spreads else 0
    
    @property
    def spread_p90(self) -> float:
        if not self.spreads:
            return 0
        s = sorted(self.spreads)
        return s[int(len(s) * 0.90)] if len(s) > 1 else s[-1]
    
    @property
    def ask_sum_min(self) -> float:
        return min(self.ask_sums) if self.ask_sums else 1.0
    
    @property
    def multi_arb_rate(self) -> float:
        return self.multi_arb_opportunities / max(1, self.valid_ticks)
    
    def compute_viability_score(self) -> float:
        """
        Compute overall viability score (higher = more promising).
        
        Factors:
        - Low invalid rate
        - Tight spreads
        - High feasibility at meaningful edge
        - For multi-outcome: actual arb opportunities
        """
        score = 0.0
        
        # Penalize bad data
        if self.invalid_rate > 0.3:
            return -1  # Too much bad data
        
        score += (1 - self.invalid_rate) * 10  # Max 10 for clean data
        
        # Reward tight spreads (spread_p50 < 2c is good)
        if self.spread_p50 > 0:
            spread_score = max(0, 5 - self.spread_p50 * 100)  # Max 5 for 0 spread
            score += spread_score
        
        if self.is_binary:
            # Binary: feasibility at 0.5% edge
            feas_05 = self.feasibility_rates.get(0.005, 0)
            score += feas_05 * 20  # Max 20 for 100% feasible
            
            # Bonus for ask_sum < 1.0 (instant arb potential)
            if self.ask_sum_min < 0.995:
                score += 15
        else:
            # Multi-outcome: arb rate
            score += self.multi_arb_rate * 50  # Heavily weight actual arb
            
            # Bonus for low sum
            if self.multi_ask_sums:
                min_sum = min(self.multi_ask_sums)
                if min_sum < 0.99:
                    score += 20
        
        return score
    
    def to_dict(self) -> dict:
        return {
            "slug": self.slug,
            "num_outcomes": self.num_outcomes,
            "is_binary": self.is_binary,
            "valid_ticks": self.valid_ticks,
            "invalid_ticks": self.invalid_ticks,
            "invalid_rate": self.invalid_rate,
            "invalid_reasons": self.invalid_reasons,
            "spread_p50_cents": self.spread_p50 * 100,
            "spread_p90_cents": self.spread_p90 * 100,
            "ask_sum_min": self.ask_sum_min,
            "multi_arb_rate": self.multi_arb_rate,
            "feasibility_rates": {f"{k*100:.1f}%": v for k, v in self.feasibility_rates.items()},
            "viability_score": self.compute_viability_score(),
        }


class EdgeRadar:
    """
    Scans all active Polymarket markets to find viable edge targets.
    """
    
    EDGE_FLOORS = [0.001, 0.002, 0.005, 0.01]  # 0.1%, 0.2%, 0.5%, 1.0%
    
    def __init__(
        self,
        output_dir: str = "pm_results_v4",
        sample_per_market: int = 50,  # Ticks to sample per market
        min_volume: float = 1000,  # Minimum volume to consider
    ):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(exist_ok=True)
        self.sample_per_market = sample_per_market
        self.min_volume = min_volume
        
        self.session = requests.Session()
        self.markets: List[MarketInfo] = []
        self.viability: Dict[str, MarketViability] = {}
    
    def _fetch_all_markets(self) -> List[dict]:
        """Fetch all active markets from Gamma API."""
        all_markets = []
        offset = 0
        limit = 100
        
        while True:
            try:
                url = f"{GAMMA_API}/markets?active=true&closed=false&limit={limit}&offset={offset}"
                resp = self.session.get(url, timeout=10)
                resp.raise_for_status()
                data = resp.json()
                
                if not data:
                    break
                
                all_markets.extend(data)
                offset += limit
                
                if len(data) < limit:
                    break
                
                time.sleep(0.2)  # Rate limit
                
            except Exception as e:
                logger.error(f"Error fetching markets at offset {offset}: {e}")
                break
        
        return all_markets
    
    def _parse_market(self, data: dict) -> Optional[MarketInfo]:
        """Parse market data into MarketInfo."""
        try:
            slug = data.get("slug", "")
            question = data.get("question", "")
            
            # Parse outcomes
            outcomes_raw = data.get("outcomes", "[]")
            if isinstance(outcomes_raw, str):
                import json as json_mod
                outcomes = json_mod.loads(outcomes_raw)
            else:
                outcomes = outcomes_raw
            
            # Parse token IDs
            tokens_raw = data.get("clobTokenIds", "[]")
            if isinstance(tokens_raw, str):
                import json as json_mod
                token_ids = json_mod.loads(tokens_raw)
            else:
                token_ids = tokens_raw if tokens_raw else []
            
            if not token_ids or not outcomes:
                return None
            
            return MarketInfo(
                slug=slug,
                question=question,
                outcomes=outcomes,
                token_ids=token_ids,
                condition_id=data.get("conditionId", ""),
                end_date=data.get("endDate", ""),
                active=data.get("active", False),
                volume=float(data.get("volume", 0) or 0),
                liquidity=float(data.get("liquidity", 0) or 0),
            )
        except Exception as e:
            logger.debug(f"Error parsing market: {e}")
            return None
    
    def _fetch_orderbook(self, token_id: str) -> Optional[dict]:
        """Fetch orderbook for a token."""
        try:
            url = f"{CLOB_API}/book?token_id={token_id}"
            resp = self.session.get(url, timeout=5)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.debug(f"Error fetching book for {token_id}: {e}")
            return None
    
    def _parse_book(self, book: dict) -> Tuple[float, float, float]:
        """
        Parse orderbook and return (best_bid, best_ask, spread).
        Returns (0, 0, 0) if invalid.
        """
        try:
            bids = book.get("bids", [])
            asks = book.get("asks", [])
            
            if not bids or not asks:
                return 0, 0, 0
            
            best_bid = float(bids[0].get("price", 0))
            best_ask = float(asks[0].get("price", 0))
            
            # Allow very low prices (near 0.01) - just not exactly 0
            if best_bid <= 0 or best_ask <= 0:
                return 0, 0, 0
            
            if best_ask <= best_bid:
                return 0, 0, 0  # Inverted book
            
            spread = best_ask - best_bid
            return best_bid, best_ask, spread
            
        except Exception:
            return 0, 0, 0
    
    def _sample_market(self, market: MarketInfo, viability: MarketViability):
        """Sample a market's orderbook multiple times."""
        
        for _ in range(self.sample_per_market):
            # Fetch all outcome books
            outcome_data = []
            any_invalid = False
            invalid_reason = None
            
            for i, token_id in enumerate(market.token_ids):
                book = self._fetch_orderbook(token_id)
                if not book:
                    any_invalid = True
                    invalid_reason = "book_fetch_failed"
                    break
                
                bid, ask, spread = self._parse_book(book)
                if bid == 0 or ask == 0:
                    any_invalid = True
                    invalid_reason = "zero_price"
                    break
                
                # Only reject if BOTH sides are near-zero (empty market)
                if ask < 0.005 and bid < 0.005:
                    any_invalid = True
                    invalid_reason = "price_too_low"
                    break
                
                outcome_data.append({
                    "outcome": market.outcomes[i] if i < len(market.outcomes) else f"outcome_{i}",
                    "bid": bid,
                    "ask": ask,
                    "spread": spread,
                    "mid": (bid + ask) / 2,
                })
            
            if any_invalid:
                viability.invalid_ticks += 1
                viability.invalid_reasons[invalid_reason] = viability.invalid_reasons.get(invalid_reason, 0) + 1
                time.sleep(0.1)
                continue
            
            viability.valid_ticks += 1
            
            # Record spreads
            for od in outcome_data:
                viability.spreads.append(od["spread"])
            
            # Compute sums
            ask_sum = sum(od["ask"] for od in outcome_data)
            bid_sum = sum(od["bid"] for od in outcome_data)
            
            if market.is_binary:
                viability.ask_sums.append(ask_sum)
                viability.bid_sums.append(bid_sum)
                
                # Compute feasibility at each edge floor
                for ef in self.EDGE_FLOORS:
                    # For binary: check if ask_yes + ask_no <= 1 - ef
                    is_feasible = ask_sum <= 1.0 - ef
                    if ef not in viability.feasibility_rates:
                        viability.feasibility_rates[ef] = 0
                    if is_feasible:
                        # Increment (we'll normalize later)
                        viability.feasibility_rates[ef] += 1
            else:
                # Multi-outcome
                viability.multi_ask_sums.append(ask_sum)
                
                # Check for arb (sum < 1 - edge)
                if ask_sum < 0.99:  # 1% edge threshold
                    viability.multi_arb_opportunities += 1
            
            # Record mid prices for spike detection
            for od in outcome_data:
                outcome_name = od["outcome"]
                if outcome_name not in viability.mid_prices:
                    viability.mid_prices[outcome_name] = []
                viability.mid_prices[outcome_name].append(od["mid"])
            
            time.sleep(0.15)  # Rate limit
        
        # Normalize feasibility rates
        if market.is_binary and viability.valid_ticks > 0:
            for ef in self.EDGE_FLOORS:
                if ef in viability.feasibility_rates:
                    viability.feasibility_rates[ef] /= viability.valid_ticks
    
    def scan_all_markets(self, max_markets: int = 100):
        """
        Scan all active markets and compute viability metrics.
        """
        print("\n" + "=" * 70)
        print("EDGE RADAR - Scanning All Active Markets")
        print("=" * 70)
        
        # Fetch all markets
        print("Fetching market list...")
        raw_markets = self._fetch_all_markets()
        print(f"Found {len(raw_markets)} raw markets")
        
        # Keywords for high-activity markets
        active_keywords = ["btc", "bitcoin", "eth", "ethereum", "crypto", "nfl", "nba", 
                          "super bowl", "trump", "fed", "gdp", "cpi", "election",
                          "minutes", "hour", "daily", "weekly"]
        
        # Parse and filter
        for data in raw_markets:
            market = self._parse_market(data)
            if not market or market.volume < self.min_volume:
                continue
            
            # Prioritize active keywords but don't exclude
            slug_lower = market.slug.lower()
            question_lower = market.question.lower()
            has_active_keyword = any(kw in slug_lower or kw in question_lower for kw in active_keywords)
            market._priority = 1 if has_active_keyword else 0
            
            self.markets.append(market)
        
        print(f"After filtering: {len(self.markets)} markets with volume >= ${self.min_volume}")
        
        # Sort by volume (highest first)
        self.markets.sort(key=lambda m: m.volume, reverse=True)
        
        # Limit
        self.markets = self.markets[:max_markets]
        print(f"Scanning top {len(self.markets)} markets by volume")
        print("=" * 70)
        
        # Sample each market
        for i, market in enumerate(self.markets):
            print(f"[{i+1}/{len(self.markets)}] {market.slug[:50]}... "
                  f"({market.num_outcomes} outcomes, ${market.volume:,.0f} vol)")
            
            viability = MarketViability(
                slug=market.slug,
                num_outcomes=market.num_outcomes,
                is_binary=market.is_binary,
            )
            
            self._sample_market(market, viability)
            self.viability[market.slug] = viability
            
            # Quick stats
            score = viability.compute_viability_score()
            print(f"    Score: {score:.1f} | Invalid: {viability.invalid_rate*100:.0f}% | "
                  f"Spread p50: {viability.spread_p50*100:.1f}c")
        
        # Save results
        self._save_results()
        self._print_top_targets()
    
    def _save_results(self):
        """Save scan results."""
        ts_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        path = self.output_dir / f"edge_radar_{ts_str}.json"
        
        results = {
            "scan_time": datetime.now(timezone.utc).isoformat(),
            "markets_scanned": len(self.markets),
            "sample_per_market": self.sample_per_market,
            "markets": [v.to_dict() for v in self.viability.values()],
        }
        
        with open(path, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2)
        
        print(f"\nResults saved: {path}")
    
    def _print_top_targets(self):
        """Print top viable targets."""
        print("\n" + "=" * 70)
        print("TOP EDGE TARGETS (by viability score)")
        print("=" * 70)
        
        # Sort by score
        ranked = sorted(
            self.viability.values(),
            key=lambda v: v.compute_viability_score(),
            reverse=True
        )
        
        # Separate binary and multi-outcome
        binary_targets = [v for v in ranked if v.is_binary][:10]
        multi_targets = [v for v in ranked if not v.is_binary][:10]
        
        print("\n--- TOP BINARY MARKETS ---")
        print(f"{'Rank':<5} {'Score':<8} {'Spread':<10} {'Feas@0.5%':<12} {'AskSum Min':<12} {'Slug':<40}")
        print("-" * 90)
        
        for i, v in enumerate(binary_targets, 1):
            feas = v.feasibility_rates.get(0.005, 0) * 100
            print(f"{i:<5} {v.compute_viability_score():<8.1f} "
                  f"{v.spread_p50*100:<10.2f}c {feas:<12.1f}% "
                  f"{v.ask_sum_min:<12.4f} {v.slug[:40]}")
        
        print("\n--- TOP MULTI-OUTCOME MARKETS ---")
        print(f"{'Rank':<5} {'Score':<8} {'Outcomes':<10} {'Arb Rate':<12} {'AskSum Min':<12} {'Slug':<40}")
        print("-" * 90)
        
        for i, v in enumerate(multi_targets, 1):
            min_sum = min(v.multi_ask_sums) if v.multi_ask_sums else 1.0
            print(f"{i:<5} {v.compute_viability_score():<8.1f} "
                  f"{v.num_outcomes:<10} {v.multi_arb_rate*100:<12.1f}% "
                  f"{min_sum:<12.4f} {v.slug[:40]}")
        
        # Recommendations
        print("\n" + "=" * 70)
        print("RECOMMENDATIONS")
        print("=" * 70)
        
        # Check for instant arb
        instant_arb = [v for v in ranked if v.ask_sum_min < 0.995]
        if instant_arb:
            print(f"\n🎯 INSTANT ARB DETECTED in {len(instant_arb)} markets:")
            for v in instant_arb[:5]:
                print(f"   {v.slug}: ask_sum_min = {v.ask_sum_min:.4f}")
        
        # Check for high feasibility binary
        high_feas = [v for v in binary_targets if v.feasibility_rates.get(0.005, 0) > 0.20]
        if high_feas:
            print(f"\n📊 HIGH FEASIBILITY BINARY ({len(high_feas)} markets with >20% at 0.5% edge):")
            for v in high_feas[:5]:
                print(f"   {v.slug}: feas = {v.feasibility_rates.get(0.005,0)*100:.1f}%")
        
        # Check for multi-outcome arb
        multi_arb = [v for v in multi_targets if v.multi_arb_rate > 0.01]
        if multi_arb:
            print(f"\n🎲 MULTI-OUTCOME ARB ({len(multi_arb)} markets with >1% arb rate):")
            for v in multi_arb[:5]:
                min_sum = min(v.multi_ask_sums) if v.multi_ask_sums else 1.0
                print(f"   {v.slug}: arb_rate = {v.multi_arb_rate*100:.1f}%, min_sum = {min_sum:.4f}")
        
        if not instant_arb and not high_feas and not multi_arb:
            print("\n⚠️ No high-viability targets found in this scan.")
            print("   Consider: expanding sample size, checking during high-volatility periods,")
            print("   or focusing on cross-market correlation opportunities.")


class MultiOutcomeArbScanner:
    """
    Scans for multi-outcome full-set arbitrage opportunities.
    
    Arb exists when: sum(best_asks) < 1 - edge
    """
    
    def __init__(
        self,
        edge_threshold: float = 0.01,  # 1% minimum edge
        output_dir: str = "pm_results_v4",
    ):
        self.edge_threshold = edge_threshold
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(exist_ok=True)
        
        self.session = requests.Session()
        self.opportunities: List[dict] = []
    
    def _fetch_market_books(self, token_ids: List[str]) -> List[Tuple[float, float]]:
        """Fetch books for all outcomes. Returns list of (bid, ask) tuples."""
        results = []
        for token_id in token_ids:
            try:
                url = f"{CLOB_API}/book?token_id={token_id}"
                resp = self.session.get(url, timeout=5)
                resp.raise_for_status()
                book = resp.json()
                
                bids = book.get("bids", [])
                asks = book.get("asks", [])
                
                if bids and asks:
                    bid = float(bids[0].get("price", 0))
                    ask = float(asks[0].get("price", 0))
                    if bid > 0 and ask > 0 and ask > bid:
                        results.append((bid, ask))
                        continue
                
                results.append((0, 0))
            except:
                results.append((0, 0))
        
        return results
    
    def scan_market(self, market: MarketInfo) -> Optional[dict]:
        """Check a single market for arb opportunity."""
        books = self._fetch_market_books(market.token_ids)
        
        # Validate all books
        if any(b[1] == 0 for b in books):
            return None
        
        ask_sum = sum(b[1] for b in books)
        bid_sum = sum(b[0] for b in books)
        
        # Check for arb
        if ask_sum < 1.0 - self.edge_threshold:
            edge = 1.0 - ask_sum
            return {
                "slug": market.slug,
                "question": market.question,
                "outcomes": market.outcomes,
                "asks": [b[1] for b in books],
                "ask_sum": ask_sum,
                "edge": edge,
                "edge_pct": edge * 100,
                "ts": time.time(),
            }
        
        return None
    
    def continuous_scan(self, markets: List[MarketInfo], duration_minutes: float = 30):
        """Continuously scan markets for arb opportunities."""
        print("\n" + "=" * 70)
        print("MULTI-OUTCOME ARB SCANNER")
        print("=" * 70)
        print(f"Markets: {len(markets)}")
        print(f"Edge threshold: {self.edge_threshold * 100:.1f}%")
        print(f"Duration: {duration_minutes} minutes")
        print("=" * 70)
        
        start = time.time()
        deadline = start + duration_minutes * 60
        scan_count = 0
        
        ts_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        log_path = self.output_dir / f"multi_arb_{ts_str}.jsonl"
        
        with open(log_path, "w", encoding="utf-8") as f:
            while time.time() < deadline:
                for market in markets:
                    opp = self.scan_market(market)
                    if opp:
                        self.opportunities.append(opp)
                        f.write(json.dumps(opp) + "\n")
                        f.flush()
                        
                        print(f"🎯 ARB FOUND: {opp['slug'][:40]}")
                        print(f"   Outcomes: {opp['outcomes']}")
                        print(f"   Asks: {opp['asks']}")
                        print(f"   Sum: {opp['ask_sum']:.4f}, Edge: {opp['edge_pct']:.2f}%")
                    
                    time.sleep(0.1)  # Rate limit
                
                scan_count += 1
                elapsed = (time.time() - start) / 60
                print(f"[{elapsed:.1f}m] Scan {scan_count} complete. Opps found: {len(self.opportunities)}")
                
                time.sleep(1)  # Brief pause between full scans
        
        print("\n" + "=" * 70)
        print(f"SCAN COMPLETE: {len(self.opportunities)} opportunities found")
        print(f"Log: {log_path}")


class SpikeHunter:
    """
    Directional spike detector for fast mean-reversion plays.
    
    Detects rapid price jumps (panic spikes) and generates signals.
    Paper-only for now.
    """
    
    def __init__(
        self,
        spike_threshold: float = 0.03,  # 3% price jump to trigger
        lookback_ticks: int = 10,
        exit_target: float = 0.02,  # Exit on 2% reversion
        stop_loss: float = 0.05,  # Stop on 5% adverse
        output_dir: str = "pm_results_v4",
    ):
        self.spike_threshold = spike_threshold
        self.lookback_ticks = lookback_ticks
        self.exit_target = exit_target
        self.stop_loss = stop_loss
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(exist_ok=True)
        
        self.session = requests.Session()
        
        # State per market
        self.price_history: Dict[str, List[float]] = defaultdict(list)
        self.positions: Dict[str, dict] = {}  # slug -> position info
        self.trades: List[dict] = []
    
    def _detect_spike(self, slug: str, current_price: float) -> Optional[str]:
        """
        Detect if current price represents a spike.
        Returns 'up_spike' or 'down_spike' or None.
        """
        history = self.price_history[slug]
        if len(history) < self.lookback_ticks:
            return None
        
        recent_avg = statistics.mean(history[-self.lookback_ticks:])
        change = (current_price - recent_avg) / recent_avg if recent_avg > 0 else 0
        
        if change > self.spike_threshold:
            return "up_spike"  # Price spiked up -> bet on reversion down
        elif change < -self.spike_threshold:
            return "down_spike"  # Price spiked down -> bet on reversion up
        
        return None
    
    def _check_exit(self, slug: str, current_price: float) -> Optional[str]:
        """Check if position should exit. Returns 'target', 'stop', or None."""
        if slug not in self.positions:
            return None
        
        pos = self.positions[slug]
        entry_price = pos["entry_price"]
        direction = pos["direction"]
        
        if direction == "long":
            pnl = (current_price - entry_price) / entry_price
            if pnl >= self.exit_target:
                return "target"
            elif pnl <= -self.stop_loss:
                return "stop"
        else:  # short
            pnl = (entry_price - current_price) / entry_price
            if pnl >= self.exit_target:
                return "target"
            elif pnl <= -self.stop_loss:
                return "stop"
        
        return None
    
    def monitor_market(self, market: MarketInfo, duration_minutes: float = 15):
        """Monitor a single market for spikes."""
        print(f"\n🔍 Monitoring: {market.slug}")
        print(f"   Spike threshold: {self.spike_threshold*100:.1f}%")
        print(f"   Exit target: {self.exit_target*100:.1f}%, Stop: {self.stop_loss*100:.1f}%")
        
        start = time.time()
        deadline = start + duration_minutes * 60
        slug = market.slug
        
        # Use first outcome (YES side for binary)
        token_id = market.token_ids[0]
        
        while time.time() < deadline:
            try:
                url = f"{CLOB_API}/book?token_id={token_id}"
                resp = self.session.get(url, timeout=5)
                resp.raise_for_status()
                book = resp.json()
                
                bids = book.get("bids", [])
                asks = book.get("asks", [])
                
                if bids and asks:
                    bid = float(bids[0].get("price", 0))
                    ask = float(asks[0].get("price", 0))
                    
                    if bid > 0 and ask > 0:
                        mid = (bid + ask) / 2
                        self.price_history[slug].append(mid)
                        
                        # Check for spike
                        if slug not in self.positions:
                            spike = self._detect_spike(slug, mid)
                            if spike:
                                # Enter position (paper)
                                direction = "short" if spike == "up_spike" else "long"
                                self.positions[slug] = {
                                    "entry_price": mid,
                                    "direction": direction,
                                    "entry_ts": time.time(),
                                    "spike_type": spike,
                                }
                                
                                trade = {
                                    "slug": slug,
                                    "action": "entry",
                                    "direction": direction,
                                    "price": mid,
                                    "spike_type": spike,
                                    "ts": time.time(),
                                }
                                self.trades.append(trade)
                                print(f"   📈 SPIKE ENTRY: {direction} @ {mid:.4f} ({spike})")
                        else:
                            # Check exit
                            exit_reason = self._check_exit(slug, mid)
                            if exit_reason:
                                pos = self.positions.pop(slug)
                                pnl = mid - pos["entry_price"] if pos["direction"] == "long" else pos["entry_price"] - mid
                                
                                trade = {
                                    "slug": slug,
                                    "action": "exit",
                                    "direction": pos["direction"],
                                    "entry_price": pos["entry_price"],
                                    "exit_price": mid,
                                    "pnl": pnl,
                                    "pnl_pct": pnl / pos["entry_price"] * 100,
                                    "reason": exit_reason,
                                    "duration": time.time() - pos["entry_ts"],
                                    "ts": time.time(),
                                }
                                self.trades.append(trade)
                                
                                emoji = "✅" if pnl > 0 else "❌"
                                print(f"   {emoji} EXIT ({exit_reason}): {pnl*100:.2f}% @ {mid:.4f}")
                
            except Exception as e:
                logger.debug(f"Error monitoring {slug}: {e}")
            
            time.sleep(0.3)
        
        # Close any open position at end
        if slug in self.positions:
            pos = self.positions.pop(slug)
            print(f"   ⚠️ Position closed at timeout")


def main():
    import argparse
    
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%H:%M:%S"
    )
    
    parser = argparse.ArgumentParser(description="Edge Radar - Find viable Polymarket targets")
    parser.add_argument("--scan", action="store_true", help="Scan all markets for viability")
    parser.add_argument("--multi-arb", action="store_true", help="Scan for multi-outcome arb")
    parser.add_argument("--spike-hunt", action="store_true", help="Monitor for price spikes")
    parser.add_argument("--max-markets", type=int, default=50, help="Max markets to scan")
    parser.add_argument("--duration", type=float, default=30, help="Duration in minutes")
    parser.add_argument("--min-volume", type=float, default=5000, help="Min volume filter")
    
    args = parser.parse_args()
    
    if args.scan:
        radar = EdgeRadar(
            sample_per_market=30,
            min_volume=args.min_volume,
        )
        radar.scan_all_markets(max_markets=args.max_markets)
    
    elif args.multi_arb:
        # First fetch markets - include binary markets for instant arb
        print("Fetching all active markets for arb scan...")
        radar = EdgeRadar(min_volume=args.min_volume)
        raw = radar._fetch_all_markets()
        
        markets = []
        for data in raw:
            m = radar._parse_market(data)
            if m and m.volume >= args.min_volume:
                markets.append(m)
        
        # Sort by volume
        markets.sort(key=lambda x: x.volume, reverse=True)
        markets = markets[:args.max_markets]
        
        print(f"Found {len(markets)} markets (binary + multi)")
        
        scanner = MultiOutcomeArbScanner(edge_threshold=0.005)  # 0.5% threshold
        scanner.continuous_scan(markets, duration_minutes=args.duration)
    
    elif args.spike_hunt:
        print("Spike hunting requires a specific market. Use Edge Radar --scan first to find targets.")
    
    else:
        print("Usage:")
        print("  Scan all markets:    python -m pm_15m_arb.edge_radar --scan --max-markets 50")
        print("  Multi-outcome arb:   python -m pm_15m_arb.edge_radar --multi-arb --duration 30")
        print("  Spike hunting:       python -m pm_15m_arb.edge_radar --spike-hunt")


if __name__ == "__main__":
    main()

