"""
Paper Execution Simulator

Simulates paired order execution for full-set arbitrage.

Key logic:
1. Attempt to fill YES and NO at VWAP prices (simulated IOC)
2. If both fill -> IMMEDIATELY REDEEM at $1 (realize PnL)
3. If only one fills -> unwind at VWAP bid (not just best bid)
4. Track partial fills, fill rates, unwind losses

Full-set accounting: When both legs fill, we hold a "full set" which is
deterministically worth $1. We auto-redeem immediately to realize PnL.

This is paper trading - no real orders placed.
"""

import logging
import time
import random
from typing import Optional, Tuple, Dict, List
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
import requests

from .config import ArbConfig, load_config
from .scanner import ArbOpportunity, ArbScanner, OrderBookSnapshot

logger = logging.getLogger(__name__)


class FillStatus(Enum):
    """Order fill status."""
    PENDING = "PENDING"
    FILLED = "FILLED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    UNWOUND = "UNWOUND"


class ExecutionStatus(Enum):
    """Overall execution status."""
    SUCCESS = "SUCCESS"           # Both legs filled
    ONE_LEG_UNWOUND = "ONE_LEG"   # One leg filled, other failed, unwound
    BOTH_FAILED = "BOTH_FAILED"   # Neither leg filled
    SKIPPED = "SKIPPED"           # Skipped for some reason


@dataclass
class LegFill:
    """Result of attempting to fill one leg."""
    token_id: str
    side: str  # "YES" or "NO"
    target_shares: float
    target_price: float  # L1 best ask
    vwap_price: float    # VWAP for target size
    status: FillStatus
    filled_shares: float = 0
    fill_price: float = 0  # Actual fill price (with slippage)
    slippage: float = 0
    slippage_pct: float = 0
    fill_time_ms: float = 0
    levels_used: int = 1  # How many price levels consumed
    partial_fill: bool = False  # True if only partially filled


@dataclass
class UnwindResult:
    """Result of unwinding a filled leg using VWAP at bid."""
    leg: LegFill
    unwind_vwap: float    # VWAP bid price we sold at (depth-aware)
    unwind_shares: float
    unwind_cost: float    # What we paid for the leg
    unwind_proceeds: float  # What we got back
    unwind_loss: float    # Loss in USD
    unwind_loss_pct: float  # Loss as % of position


@dataclass
class ExecutionResult:
    """
    Result of executing a full-set arb opportunity.
    
    Full-set accounting:
    - If both legs fill, we hold qty shares of (YES + NO) = guaranteed $1 each
    - We IMMEDIATELY redeem at $1, so realized_pnl = (1 * qty) - total_cost
    - No "unrealized" PnL for successful full-sets (they're redeemed instantly)
    """
    
    opportunity: ArbOpportunity
    timestamp: datetime
    status: ExecutionStatus
    
    # Leg fills
    yes_fill: Optional[LegFill] = None
    no_fill: Optional[LegFill] = None
    
    # If one leg failed
    unwind: Optional[UnwindResult] = None
    
    # Full-set accounting
    shares_filled: float = 0        # Qty of full sets (min of both legs)
    total_cost: float = 0           # Total USD spent on both legs
    redemption_value: float = 0     # $1 per share for full sets
    realized_pnl: float = 0         # ACTUAL realized P&L (after redemption or unwind)
    
    # For partial fills
    excess_yes_shares: float = 0    # YES shares without matching NO
    excess_no_shares: float = 0     # NO shares without matching YES
    
    # Execution metrics
    total_time_ms: float = 0
    
    def to_dict(self) -> dict:
        """Convert to dictionary for logging."""
        return {
            "market_slug": self.opportunity.market.slug,
            "timestamp": self.timestamp.isoformat(),
            "status": self.status.value,
            "edge_l1": self.opportunity.edge_l1,
            "edge_exec": self.opportunity.edge_exec,
            "shares_filled": self.shares_filled,
            "total_cost": self.total_cost,
            "redemption_value": self.redemption_value,
            "realized_pnl": self.realized_pnl,
            "total_time_ms": self.total_time_ms,
            "yes_status": self.yes_fill.status.value if self.yes_fill else None,
            "no_status": self.no_fill.status.value if self.no_fill else None,
            "yes_filled_shares": self.yes_fill.filled_shares if self.yes_fill else 0,
            "no_filled_shares": self.no_fill.filled_shares if self.no_fill else 0,
            "unwind_loss": self.unwind.unwind_loss if self.unwind else None,
        }


