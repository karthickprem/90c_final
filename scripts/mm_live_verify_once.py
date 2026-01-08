"""
V12 PRODUCTION VERIFICATION SCRIPT
===================================

Capital protection is #1. This script verifies a single round-trip
with strict safety rules.

EXIT CODES:
0 = PASS - Complete round-trip verified (entry + exit fills)
1 = FAIL - Safety violation or error
2 = NO_TRADE_SAFE - Conditions unsuitable, correctly refused to trade
3 = STATE_DESYNC - Fills don't match REST positions

SAFETY RULES:
- Trade ingestion boundary: ignore trades before window_start
- Strict regime: mid in [0.40, 0.60]
- Cap enforcement: MAX_SHARES and MAX_USDC_LOCKED
- No pyramiding: 1 entry order max, no entries if inventory exists
- Exit management: automatic, not manual
"""

import os
import sys
import time
import hashlib
from enum import Enum
from dataclasses import dataclass
from typing import Optional, Set, Dict, List

# Force UTF-8 output
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

# Add project root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mm_bot.config import Config
from mm_bot.clob import ClobWrapper
from mm_bot.market import MarketResolver
import requests


# ============================================================================
# V12 CONFIGURATION
# ============================================================================

# Caps (strict for verification)
MAX_USDC_LOCKED = 1.50
MAX_SHARES = 3  # This is the HARD cap
QUOTE_SIZE = 6  # Will be clamped to MAX_SHARES

# Regime (strict for verification)
ENTRY_MID_MIN = 0.40
ENTRY_MID_MAX = 0.60
MAX_SPREAD_CENTS = 3
MAX_VOL_10S_CENTS = 8
MIN_TIME_TO_END_SECS = 180

# Timing
MAX_RUNTIME_SECS = 300  # 5 minutes max
TICK_INTERVAL_SECS = 0.25
LOG_INTERVAL_SECS = 5
EXIT_REPRICE_INTERVAL_SECS = 5

# Adverse budget for rebate check
ADVERSE_BUDGET_PER_SHARE = 0.02  # 2 cents


class VerifierState(Enum):
    WAIT_ENTRY = "WAIT_ENTRY"
    WAIT_EXIT = "WAIT_EXIT"
    DONE = "DONE"
    STOPPED = "STOPPED"


@dataclass
class ConfirmedFill:
    trade_id: str
    side: str
    price: float
    size: float
    timestamp: float
    token_id: str


