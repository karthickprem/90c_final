"""
V13 PRODUCTION VERIFICATION SCRIPT
===================================

Capital protection is #1. This script verifies a single round-trip
with strict safety rules.

EXIT CODES:
0 = PASS - Complete round-trip verified (BUY + SELL fills with valid trade_ids)
1 = FAIL - Invariant violation (KILL_SWITCH triggered)
2 = NO_TRADE_SAFE - Conditions unsuitable, correctly refused to trade

INVARIANTS ENFORCED:
- Positions open ONLY from confirmed BUY fills
- Positions close ONLY from confirmed SELL fills
- NO synthetic fills from reconcile
- Trade ingestion boundary: ignore trades before start
- Missing transactionHash = FAIL (exit 1)
- Exposure caps enforced from confirmed fills
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
# V13 CONFIGURATION
# ============================================================================

# Caps (strict for verification)
MAX_USDC_LOCKED = float(os.environ.get("MM_MAX_USDC", "1.50"))
MAX_SHARES = int(float(os.environ.get("MM_MAX_SHARES", "3")))
QUOTE_SIZE = int(float(os.environ.get("MM_QUOTE_SIZE", "3")))  # Clamped to MAX_SHARES

# Regime (STRICT for verification)
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
FLATTEN_DEADLINE_SECS = 60  # Emergency taker exit in last 60s


class VerifierState(Enum):
    WAIT_ENTRY = "WAIT_ENTRY"
    WAIT_EXIT = "WAIT_EXIT"
    DONE = "DONE"
    KILLED = "KILLED"


class V13Verifier:
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
        self.market_end_time: int = 0
        
        # State machine
        self.state = VerifierState.WAIT_ENTRY
        self.entry_order_id: Optional[str] = None
        self.exit_order_id: Optional[str] = None
        
        # Fill tracker (V13 - no synthetic fills)
        self.fill_tracker: Optional[FillTrackerV13] = None
        
        # Volatility tracker (V13 - time-based)
        self.vol_tracker = VolatilityTracker(window_secs=10.0)
        
        # Entry tracking
        self.entry_price: float = 0.0
        self.entry_token: str = ""
        
        # Exit ladder
        self.exit_reprice_count: int = 0
        self.exit_posted_at: float = 0.0
        
        # Timing
        self.start_time: float = 0.0
        self.last_log_time: float = 0.0
        
        # Kill switch state
        self.kill_reason: str = ""
    
    def log(self, msg: str):
        print(f"[V13] {msg}", flush=True)
    
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
    
    # ========================================================================
    # VOLATILITY (V13 - time-based)
    # ========================================================================
    
    def update_volatility(self, mid: float) -> VolatilitySnapshot:
        """Update volatility tracker and return snapshot."""
        return self.vol_tracker.update(mid)
    
    # ========================================================================
    # REGIME CHECKS (V13 - strict for verification)
    # ========================================================================
    
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
        # Regime check - STRICT [0.45, 0.55]
        if mid < ENTRY_MID_MIN or mid > ENTRY_MID_MAX:
            return (False, f"mid {mid:.2f} outside [{ENTRY_MID_MIN}, {ENTRY_MID_MAX}]")
        
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
    
    # ========================================================================
    # EXIT MANAGEMENT (V13 - deterministic ladder)
    # ========================================================================
    
    def get_exit_price(self, best_bid: float, best_ask: float, time_to_end: int) -> tuple:
        """
        Get exit price based on ladder.
        Returns (price, is_taker)
        
        Ladder: TP(entry+1c) -> entry -> entry-1c -> entry-2c -> emergency(bid)
        Emergency taker only if time_to_end < FLATTEN_DEADLINE_SECS
        """
        # Emergency taker
        if time_to_end < FLATTEN_DEADLINE_SECS:
            return (best_bid, True)
        
        # Ladder based on reprice count
        if self.exit_reprice_count == 0:
            # TP: entry+1c or best_ask-1c (whichever is better)
            tp_price = min(0.99, self.entry_price + 0.01)
            ask_minus = max(0.01, best_ask - 0.01)
            return (min(tp_price, ask_minus), False)
        elif self.exit_reprice_count == 1:
            return (self.entry_price, False)  # Scratch
        elif self.exit_reprice_count == 2:
            return (max(0.01, self.entry_price - 0.01), False)
        elif self.exit_reprice_count == 3:
            return (max(0.01, self.entry_price - 0.02), False)
        else:
            return (best_bid, True)  # Emergency
    
    def manage_exit(self, best_bid: float, best_ask: float, time_to_end: int) -> bool:
        """
        Manage exit order with ladder repricing.
        Returns True if exit was posted/repriced.
        """
        if not self.fill_tracker:
            return False
        
        confirmed_shares = self.fill_tracker.get_confirmed_shares(self.entry_token)
        if confirmed_shares < 0.01:
            return False
        
        now = time.time()
        
        # Check if need to reprice
        should_reprice = False
        if not self.exit_order_id:
            should_reprice = True
        elif now - self.exit_posted_at > EXIT_REPRICE_INTERVAL_SECS:
            should_reprice = True
        
        if not should_reprice:
            return False
        
        # Cancel existing exit
        if self.exit_order_id and self.live:
            try:
                self.clob.cancel_order(self.exit_order_id)
            except:
                pass
            self.exit_order_id = None
        
        # Get exit price from ladder
        exit_price, is_taker = self.get_exit_price(best_bid, best_ask, time_to_end)
        
        # Clamp size to confirmed shares
        exit_size = int(confirmed_shares)
        if exit_size < 5:
            self.log(f"Dust position: {confirmed_shares:.2f} < 5, cannot exit via API")
            return False
        
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
                    self.log(f"EXIT posted: SELL {exit_size} @ {exit_price:.4f}{taker_str} (reprice #{self.exit_reprice_count})")
                    return True
                else:
                    error = str(result.error).lower() if result.error else ""
                    if "balance" in error:
                        # Balance error on SELL is a KILL_SWITCH condition
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
    
    # ========================================================================
    # MAIN RUN LOOP
    # ========================================================================
    
    def run(self) -> int:
        """
        Run verification.
        Returns exit code.
        """
        print("=" * 60)
        print("  V13 PRODUCTION VERIFICATION")
        print("=" * 60)
        print(f"  MAX_USDC_LOCKED: ${MAX_USDC_LOCKED:.2f}")
        print(f"  MAX_SHARES: {MAX_SHARES}")
        print(f"  QUOTE_SIZE: {QUOTE_SIZE}")
        print(f"  REGIME: [{ENTRY_MID_MIN}, {ENTRY_MID_MAX}]")
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
        
        # Initialize fill tracker with kill switch callback
        self.fill_tracker = FillTrackerV13(
            proxy_address=self.config.api.proxy_address,
            on_kill_switch=lambda reason: self.kill_switch(reason)
        )
        
        # Set trade ingestion boundary
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
            
            # Check if killed
            if self.state == VerifierState.KILLED:
                return 1
            
            # Timeout
            if elapsed > MAX_RUNTIME_SECS:
                self.log("Timeout reached")
                if self.live:
                    self.clob.cancel_all()
                return 2  # NO_TRADE_SAFE (timeout without trade)
            
            # Get book
            yes_book = self.clob.get_order_book(self.yes_token)
            if not yes_book or yes_book.best_bid < 0.01:
                time.sleep(TICK_INTERVAL_SECS)
                continue
            
            mid = (yes_book.best_bid + yes_book.best_ask) / 2
            spread = yes_book.best_ask - yes_book.best_bid
            time_to_end = self.market_end_time - int(time.time())
            
            # Update volatility (V13 - time-based)
            vol = self.update_volatility(mid)
            
            # Poll fills (may raise FillTrackerError -> KILL_SWITCH)
            try:
                new_fills = self.fill_tracker.poll_fills()
            except FillTrackerError as e:
                return 1  # Kill switch already triggered
            
            # Process fills for state transitions
            for fill in new_fills:
                if fill.side.value == "BUY":
                    # Entry fill confirmed
                    self.entry_price = fill.price
                    self.entry_token = fill.token_id
                    self.state = VerifierState.WAIT_EXIT
                    
                    # Check exposure cap
                    confirmed = self.fill_tracker.get_confirmed_shares(fill.token_id)
                    if confirmed > MAX_SHARES:
                        self.log(f"EXPOSURE CAP BREACH: {confirmed} > {MAX_SHARES}")
                        # Cancel entries, go exit only
                        if self.entry_order_id and self.live:
                            try:
                                self.clob.cancel_order(self.entry_order_id)
                            except:
                                pass
                            self.entry_order_id = None
                    
                elif fill.side.value == "SELL":
                    # Exit fill confirmed
                    remaining = self.fill_tracker.get_confirmed_shares(self.entry_token)
                    if remaining <= 0.01:
                        self.state = VerifierState.DONE
            
            # State machine
            if self.state == VerifierState.WAIT_ENTRY:
                # Check conditions
                can_trade, reason = self.check_entry_conditions(mid, spread, vol, time_to_end)
                
                if not can_trade:
                    if now - self.last_log_time > LOG_INTERVAL_SECS:
                        self.log(f"NO_TRADE: {reason}")
                        self.last_log_time = now
                    
                    # If we've waited without trade opportunity, exit safely
                    if elapsed > MAX_RUNTIME_SECS * 0.3:  # 30% of max runtime
                        if self.fill_tracker.total_buys == 0:
                            self.log("NO_TRADE_SAFE: No suitable conditions found")
                            return 2
                else:
                    # Post entry (if not already pending)
                    if not self.entry_order_id and self.live:
                        # V13: Clamp to min(QUOTE_SIZE, MAX_SHARES)
                        entry_size = min(QUOTE_SIZE, MAX_SHARES)
                        
                        # Check exposure before posting
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
            
            elif self.state == VerifierState.WAIT_EXIT:
                # Manage exit with ladder
                self.manage_exit(yes_book.best_bid, yes_book.best_ask, time_to_end)
            
            elif self.state == VerifierState.DONE:
                # Success!
                summary = self.fill_tracker.get_summary()
                
                print("\n" + "=" * 60)
                print("  VERIFICATION COMPLETE")
                print("=" * 60)
                print(f"  Round-trips: {summary['round_trips']}")
                print(f"  Realized PnL: ${summary['realized_pnl']:+.4f}")
                print(f"  Total buys: {summary['total_buys']} (${summary['total_buy_cost']:.2f})")
                print(f"  Total sells: {summary['total_sells']} (${summary['total_sell_revenue']:.2f})")
                
                # Print round-trip details
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
            
            # Tick log every LOG_INTERVAL_SECS
            if now - self.last_log_time > LOG_INTERVAL_SECS:
                confirmed = self.fill_tracker.get_total_confirmed_shares() if self.fill_tracker else 0
                self.log(
                    f"TICK: mid={mid:.4f} spread={spread*100:.1f}c "
                    f"vol={vol.vol_10s_cents:.1f}c (min={vol.mid_min:.4f} max={vol.mid_max:.4f}) "
                    f"state={self.state.value} pos={confirmed:.1f} "
                    f"time_left={time_to_end}s"
                )
                self.last_log_time = now
            
            time.sleep(TICK_INTERVAL_SECS)
        
        return 1


if __name__ == "__main__":
    verifier = V13Verifier()
    try:
        exit_code = verifier.run()
    except FillTrackerError:
        exit_code = 1
    except Exception as e:
        print(f"[V13] Unexpected error: {e}", flush=True)
        exit_code = 1
    
    print(f"\n[EXIT] Code {exit_code}")
    sys.exit(exit_code)
