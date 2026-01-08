"""
V14 PRODUCTION VERIFICATION SCRIPT
===================================

Capital protection is #1. This script verifies a single round-trip
with strict safety rules.

EXIT CODES:
0 = PASS - Complete round-trip verified (BUY + SELL fills with valid trade_ids)
1 = FAIL - Invariant violation (KILL_SWITCH triggered) or DUST_UNEXITABLE
2 = NO_TRADE_SAFE - Conditions unsuitable OR invalid config

CRITICAL PROTOCOL CONSTRAINT:
- Polymarket MIN_ORDER_SHARES = 5
- Any config with MAX_SHARES < 5 or QUOTE_SIZE < 5 is INVALID
- Partial fills < 5 shares require ACCUMULATE state to top-up

STATES:
- WAIT_ENTRY: Waiting for conditions + posting entry
- ACCUMULATE: Have partial fill < MIN_ORDER_SHARES, need to top-up
- WAIT_EXIT: Have >= MIN_ORDER_SHARES, managing exit
- DONE: Round-trip complete
- KILLED: Safety violation
"""

import os
import sys
import time
from enum import Enum
from typing import Optional

# Force UTF-8 output
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

# Add project root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mm_bot.config import Config
from mm_bot.clob import ClobWrapper
from mm_bot.market import MarketResolver
from mm_bot.volatility import VolatilityTracker, VolatilitySnapshot
from mm_bot.fill_tracker_v13 import FillTrackerV13, FillTrackerError


# ============================================================================
# V14 CONFIGURATION - PROTOCOL CONSTRAINTS
# ============================================================================

# CRITICAL: Polymarket minimum order size
MIN_ORDER_SHARES = 5

# Epsilon for float comparisons
EPSILON = 1e-6

# Caps (MUST be >= MIN_ORDER_SHARES)
MAX_USDC_LOCKED = float(os.environ.get("MM_MAX_USDC", "3.00"))
MAX_SHARES = int(float(os.environ.get("MM_MAX_SHARES", "6")))
QUOTE_SIZE = int(float(os.environ.get("MM_QUOTE_SIZE", "6")))

# Regime (STRICT for verification) - use epsilon for boundaries
ENTRY_MID_MIN = 0.45
ENTRY_MID_MAX = 0.55
MAX_SPREAD_CENTS = 3
MAX_VOL_10S_CENTS = float(os.environ.get("MM_VOL_10S_CENTS", "8.0"))
MIN_TIME_TO_END_SECS = 180

# Timing
MAX_RUNTIME_SECS = int(os.environ.get("MM_MAX_RUNTIME", "600"))
TICK_INTERVAL_SECS = 0.25
LOG_INTERVAL_SECS = 5
EXIT_REPRICE_INTERVAL_SECS = 5
FLATTEN_DEADLINE_SECS = 60
ACCUMULATE_TIMEOUT_SECS = 60  # Max time to accumulate before FAIL


class VerifierState(Enum):
    WAIT_ENTRY = "WAIT_ENTRY"
    ACCUMULATE = "ACCUMULATE"  # NEW: Partial fill < MIN_ORDER_SHARES
    WAIT_EXIT = "WAIT_EXIT"
    DONE = "DONE"
    KILLED = "KILLED"


class ConfigError(Exception):
    """Invalid configuration."""
    pass


def validate_config() -> tuple:
    """
    Validate configuration against protocol constraints.
    Returns (valid, error_message)
    """
    errors = []
    
    if MAX_SHARES < MIN_ORDER_SHARES:
        errors.append(f"MAX_SHARES({MAX_SHARES}) < MIN_ORDER_SHARES({MIN_ORDER_SHARES})")
    
    if QUOTE_SIZE < MIN_ORDER_SHARES:
        errors.append(f"QUOTE_SIZE({QUOTE_SIZE}) < MIN_ORDER_SHARES({MIN_ORDER_SHARES})")
    
    # Check exposure is reasonable
    min_exposure = MIN_ORDER_SHARES * 0.50  # At mid = 0.50
    if MAX_USDC_LOCKED < min_exposure:
        errors.append(f"MAX_USDC_LOCKED(${MAX_USDC_LOCKED:.2f}) < min exposure(${min_exposure:.2f})")
    
    if errors:
        return (False, "INVALID_CONFIG: " + "; ".join(errors))
    
    return (True, "OK")