class PaperExecutor:
    """
    Paper trading executor for full-set arbitrage.
    
    Simulates order execution with realistic assumptions:
    - Fill at VWAP prices (depth-aware), not just best ask
    - Partial fill tracking
    - VWAP-based unwind for one-leg situations
    - Immediate redemption for successful full-sets
    
    Does NOT place real orders.
    """
    
    def __init__(self, config: ArbConfig = None, scanner: ArbScanner = None):
        self.config = config or load_config()
        self.scanner = scanner
        
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "PolymarketArbBot/1.0",
            "Accept": "application/json"
        })
        
        # Execution statistics
        self.executions_total = 0
        self.executions_success = 0
        self.executions_one_leg = 0
        self.executions_partial = 0
        self.executions_failed = 0
        
        self.total_realized_pnl = 0
        self.total_unwind_loss = 0
        self.total_redemption_profit = 0
    
    def _simulate_fill_probability(self, 
                                   target_shares: float, 
                                   available_depth: float,
                                   spread: float,
                                   vwap_slippage: float) -> Tuple[float, float]:
        """
        Estimate probability and extent of fill.
        
        Returns: (fill_probability, fill_fraction)
        - fill_probability: chance of getting any fill
        - fill_fraction: expected fraction filled (for partial fills)
        """
        # Depth factor: more depth = more likely to fill
        depth_ratio = available_depth / max(target_shares, 1)
        depth_prob = min(1.0, depth_ratio * 0.95)  # Cap at 95% from depth alone
        
        # Spread factor: tighter spread = better liquidity
        spread_factor = max(0.5, 1.0 - (spread * 10))
        
        # Slippage penalty: higher VWAP slippage = harder to fill
        slippage_factor = max(0.7, 1.0 - (vwap_slippage * 5))
        
        # Combined probability
        fill_prob = depth_prob * spread_factor * slippage_factor
        
        # Add some randomness (simulation noise)
        noise = random.uniform(-0.05, 0.05)
        fill_prob = max(0.1, min(0.98, fill_prob + noise))
        
        # Fill fraction (for partial fills)
        fill_fraction = min(1.0, depth_ratio) * random.uniform(0.9, 1.0)
        
        return fill_prob, fill_fraction
    
    def _get_orderbook(self, token_id: str) -> Optional[OrderBookSnapshot]:
        """Fetch current orderbook for VWAP calculations."""
        try:
            from .scanner import OrderBookLevel
            
            start_time = time.time()
            url = f"{self.config.clob_api_url}/book"
            response = self.session.get(url, params={"token_id": token_id}, timeout=5)
            response.raise_for_status()
            fetch_time_ms = (time.time() - start_time) * 1000
            
            data = response.json()
            
            bids = []
            for level in (data.get("bids") or []):
                price = float(level.get("price", 0))
                size = float(level.get("size", 0))
                if 0 < price <= 1:
                    bids.append(OrderBookLevel(price=price, size=size))
            
            asks = []
            for level in (data.get("asks") or []):
                price = float(level.get("price", 0))
                size = float(level.get("size", 0))
                if 0 < price <= 1:
                    asks.append(OrderBookLevel(price=price, size=size))
            
            bids.sort(key=lambda x: x.price, reverse=True)
            asks.sort(key=lambda x: x.price)
            
            from datetime import datetime
            return OrderBookSnapshot(
                token_id=token_id,
                bids=bids,
                asks=asks,
                timestamp=datetime.now(),
                fetch_time_ms=fetch_time_ms
            )
        except Exception:
            return None
    
    def _simulate_leg_fill(self, 
                           token_id: str, 
                           side: str,
                           target_shares: float,
                           l1_price: float,
                           vwap_price: float,
                           available_depth: float,
                           spread: float) -> LegFill:
        """
        Simulate filling one leg using VWAP pricing.
        
        Uses probabilistic model based on depth, spread, and VWAP slippage.
        Supports partial fills.
        """
        start_time = time.time()
        
        # Calculate VWAP slippage from L1
        vwap_slippage = (vwap_price - l1_price) / l1_price if l1_price > 0 else 0
        
        # Calculate fill probability and fraction
        fill_prob, fill_fraction = self._simulate_fill_probability(
            target_shares, available_depth, spread, vwap_slippage
        )
        
        # Simulate fill/no-fill/partial
        roll = random.random()
        
        if roll < fill_prob:
            # Full or partial fill
            if roll < fill_prob * 0.9:  # 90% of fills are complete
                filled_shares = target_shares
                partial = False
            else:  # 10% are partial
                filled_shares = target_shares * fill_fraction
                partial = True
            
            # Use VWAP + small random slippage
            extra_slippage = random.uniform(0, 0.002)
            fill_price = vwap_price * (1 + extra_slippage)
            fill_price = min(fill_price, 1.0)
            
            actual_slippage = fill_price - l1_price
            slippage_pct = actual_slippage / l1_price if l1_price > 0 else 0
            
            elapsed_ms = (time.time() - start_time) * 1000 + random.uniform(10, 50)
            
            return LegFill(
                token_id=token_id,
                side=side,
                target_shares=target_shares,
                target_price=l1_price,
                vwap_price=vwap_price,
                status=FillStatus.PARTIAL if partial else FillStatus.FILLED,
                filled_shares=filled_shares,
                fill_price=fill_price,
                slippage=actual_slippage,
                slippage_pct=slippage_pct,
                fill_time_ms=elapsed_ms,
                partial_fill=partial,
            )
        else:
            # Failed to fill
            elapsed_ms = (time.time() - start_time) * 1000 + random.uniform(5, 20)
            
            return LegFill(
                token_id=token_id,
                side=side,
                target_shares=target_shares,
                target_price=l1_price,
                vwap_price=vwap_price,
                status=FillStatus.FAILED,
                fill_time_ms=elapsed_ms,
            )
    
    def _unwind_leg_vwap(self, leg: LegFill) -> UnwindResult:
        """
        Unwind a filled leg using VWAP at bid (depth-aware).
        
        This is the "one-leg risk" - we bought one side but couldn't
        buy the other, so we must sell what we have at a loss.
        
        Uses VWAP across bid levels, not just best bid.
        """
        # Get current orderbook for VWAP unwind
        book = self._get_orderbook(leg.token_id)
        
        if book and book.bids:
            # Calculate VWAP to sell our shares
            vwap_result = book.vwap_sell(leg.filled_shares)
            unwind_vwap = vwap_result.vwap if vwap_result.can_fill else book.best_bid.price
        else:
            # Fallback: estimate bid from fill price (assume 2-3 cent spread)
            unwind_vwap = leg.fill_price - random.uniform(0.02, 0.04)
            unwind_vwap = max(0.01, unwind_vwap)
        
        # Calculate loss
        cost = leg.filled_shares * leg.fill_price
        proceeds = leg.filled_shares * unwind_vwap
        loss = cost - proceeds
        loss_pct = loss / cost if cost > 0 else 0
        
        return UnwindResult(
            leg=leg,
            unwind_vwap=unwind_vwap,
            unwind_shares=leg.filled_shares,
            unwind_cost=cost,
            unwind_proceeds=proceeds,
            unwind_loss=loss,
            unwind_loss_pct=loss_pct,
        )
    
    def _redeem_full_set(self, shares: float, total_cost: float) -> Tuple[float, float]:
        """
        Redeem a full set (YES + NO) for $1 per share.
        
        This is deterministic - no risk once you hold both.
        
        Returns: (redemption_value, realized_pnl)
        """
        redemption_value = shares * 1.0  # $1 per full set
        realized_pnl = redemption_value - total_cost
        return redemption_value, realized_pnl
    
    def execute(self, opportunity: ArbOpportunity, shares: float = None) -> ExecutionResult:
        """
        Execute a full-set arbitrage opportunity (paper).
        
        1. Attempt to buy YES at VWAP
        2. Attempt to buy NO at VWAP
        3. If both fill: IMMEDIATELY REDEEM at $1, realize PnL
        4. If one fills: unwind at VWAP bid and record loss
        5. Handle partial fills by matching what we can
        
        Args:
            opportunity: The arbitrage opportunity to execute
            shares: Number of shares to buy (default: based on opportunity.target_shares)
        
        Returns:
            ExecutionResult with fill details and P&L
        """
        self.executions_total += 1
        start_time = time.time()
        
        if not opportunity.is_actionable:
            return ExecutionResult(
                opportunity=opportunity,
                timestamp=datetime.now(),
                status=ExecutionStatus.SKIPPED,
            )
        
        # Use target shares from opportunity (already VWAP-priced)
        if shares is None:
            shares = opportunity.target_shares
        
        # Cap by available depth (with buffer)
        shares = min(shares, opportunity.min_depth * 0.9)
        
        if shares < 1:
            return ExecutionResult(
                opportunity=opportunity,
                timestamp=datetime.now(),
                status=ExecutionStatus.SKIPPED,
            )
        
        # Execute YES leg with VWAP pricing
        yes_fill = self._simulate_leg_fill(
            token_id=opportunity.market.yes_token_id,
            side="YES",
            target_shares=shares,
            l1_price=opportunity.ask_yes,
            vwap_price=opportunity.vwap_yes,
            available_depth=opportunity.depth_yes,
            spread=opportunity.spread_yes,
        )
        
        # Execute NO leg with VWAP pricing
        no_fill = self._simulate_leg_fill(
            token_id=opportunity.market.no_token_id,
            side="NO",
            target_shares=shares,
            l1_price=opportunity.ask_no,
            vwap_price=opportunity.vwap_no,
            available_depth=opportunity.depth_no,
            spread=opportunity.spread_no,
        )
        
        total_time_ms = (time.time() - start_time) * 1000
        
        # Check fill statuses
        yes_filled = yes_fill.status in (FillStatus.FILLED, FillStatus.PARTIAL)
        no_filled = no_fill.status in (FillStatus.FILLED, FillStatus.PARTIAL)
        
        if yes_filled and no_filled:
            # SUCCESS: Both legs filled (possibly partial)
            # Match the minimum filled shares as our "full set"
            matched_shares = min(yes_fill.filled_shares, no_fill.filled_shares)
            excess_yes = yes_fill.filled_shares - matched_shares
            excess_no = no_fill.filled_shares - matched_shares
            
            # Cost for matched shares only
            yes_cost = matched_shares * yes_fill.fill_price
            no_cost = matched_shares * no_fill.fill_price
            total_cost = yes_cost + no_cost
            
            # IMMEDIATE REDEMPTION at $1 per matched pair
            redemption_value, realized_pnl = self._redeem_full_set(matched_shares, total_cost)
            
            self.executions_success += 1
            self.total_realized_pnl += realized_pnl
            self.total_redemption_profit += realized_pnl
            
            # Handle excess shares (partial fill mismatch)
            if excess_yes > 0 or excess_no > 0:
                self.executions_partial += 1
                # In real trading, we'd unwind excess. For paper, just log it.
                logger.debug(f"Partial mismatch: excess_yes={excess_yes:.2f} excess_no={excess_no:.2f}")
            
            logger.info(
                f"SUCCESS+REDEEM: {opportunity.market.slug} | "
                f"shares={matched_shares:.2f} cost={total_cost:.4f} "
                f"redemption={redemption_value:.4f} PnL={realized_pnl:.4f}"
            )
            
            return ExecutionResult(
                opportunity=opportunity,
                timestamp=datetime.now(),
                status=ExecutionStatus.SUCCESS,
                yes_fill=yes_fill,
                no_fill=no_fill,
                shares_filled=matched_shares,
                total_cost=total_cost,
                redemption_value=redemption_value,
                realized_pnl=realized_pnl,
                excess_yes_shares=excess_yes,
                excess_no_shares=excess_no,
                total_time_ms=total_time_ms,
            )
        
        elif yes_filled or no_filled:
            # ONE LEG: Need to unwind using VWAP at bid
            self.executions_one_leg += 1
            
            filled_leg = yes_fill if yes_filled else no_fill
            filled_leg.status = FillStatus.UNWOUND
            
            # Use VWAP-based unwind
            unwind = self._unwind_leg_vwap(filled_leg)
            
            realized_pnl = -unwind.unwind_loss
            self.total_realized_pnl += realized_pnl
            self.total_unwind_loss += unwind.unwind_loss
            
            # Disable market if unwind loss exceeds threshold
            if unwind.unwind_loss_pct > self.config.max_unwind_loss_pct:
                if self.scanner:
                    self.scanner.disable_market(
                        opportunity.market.market_id,
                        self.config.market_disable_minutes
                    )
            
            logger.warning(
                f"ONE-LEG UNWIND: {opportunity.market.slug} | "
                f"filled={filled_leg.side} shares={filled_leg.filled_shares:.2f} | "
                f"cost={unwind.unwind_cost:.4f} proceeds={unwind.unwind_proceeds:.4f} | "
                f"loss={unwind.unwind_loss:.4f} ({unwind.unwind_loss_pct:.2%})"
            )
            
            return ExecutionResult(
                opportunity=opportunity,
                timestamp=datetime.now(),
                status=ExecutionStatus.ONE_LEG_UNWOUND,
                yes_fill=yes_fill,
                no_fill=no_fill,
                unwind=unwind,
                shares_filled=0,
                total_cost=unwind.unwind_cost,
                realized_pnl=realized_pnl,
                total_time_ms=total_time_ms,
            )
        
        else:
            # BOTH FAILED: No harm done
            self.executions_failed += 1
            
            logger.debug(f"BOTH FAILED: {opportunity.market.slug}")
            
            return ExecutionResult(
                opportunity=opportunity,
                timestamp=datetime.now(),
                status=ExecutionStatus.BOTH_FAILED,
                yes_fill=yes_fill,
                no_fill=no_fill,
                total_time_ms=total_time_ms,
            )
    
    def get_stats(self) -> Dict:
        """Get execution statistics."""
        total = max(1, self.executions_total)
        success_rate = (self.executions_success / total) * 100
        one_leg_rate = (self.executions_one_leg / total) * 100
        partial_rate = (self.executions_partial / total) * 100
        
        return {
            "executions_total": self.executions_total,
            "executions_success": self.executions_success,
            "executions_one_leg": self.executions_one_leg,
            "executions_partial": self.executions_partial,
            "executions_failed": self.executions_failed,
            "success_rate_pct": round(success_rate, 2),
            "one_leg_rate_pct": round(one_leg_rate, 2),
            "partial_rate_pct": round(partial_rate, 2),
            "total_realized_pnl": round(self.total_realized_pnl, 6),
            "total_redemption_profit": round(self.total_redemption_profit, 6),
            "total_unwind_loss": round(self.total_unwind_loss, 6),
        }