class V12Verifier:
    """
    Production-grade single round-trip verifier.
    """
    
    def __init__(self):
        self.config = Config.from_env()
        self.clob = ClobWrapper(self.config)
        self.resolver = MarketResolver(self.config)
        self.live = os.environ.get("LIVE", "0") == "1"
        
        # Market info
        self.yes_token: str = ""
        self.no_token: str = ""
        self.market_tokens: Set[str] = set()
        self.market_end_time: int = 0
        
        # V12: Trade ingestion boundary
        self.window_start_ts: float = 0.0
        self.seen_trade_ids: Set[str] = set()
        
        # State machine
        self.state = VerifierState.WAIT_ENTRY
        self.entry_order_id: Optional[str] = None
        self.exit_order_id: Optional[str] = None
        
        # Confirmed fills (from trades API only)
        self.entry_fill: Optional[ConfirmedFill] = None
        self.exit_fill: Optional[ConfirmedFill] = None
        self.confirmed_shares: float = 0.0
        self.entry_price: float = 0.0
        
        # Exit ladder tracking
        self.exit_posted_at: float = 0.0
        self.exit_reprice_count: int = 0
        
        # Metrics
        self.orders_posted: int = 0
        self.orders_cancelled: int = 0
        self.balance_errors: int = 0
        self.safety_violations: List[str] = []
        
        # Mid history for volatility
        self.mid_history: List[float] = []
        
        # Timing
        self.start_time: float = 0.0
        self.last_log_time: float = 0.0
    
    def log(self, msg: str):
        print(f"[V12] {msg}", flush=True)
    
    def log_violation(self, msg: str):
        self.safety_violations.append(msg)
        print(f"[SAFETY] {msg}", flush=True)
    
    def stop_trading(self, reason: str) -> int:
        """Cancel all orders and stop."""
        self.log(f"STOP_TRADING: {reason}")
        self.state = VerifierState.STOPPED
        
        if self.live:
            try:
                self.clob.cancel_all()
                self.log("Cancelled all orders")
            except Exception as e:
                self.log(f"Error cancelling: {e}")
        
        return 1
    
    # ========================================================================
    # V12 FIX A: Trade Ingestion Boundary
    # ========================================================================
    
    def reset_fill_tracking(self):
        """Reset fill tracking for new window."""
        self.window_start_ts = time.time() - 2  # 2s skew allowance
        self.seen_trade_ids.clear()
        self.entry_fill = None
        self.exit_fill = None
        self.confirmed_shares = 0.0
        self.log(f"Trade boundary set: ignore trades before {self.window_start_ts:.0f}")
    
    def poll_fills(self) -> List[ConfirmedFill]:
        """
        Poll trades API with V12 boundary filtering.
        Only accepts trades:
        - After window_start_ts
        - For current market tokens
        - Not already seen
        """
        new_fills = []
        
        try:
            r = requests.get(
                "https://data-api.polymarket.com/trades",
                params={"user": self.config.api.proxy_address, "limit": 20},
                timeout=10
            )
            
            if r.status_code != 200:
                return []
            
            trades = r.json()
            
            for t in trades:
                tx_hash = t.get("transactionHash", "")
                token_id = t.get("asset", "")
                side = t.get("side", "").upper()
                size = float(t.get("size", 0) or 0)
                price = float(t.get("price", 0) or 0)
                timestamp = float(t.get("timestamp", 0) or 0)
                
                # V12: Boundary check
                if timestamp < self.window_start_ts:
                    continue
                
                # V12: Token filter
                if token_id not in self.market_tokens:
                    continue
                
                # Dedupe key
                trade_id = f"{tx_hash}_{token_id[-8:]}_{timestamp}_{side}_{size}_{price}"
                if trade_id in self.seen_trade_ids:
                    continue
                
                # V12: Validate tx_hash exists
                if not tx_hash or len(tx_hash) < 10:
                    self.log_violation(f"Trade missing txHash: {side} {size} @ {price}")
                    return []  # Stop processing
                
                self.seen_trade_ids.add(trade_id)
                
                fill = ConfirmedFill(
                    trade_id=trade_id,
                    side=side,
                    price=price,
                    size=size,
                    timestamp=timestamp,
                    token_id=token_id
                )
                new_fills.append(fill)
                
        except Exception as e:
            self.log(f"Trades API error: {e}")
        
        return new_fills
    
    # ========================================================================
    # V12 FIX B: Regime + Rebate Viability
    # ========================================================================
    
    def get_fee_per_100_shares(self, price: float) -> float:
        """Get taker fee for 100 shares at given price (from Polymarket fee table)."""
        # Simplified fee curve: max at 0.50, zero at extremes
        # Fee = 0.78 * 4 * price * (1 - price) for 100 shares
        return 0.78 * 4 * price * (1 - price)
    
    def expected_rebate_total(self, shares: float, entry_price: float, exit_price: float) -> float:
        """
        Estimate total maker rebate for round-trip.
        Rebates are funded from taker fees, assume ~50% redistribution.
        """
        entry_fee_100 = self.get_fee_per_100_shares(entry_price)
        exit_fee_100 = self.get_fee_per_100_shares(exit_price)
        
        # Rebate is portion of fee collected
        rebate_rate = 0.3  # Conservative estimate
        entry_rebate = (shares / 100) * entry_fee_100 * rebate_rate
        exit_rebate = (shares / 100) * exit_fee_100 * rebate_rate
        
        return entry_rebate + exit_rebate
    
    def get_vol_10s(self) -> float:
        """Get 10-second volatility in cents."""
        if len(self.mid_history) < 10:
            return 0.0
        recent = self.mid_history[-40:]  # ~10s at 250ms ticks
        if len(recent) < 2:
            return 0.0
        return (max(recent) - min(recent)) * 100
    
    def check_entry_conditions(self, mid: float, spread: float, time_to_end: int) -> tuple:
        """
        V12: Check all entry conditions.
        Returns (can_trade, reason)
        """
        # Regime check
        if mid < ENTRY_MID_MIN or mid > ENTRY_MID_MAX:
            return (False, f"mid {mid:.2f} outside [{ENTRY_MID_MIN}, {ENTRY_MID_MAX}]")
        
        # Spread check
        spread_cents = spread * 100
        if spread_cents > MAX_SPREAD_CENTS:
            return (False, f"spread {spread_cents:.1f}c > {MAX_SPREAD_CENTS}c")
        
        # Volatility check
        vol = self.get_vol_10s()
        if vol > MAX_VOL_10S_CENTS:
            return (False, f"vol {vol:.1f}c > {MAX_VOL_10S_CENTS}c")
        
        # Time check
        if time_to_end < MIN_TIME_TO_END_SECS:
            return (False, f"time_to_end {time_to_end}s < {MIN_TIME_TO_END_SECS}s")
        
        # Rebate viability check
        shares = min(QUOTE_SIZE, MAX_SHARES)  # V12: Clamp to cap
        expected_rebate = self.expected_rebate_total(shares, mid, mid)
        required_rebate = shares * ADVERSE_BUDGET_PER_SHARE
        
        if expected_rebate < required_rebate:
            return (False, f"rebate ${expected_rebate:.4f} < adverse ${required_rebate:.4f}")
        
        return (True, "OK")
    
    # ========================================================================
    # V12 FIX C: Cap Enforcement
    # ========================================================================
    
    def check_caps(self, mid: float) -> bool:
        """
        Check exposure caps. Returns True if within caps.
        """
        # Shares cap
        if self.confirmed_shares > MAX_SHARES:
            self.log_violation(f"SHARES CAP BREACH: {self.confirmed_shares} > {MAX_SHARES}")
            return False
        
        # USDC cap
        exposure = self.confirmed_shares * mid
        if exposure > MAX_USDC_LOCKED:
            self.log_violation(f"USDC CAP BREACH: ${exposure:.2f} > ${MAX_USDC_LOCKED}")
            return False
        
        return True
    
    def enforce_caps_on_fill(self, mid: float):
        """V12: Enforce caps after fill. Cancel entries if breached."""
        if not self.check_caps(mid):
            self.log("Cap breached - cancelling entries, entering EXIT_ONLY")
            if self.entry_order_id and self.live:
                try:
                    self.clob.cancel_order(self.entry_order_id)
                    self.entry_order_id = None
                except:
                    pass
            self.state = VerifierState.WAIT_EXIT
    
    # ========================================================================
    # V12 FIX D: Exit Management
    # ========================================================================
    
    def get_exit_price(self, best_bid: float, time_to_end: int) -> float:
        """
        Get exit price based on ladder.
        Reprice: entry+1c -> entry -> entry-1c -> entry-2c -> bid (emergency)
        """
        if time_to_end < 60:
            # Emergency: cross spread
            return best_bid
        
        if self.exit_reprice_count == 0:
            return min(0.99, self.entry_price + 0.01)  # TP +1c
        elif self.exit_reprice_count == 1:
            return self.entry_price  # Scratch
        elif self.exit_reprice_count == 2:
            return max(0.01, self.entry_price - 0.01)  # -1c
        elif self.exit_reprice_count == 3:
            return max(0.01, self.entry_price - 0.02)  # -2c
        else:
            return best_bid  # Cross
    
    def manage_exit(self, best_bid: float, time_to_end: int):
        """V12: Manage exit order with ladder repricing."""
        if self.confirmed_shares < 0.01:
            return
        
        now = time.time()
        
        # Check if need to reprice
        should_reprice = False
        if not self.exit_order_id:
            should_reprice = True
        elif now - self.exit_posted_at > EXIT_REPRICE_INTERVAL_SECS:
            should_reprice = True
        
        if not should_reprice:
            return
        
        # Cancel existing exit
        if self.exit_order_id and self.live:
            try:
                self.clob.cancel_order(self.exit_order_id)
                self.orders_cancelled += 1
            except:
                pass
            self.exit_order_id = None
        
        # Get new price
        exit_price = self.get_exit_price(best_bid, time_to_end)
        
        # V12: Clamp size to confirmed shares
        exit_size = int(self.confirmed_shares)
        if exit_size < 5:
            self.log(f"Dust position: {self.confirmed_shares:.2f} < 5, cannot exit via API")
            return
        
        if self.live:
            try:
                result = self.clob.post_order(
                    token_id=self.entry_fill.token_id if self.entry_fill else self.yes_token,
                    side="SELL",
                    price=exit_price,
                    size=exit_size,
                    post_only=(time_to_end >= 60)  # Taker only in emergency
                )
                
                if result.success:
                    self.exit_order_id = result.order_id
                    self.exit_posted_at = now
                    self.exit_reprice_count += 1
                    self.orders_posted += 1
                    self.log(f"EXIT posted: SELL {exit_size} @ {exit_price:.4f} (reprice #{self.exit_reprice_count})")
                else:
                    if result.error and "balance" in str(result.error).lower():
                        self.balance_errors += 1
                        self.log_violation(f"BALANCE_ERROR on SELL: {result.error}")
            except Exception as e:
                if "balance" in str(e).lower():
                    self.balance_errors += 1
                    self.log_violation(f"BALANCE_ERROR on SELL: {e}")
                else:
                    self.log(f"Exit order error: {e}")
    
    # ========================================================================
    # V12 FIX E: Stop Conditions
    # ========================================================================
    
    def check_stop_conditions(self) -> Optional[str]:
        """Check all stop conditions. Returns reason if should stop."""
        # Balance errors
        if self.balance_errors > 0:
            return "BALANCE_ERROR on exit"
        
        # Safety violations with missing trade_id
        for v in self.safety_violations:
            if "txHash" in v or "trade_id" in v.lower():
                return v
        
        return None
    
    # ========================================================================
    # MAIN RUN LOOP
    # ========================================================================
    
    def run(self) -> int:
        """
        Run verification.
        Returns exit code.
        """
        print("=" * 60)
        print("  V12 PRODUCTION VERIFICATION")
        print("=" * 60)
        print(f"  MAX_USDC_LOCKED: ${MAX_USDC_LOCKED:.2f}")
        print(f"  MAX_SHARES: {MAX_SHARES}")
        print(f"  REGIME: [{ENTRY_MID_MIN}, {ENTRY_MID_MAX}]")
        print(f"  MODE: {'LIVE' if self.live else 'DRYRUN'}")
        print("=" * 60)
        
        # Resolve market
        market = self.resolver.resolve_market()
        if not market:
            self.log("No active market")
            return 2
        
        self.yes_token = market.yes_token_id
        self.no_token = market.no_token_id
        self.market_tokens = {self.yes_token, self.no_token}
        self.market_end_time = market.end_time
        
        self.log(f"Market: {market.question}")
        self.log(f"Ends in: {market.time_str}")
        
        # V12: Reset fill tracking with boundary
        self.reset_fill_tracking()
        
        # Cleanup
        if self.live:
            self.clob.cancel_all()
            self.log("Cancelled all orders")
        
        # Get starting balance
        bal = self.clob.get_balance()
        start_usdc = bal.get("usdc", 0)
        self.log(f"Start balance: ${start_usdc:.2f}")
        
        # Main loop
        self.start_time = time.time()
        self.last_log_time = time.time()
        
        while True:
            now = time.time()
            elapsed = now - self.start_time
            
            # Timeout
            if elapsed > MAX_RUNTIME_SECS:
                self.log("Timeout reached")
                return self.stop_trading("Timeout")
            
            # Check stop conditions
            stop_reason = self.check_stop_conditions()
            if stop_reason:
                return self.stop_trading(stop_reason)
            
            # Get book
            yes_book = self.clob.get_order_book(self.yes_token)
            if not yes_book or yes_book.best_bid < 0.01:
                time.sleep(TICK_INTERVAL_SECS)
                continue
            
            mid = (yes_book.best_bid + yes_book.best_ask) / 2
            spread = yes_book.best_ask - yes_book.best_bid
            time_to_end = self.market_end_time - int(time.time())
            
            # Update mid history
            self.mid_history.append(mid)
            if len(self.mid_history) > 100:
                self.mid_history = self.mid_history[-100:]
            
            # Poll fills
            new_fills = self.poll_fills()
            
            for fill in new_fills:
                if fill.side == "BUY":
                    # V12: Check for unexpected entry after we have inventory
                    if self.confirmed_shares > 0:
                        self.log_violation("Unexpected BUY while holding inventory (pyramid)")
                    
                    self.entry_fill = fill
                    self.confirmed_shares += fill.size
                    self.entry_price = fill.price
                    self.state = VerifierState.WAIT_EXIT
                    
                    self.log(f"ENTRY FILL: BUY {fill.size:.2f} @ {fill.price:.4f} trade_id={fill.trade_id[:40]}...")
                    
                    # V12: Enforce caps after fill
                    self.enforce_caps_on_fill(mid)
                    
                elif fill.side == "SELL":
                    if not self.entry_fill:
                        # V12: Exit without entry is a boundary violation
                        self.log_violation(f"EXIT without matching entry: {fill.size} @ {fill.price}")
                        return 3  # STATE_DESYNC
                    
                    self.exit_fill = fill
                    self.confirmed_shares -= fill.size
                    
                    self.log(f"EXIT FILL: SELL {fill.size:.2f} @ {fill.price:.4f} trade_id={fill.trade_id[:40]}...")
                    
                    if self.confirmed_shares <= 0.01:
                        self.state = VerifierState.DONE
            
            # State machine
            if self.state == VerifierState.WAIT_ENTRY:
                # Check conditions
                can_trade, reason = self.check_entry_conditions(mid, spread, time_to_end)
                
                if not can_trade:
                    if now - self.last_log_time > LOG_INTERVAL_SECS:
                        self.log(f"NO_TRADE: {reason}")
                        self.last_log_time = now
                    
                    # If we've waited 2 minutes with no trade opportunity, exit safely
                    if elapsed > 120:
                        self.log("NO_TRADE_SAFE: Conditions never suitable")
                        return 2
                else:
                    # Post entry
                    if not self.entry_order_id and self.live:
                        # V12: Clamp to MAX_SHARES
                        entry_size = min(QUOTE_SIZE, MAX_SHARES)
                        
                        # V12: Check cap before posting
                        exposure = entry_size * mid
                        if exposure > MAX_USDC_LOCKED:
                            self.log(f"Exposure ${exposure:.2f} > cap ${MAX_USDC_LOCKED}")
                        else:
                            try:
                                result = self.clob.post_order(
                                    token_id=self.yes_token,
                                    side="BUY",
                                    price=yes_book.best_bid,
                                    size=entry_size,
                                    post_only=True
                                )
                                if result.success:
                                    self.entry_order_id = result.order_id
                                    self.orders_posted += 1
                                    self.log(f"ENTRY posted: BUY {entry_size} @ {yes_book.best_bid:.4f}")
                            except Exception as e:
                                self.log(f"Entry error: {e}")
            
            elif self.state == VerifierState.WAIT_EXIT:
                # Manage exit with ladder
                self.manage_exit(yes_book.best_bid, time_to_end)
            
            elif self.state == VerifierState.DONE:
                # Success!
                pnl = (self.exit_fill.price - self.entry_fill.price) * self.exit_fill.size
                pnl_str = f"+${pnl:.4f}" if pnl >= 0 else f"-${abs(pnl):.4f}"
                
                print("\n" + "=" * 60)
                print("  VERIFICATION COMPLETE")
                print("=" * 60)
                print(f"  Entry: BUY {self.entry_fill.size:.2f} @ {self.entry_fill.price:.4f}")
                print(f"  Exit:  SELL {self.exit_fill.size:.2f} @ {self.exit_fill.price:.4f}")
                print(f"  [ROUND-TRIP] PnL = {pnl_str} (from fills)")
                print(f"  Entry trade_id: {self.entry_fill.trade_id[:50]}...")
                print(f"  Exit trade_id:  {self.exit_fill.trade_id[:50]}...")
                print("-" * 60)
                print("  RESULT: PASS - Verifier complete")
                print("=" * 60)
                
                return 0
            
            # Tick log
            if now - self.last_log_time > LOG_INTERVAL_SECS:
                vol = self.get_vol_10s()
                self.log(f"TICK: mid={mid:.2f} spread={spread*100:.1f}c vol={vol:.1f}c "
                         f"state={self.state.value} pos={self.confirmed_shares:.1f} "
                         f"time_left={time_to_end}s")
                self.last_log_time = now
            
            time.sleep(TICK_INTERVAL_SECS)
        
        return 1


if __name__ == "__main__":
    verifier = V12Verifier()
    exit_code = verifier.run()
    print(f"\n[EXIT] Code {exit_code}")
    sys.exit(exit_code)