def in_range(value: float, lo: float, hi: float) -> bool:
    """Check if value is in [lo, hi] with epsilon tolerance."""
    return value >= (lo - EPSILON) and value <= (hi + EPSILON)


class V14Verifier:
    """
    Production-grade single round-trip verifier with ACCUMULATE state.
    """
    
    def __init__(self):
        self.config = Config.from_env()
        self.clob = ClobWrapper(self.config)
        self.resolver = MarketResolver(self.config)
        self.live = os.environ.get("LIVE", "0") == "1"
        
        # Market info
        self.yes_token: str = ""
        self.no_token: str = ""
        self.market_end_time: int = 0
        
        # State machine
        self.state = VerifierState.WAIT_ENTRY
        self.entry_order_id: Optional[str] = None
        self.exit_order_id: Optional[str] = None
        self.accumulate_order_id: Optional[str] = None
        
        # Fill tracker (V13 - no synthetic fills)
        self.fill_tracker: Optional[FillTrackerV13] = None
        
        # Volatility tracker (time-based)
        self.vol_tracker = VolatilityTracker(window_secs=10.0)
        
        # Entry tracking
        self.entry_price: float = 0.0
        self.entry_token: str = ""
        self.accumulate_start_time: float = 0.0
        
        # Exit ladder
        self.exit_reprice_count: int = 0
        self.exit_posted_at: float = 0.0
        
        # Timing
        self.start_time: float = 0.0
        self.last_log_time: float = 0.0
        
        # Kill switch state
        self.kill_reason: str = ""
    
    def log(self, msg: str):
        print(f"[V14] {msg}", flush=True)
    
    def kill_switch(self, reason: str) -> int:
        """Trigger kill switch - cancel all orders and exit with code 1."""
        self.log(f"KILL_SWITCH: {reason}")
        self.kill_reason = reason
        self.state = VerifierState.KILLED
        
        if self.live:
            try:
                self.clob.cancel_all()
                self.log("Cancelled all orders")
            except Exception as e:
                self.log(f"Error cancelling: {e}")
        
        return 1
    
    def update_volatility(self, mid: float) -> VolatilitySnapshot:
        """Update volatility tracker and return snapshot."""
        return self.vol_tracker.update(mid)
    
    def check_entry_conditions(
        self,
        mid: float,
        spread: float,
        vol: VolatilitySnapshot,
        time_to_end: int
    ) -> tuple:
        """
        Check all entry conditions.
        Returns (can_trade, reason)
        """
        # Regime check with epsilon
        if not in_range(mid, ENTRY_MID_MIN, ENTRY_MID_MAX):
            return (False, f"mid {mid:.4f} outside [{ENTRY_MID_MIN}, {ENTRY_MID_MAX}]")
        
        # Spread check
        spread_cents = spread * 100
        if spread_cents > MAX_SPREAD_CENTS:
            return (False, f"spread {spread_cents:.1f}c > {MAX_SPREAD_CENTS}c")
        
        # Volatility check
        if vol.vol_10s_cents > MAX_VOL_10S_CENTS:
            return (False, f"vol {vol.vol_10s_cents:.1f}c > {MAX_VOL_10S_CENTS}c")
        
        # Time check
        if time_to_end < MIN_TIME_TO_END_SECS:
            return (False, f"time_to_end {time_to_end}s < {MIN_TIME_TO_END_SECS}s")
        
        return (True, "OK")
    
    def get_exit_price(self, best_bid: float, best_ask: float, time_to_end: int) -> tuple:
        """
        Get exit price based on ladder.
        Returns (price, is_taker)
        """
        if time_to_end < FLATTEN_DEADLINE_SECS:
            return (best_bid, True)
        
        if self.exit_reprice_count == 0:
            tp_price = min(0.99, self.entry_price + 0.01)
            ask_minus = max(0.01, best_ask - 0.01)
            return (min(tp_price, ask_minus), False)
        elif self.exit_reprice_count == 1:
            return (self.entry_price, False)
        elif self.exit_reprice_count == 2:
            return (max(0.01, self.entry_price - 0.01), False)
        elif self.exit_reprice_count == 3:
            return (max(0.01, self.entry_price - 0.02), False)
        else:
            return (best_bid, True)
    
    def manage_exit(self, best_bid: float, best_ask: float, time_to_end: int) -> bool:
        """Manage exit order with ladder repricing."""
        if not self.fill_tracker:
            return False
        
        confirmed_shares = self.fill_tracker.get_confirmed_shares(self.entry_token)
        
        # CRITICAL: Only exit if we have >= MIN_ORDER_SHARES
        if confirmed_shares < MIN_ORDER_SHARES:
            self.log(f"Cannot exit: {confirmed_shares:.2f} < MIN_ORDER_SHARES({MIN_ORDER_SHARES})")
            return False
        
        now = time.time()
        
        should_reprice = False
        if not self.exit_order_id:
            should_reprice = True
        elif now - self.exit_posted_at > EXIT_REPRICE_INTERVAL_SECS:
            should_reprice = True
        
        if not should_reprice:
            return False
        
        if self.exit_order_id and self.live:
            try:
                self.clob.cancel_order(self.exit_order_id)
            except:
                pass
            self.exit_order_id = None
        
        exit_price, is_taker = self.get_exit_price(best_bid, best_ask, time_to_end)
        exit_size = int(confirmed_shares)
        
        if self.live:
            try:
                result = self.clob.post_order(
                    token_id=self.entry_token,
                    side="SELL",
                    price=exit_price,
                    size=exit_size,
                    post_only=(not is_taker)
                )
                
                if result.success:
                    self.exit_order_id = result.order_id
                    self.exit_posted_at = now
                    self.exit_reprice_count += 1
                    taker_str = " (TAKER)" if is_taker else ""
                    self.log(f"EXIT posted: SELL {exit_size} @ {exit_price:.4f}{taker_str}")
                    return True
                else:
                    error = str(result.error).lower() if result.error else ""
                    if "balance" in error:
                        self.kill_switch(f"BALANCE_ERROR on SELL: {result.error}")
                    return False
            except Exception as e:
                error = str(e).lower()
                if "balance" in error:
                    self.kill_switch(f"BALANCE_ERROR on SELL: {e}")
                else:
                    self.log(f"Exit order error: {e}")
                return False
        
        return False
    
    def manage_accumulate(self, best_bid: float, time_to_end: int) -> bool:
        """
        Manage accumulate state - top up to MIN_ORDER_SHARES.
        Returns True if accumulate order posted.
        """
        if not self.fill_tracker:
            return False
        
        confirmed_shares = self.fill_tracker.get_confirmed_shares(self.entry_token)
        needed = MIN_ORDER_SHARES - confirmed_shares
        
        if needed <= 0:
            # Already have enough, transition to WAIT_EXIT
            return False
        
        # Check if we can top-up within caps
        potential_total = confirmed_shares + needed
        if potential_total > MAX_SHARES:
            self.log(f"Cannot accumulate: {confirmed_shares:.2f} + {needed:.2f} > MAX_SHARES({MAX_SHARES})")
            return False
        
        # Check time - must have enough time to exit after accumulating
        if time_to_end < MIN_TIME_TO_END_SECS:
            self.log(f"Cannot accumulate: time_to_end({time_to_end}s) < MIN_TIME({MIN_TIME_TO_END_SECS}s)")
            return False
        
        # Check accumulate timeout
        if time.time() - self.accumulate_start_time > ACCUMULATE_TIMEOUT_SECS:
            self.log(f"Accumulate timeout after {ACCUMULATE_TIMEOUT_SECS}s")
            return False
        
        # Post accumulate order
        if not self.accumulate_order_id and self.live:
            # Round up to ensure we get to MIN_ORDER_SHARES
            accum_size = max(MIN_ORDER_SHARES, int(needed) + 1)
            
            try:
                result = self.clob.post_order(
                    token_id=self.entry_token,
                    side="BUY",
                    price=best_bid,
                    size=accum_size,
                    post_only=True
                )
                
                if result.success:
                    self.accumulate_order_id = result.order_id
                    self.log(f"ACCUMULATE posted: BUY {accum_size} @ {best_bid:.4f} (need {needed:.2f} more)")
                    return True
            except Exception as e:
                self.log(f"Accumulate order error: {e}")
        
        return False
    
    def run(self) -> int:
        """Run verification. Returns exit code."""
        
        # ====================================================================
        # CONFIG VALIDATION (NON-NEGOTIABLE)
        # ====================================================================
        valid, error_msg = validate_config()
        if not valid:
            print("=" * 60)
            print("  V14 PRODUCTION VERIFICATION - CONFIG ERROR")
            print("=" * 60)
            print(f"  {error_msg}")
            print()
            print(f"  MIN_ORDER_SHARES: {MIN_ORDER_SHARES} (Polymarket protocol)")
            print(f"  MAX_SHARES: {MAX_SHARES}")
            print(f"  QUOTE_SIZE: {QUOTE_SIZE}")
            print(f"  MAX_USDC_LOCKED: ${MAX_USDC_LOCKED:.2f}")
            print()
            print("  Cannot guarantee exit. Refusing to trade.")
            print("=" * 60)
            return 2  # NO_TRADE_SAFE
        
        print("=" * 60)
        print("  V14 PRODUCTION VERIFICATION")
        print("=" * 60)
        print(f"  MIN_ORDER_SHARES: {MIN_ORDER_SHARES} (protocol)")
        print(f"  MAX_USDC_LOCKED: ${MAX_USDC_LOCKED:.2f}")
        print(f"  MAX_SHARES: {MAX_SHARES}")
        print(f"  QUOTE_SIZE: {QUOTE_SIZE}")
        print(f"  REGIME: [{ENTRY_MID_MIN}, {ENTRY_MID_MAX}] (epsilon={EPSILON})")
        print(f"  VOL_THRESHOLD: {MAX_VOL_10S_CENTS}c")
        print(f"  MODE: {'LIVE' if self.live else 'DRYRUN'}")
        print("=" * 60)
        
        # Resolve market
        market = self.resolver.resolve_market()
        if not market:
            self.log("No active market")
            return 2
        
        self.yes_token = market.yes_token_id
        self.no_token = market.no_token_id
        self.market_end_time = market.end_time
        
        self.log(f"Market: {market.question}")
        self.log(f"Ends in: {market.time_str}")
        
        # Initialize fill tracker
        self.fill_tracker = FillTrackerV13(
            proxy_address=self.config.api.proxy_address,
            on_kill_switch=lambda reason: self.kill_switch(reason)
        )
        self.fill_tracker.set_boundary()
        self.fill_tracker.set_valid_tokens(self.yes_token, self.no_token)
        
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
            
            if self.state == VerifierState.KILLED:
                return 1
            
            if elapsed > MAX_RUNTIME_SECS:
                self.log("Timeout reached")
                if self.live:
                    self.clob.cancel_all()
                return 2
            
            # Get book
            yes_book = self.clob.get_order_book(self.yes_token)
            if not yes_book or yes_book.best_bid < 0.01:
                time.sleep(TICK_INTERVAL_SECS)
                continue
            
            mid = (yes_book.best_bid + yes_book.best_ask) / 2
            spread = yes_book.best_ask - yes_book.best_bid
            time_to_end = self.market_end_time - int(time.time())
            
            vol = self.update_volatility(mid)
            
            # Poll fills
            try:
                new_fills = self.fill_tracker.poll_fills()
            except FillTrackerError:
                return 1
            
            # Process fills for state transitions
            for fill in new_fills:
                if fill.side.value == "BUY":
                    self.entry_price = fill.price
                    self.entry_token = fill.token_id
                    
                    confirmed = self.fill_tracker.get_confirmed_shares(fill.token_id)
                    
                    if confirmed >= MIN_ORDER_SHARES:
                        # Can exit immediately
                        self.state = VerifierState.WAIT_EXIT
                        self.log(f"STATE: WAIT_ENTRY -> WAIT_EXIT (pos={confirmed:.2f} >= {MIN_ORDER_SHARES})")
                    else:
                        # Need to accumulate
                        self.state = VerifierState.ACCUMULATE
                        self.accumulate_start_time = now
                        self.log(f"STATE: WAIT_ENTRY -> ACCUMULATE (pos={confirmed:.2f} < {MIN_ORDER_SHARES})")
                    
                    # Check exposure cap
                    if confirmed > MAX_SHARES:
                        self.log(f"EXPOSURE CAP BREACH: {confirmed} > {MAX_SHARES}")
                        if self.entry_order_id and self.live:
                            try:
                                self.clob.cancel_order(self.entry_order_id)
                            except:
                                pass
                            self.entry_order_id = None
                
                elif fill.side.value == "SELL":
                    remaining = self.fill_tracker.get_confirmed_shares(self.entry_token)
                    if remaining <= 0.01:
                        self.state = VerifierState.DONE
                        self.log("STATE: WAIT_EXIT -> DONE")
            
            # State machine
            if self.state == VerifierState.WAIT_ENTRY:
                can_trade, reason = self.check_entry_conditions(mid, spread, vol, time_to_end)
                
                if not can_trade:
                    if now - self.last_log_time > LOG_INTERVAL_SECS:
                        self.log(f"NO_TRADE: {reason}")
                        self.last_log_time = now
                    
                    if elapsed > MAX_RUNTIME_SECS * 0.3:
                        if self.fill_tracker.total_buys == 0:
                            self.log("NO_TRADE_SAFE: No suitable conditions found")
                            return 2
                else:
                    if not self.entry_order_id and self.live:
                        entry_size = min(QUOTE_SIZE, MAX_SHARES)
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
                                    self.log(f"ENTRY posted: BUY {entry_size} @ {yes_book.best_bid:.4f}")
                            except Exception as e:
                                self.log(f"Entry error: {e}")
            
            elif self.state == VerifierState.ACCUMULATE:
                # Check if we now have enough
                confirmed = self.fill_tracker.get_confirmed_shares(self.entry_token) if self.entry_token else 0
                
                if confirmed >= MIN_ORDER_SHARES:
                    self.state = VerifierState.WAIT_EXIT
                    self.log(f"STATE: ACCUMULATE -> WAIT_EXIT (pos={confirmed:.2f} >= {MIN_ORDER_SHARES})")
                    # Cancel any pending accumulate order
                    if self.accumulate_order_id and self.live:
                        try:
                            self.clob.cancel_order(self.accumulate_order_id)
                        except:
                            pass
                        self.accumulate_order_id = None
                else:
                    # Try to accumulate more
                    can_accum = self.manage_accumulate(yes_book.best_bid, time_to_end)
                    
                    # Check for timeout/failure
                    if not can_accum and (now - self.accumulate_start_time > ACCUMULATE_TIMEOUT_SECS):
                        self.log(f"DUST_UNEXITABLE: pos={confirmed:.2f} < {MIN_ORDER_SHARES}, cannot top-up")
                        # Cancel all and fail
                        if self.live:
                            self.clob.cancel_all()
                        return 1
            
            elif self.state == VerifierState.WAIT_EXIT:
                self.manage_exit(yes_book.best_bid, yes_book.best_ask, time_to_end)
            
            elif self.state == VerifierState.DONE:
                summary = self.fill_tracker.get_summary()
                
                print("\n" + "=" * 60)
                print("  VERIFICATION COMPLETE")
                print("=" * 60)
                print(f"  Round-trips: {summary['round_trips']}")
                print(f"  Realized PnL: ${summary['realized_pnl']:+.4f}")
                print(f"  Total buys: {summary['total_buys']} (${summary['total_buy_cost']:.2f})")
                print(f"  Total sells: {summary['total_sells']} (${summary['total_sell_revenue']:.2f})")
                
                for i, rt in enumerate(self.fill_tracker.round_trips):
                    print(f"\n  [ROUND-TRIP {i+1}]")
                    print(f"    Entry: {rt['entry_price']:.4f}")
                    print(f"    Exit:  {rt['exit_price']:.4f}")
                    print(f"    Size:  {rt['size']:.2f}")
                    print(f"    PnL:   ${rt['pnl']:+.4f}")
                    print(f"    Entry txHash: {rt['entry_txhash']}...")
                    print(f"    Exit txHash:  {rt['exit_txhash']}...")
                
                print("-" * 60)
                print("  RESULT: PASS - Verifier complete")
                print("=" * 60)
                
                return 0
            
            # Tick log
            if now - self.last_log_time > LOG_INTERVAL_SECS:
                confirmed = self.fill_tracker.get_total_confirmed_shares() if self.fill_tracker else 0
                self.log(
                    f"TICK: mid={mid:.4f} spread={spread*100:.1f}c "
                    f"vol={vol.vol_10s_cents:.1f}c state={self.state.value} "
                    f"pos={confirmed:.1f} time_left={time_to_end}s"
                )
                self.last_log_time = now
            
            time.sleep(TICK_INTERVAL_SECS)
        
        return 1


if __name__ == "__main__":
    verifier = V14Verifier()
    try:
        exit_code = verifier.run()
    except FillTrackerError:
        exit_code = 1
    except Exception as e:
        print(f"[V14] Unexpected error: {e}", flush=True)
        import traceback
        traceback.print_exc()
        exit_code = 1
    
    print(f"\n[EXIT] Code {exit_code}")
    sys.exit(exit_code)