def main():
    """Test executor with VWAP-based execution and instant redemption."""
    logging.basicConfig(level=logging.INFO)
    
    from .scanner import ArbScanner
    
    config = ArbConfig()
    config.min_edge = 0.006  # Use proper positive threshold
    config.order_size_usd = 10.0
    
    scanner = ArbScanner(config)
    executor = PaperExecutor(config, scanner)
    
    print("\n=== Discovering and Scanning Markets (VWAP-based) ===")
    markets = scanner.discovery.discover_all(max_markets=30)
    opportunities = scanner.get_actionable_opportunities(markets)
    
    print(f"\nFound {len(opportunities)} actionable opportunities")
    
    if not opportunities:
        print("No actionable opportunities with positive edge_exec.")
        print("This is expected - real arb is rare!")
        # Show best edges anyway
        all_opps = scanner.scan_all(markets)[:5]
        if all_opps:
            print(f"\nTop 5 edges (not actionable):")
            for opp in all_opps:
                print(f"  {opp.market.slug[:40]}: edge_exec={opp.edge_exec:.4f}")
        return
    
    print("\n=== Executing Paper Trades with Instant Redemption ===\n")
    
    for opp in opportunities[:5]:
        result = executor.execute(opp)
        print(f"Market: {opp.market.slug[:40]}")
        print(f"  Status: {result.status.value}")
        print(f"  Edge: L1={opp.edge_l1:.4f} EXEC={opp.edge_exec:.4f}")
        print(f"  Shares: {result.shares_filled:.2f}")
        print(f"  Cost: ${result.total_cost:.4f}")
        print(f"  Redemption: ${result.redemption_value:.4f}")
        print(f"  Realized P&L: ${result.realized_pnl:.4f}")
        print()
    
    print("\n=== Execution Stats ===")
    stats = executor.get_stats()
    for key, value in stats.items():
        print(f"  {key}: {value}")


if __name__ == "__main__":
    main()

