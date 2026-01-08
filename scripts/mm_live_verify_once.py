"""
V15 PRODUCTION VERIFICATION SCRIPT
===================================

CRITICAL FIX: Multi-source fill detection
- Primary: open-orders endpoint (order disappears = probable fill)
- Secondary: trades endpoint (confirm with wide lookback)
- Fail-safe: positions endpoint (sanity check)

EXIT CODES:
0 = PASS - Complete round-trip verified
1 = FAIL - Safety violation or STATE_DESYNC
2 = NO_TRADE_SAFE - Conditions unsuitable
"""

import os
import sys
import time
import requests
from enum import Enum
from typing import Optional, List, Dict, Set
from dataclasses import dataclass

# Force UTF-8 output
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mm_bot.config import Config
from mm_bot.clob import ClobWrapper
from mm_bot.market import MarketResolver


# ============================================================================
# V15 CONFIGURATION
# ============================================================================

MIN_ORDER_SHARES = 5
MAX_USDC_LOCKED = float(os.environ.get("MM_MAX_USDC", "3.00"))
MAX_SHARES = int(float(os.environ.get("MM_MAX_SHARES", "6")))
QUOTE_SIZE = int(float(os.environ.get("MM_QUOTE_SIZE", "6")))

ENTRY_MID_MIN = 0.45
ENTRY_MID_MAX = 0.55
MAX_SPREAD_CENTS = 3
MIN_TIME_TO_END_SECS = 180

MAX_RUNTIME_SECS = int(os.environ.get("MM_MAX_RUNTIME", "600"))
TICK_INTERVAL_SECS = 0.25
LOG_INTERVAL_SECS = 5
EXIT_REPRICE_INTERVAL_SECS = 5

# Fill confirmation timeout
FILL_CONFIRM_TIMEOUT_SECS = 15

# Debug mode
DEBUG_TRADES = os.environ.get("DEBUG_TRADES", "1") == "1"


class VerifierState(Enum):
    WAIT_ENTRY = "WAIT_ENTRY"
    FILL_PENDING = "FILL_PENDING"  # Order disappeared, confirming via trades
    WAIT_EXIT = "WAIT_EXIT"
    DONE = "DONE"
    FAILED = "FAILED"


@dataclass
class ConfirmedFill:
    trade_id: str
    tx_hash: str
    side: str
    price: float
    size: float
    timestamp: float
    token_id: str


class V15Verifier:
    """
    Production verifier with GUARANTEED fill detection.
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
        
        # State
        self.state = VerifierState.WAIT_ENTRY
        self.entry_order_id: Optional[str] = None
        self.entry_order_price: float = 0.0
        self.entry_order_size: int = 0
        self.exit_order_id: Optional[str] = None
        
        # Fill tracking
        self.script_start_ts: float = 0.0
        self.fill_pending_start: float = 0.0
        self.entry_fill: Optional[ConfirmedFill] = None
        self.exit_fill: Optional[ConfirmedFill] = None
        self.confirmed_shares: float = 0.0
        self.entry_price: float = 0.0
        
        # Exit ladder
        self.exit_reprice_count: int = 0
        self.exit_posted_at: float = 0.0
        
        # Seen trades
        self.seen_trade_ids: Set[str] = set()
        
        # Timing
        self.start_time: float = 0.0
        self.last_log_time: float = 0.0
        
        # Mid history for volatility
        self.mid_history: List[float] = []
    
    def log(self, msg: str):
        print(f"[V15] {msg}", flush=True)
    
    def get_vol_10s(self) -> float:
        if len(self.mid_history) < 10:
            return 0.0
        recent = self.mid_history[-40:]
        if len(recent) < 2:
            return 0.0
        return (max(recent) - min(recent)) * 100
    
    # ========================================================================
    # FILL DETECTION - MULTI-SOURCE
    # ========================================================================
    
    def check_order_still_open(self, order_id: str) -> bool:
        """Check if order_id is still in open orders."""
        try:
            open_orders = self.clob.get_open_orders()
            if open_orders:
                for order in open_orders:
                    if order.get("id") == order_id:
                        return True
            return False
        except Exception as e:
            self.log(f"Error checking open orders: {e}")
            return True  # Assume still open on error
    
    def fetch_recent_trades(self, lookback_secs: float = 60.0) -> List[Dict]:
        """
        Fetch trades with wide lookback.
        Query BOTH proxy address.
        """
        trades = []
        cutoff_ts = self.script_start_ts - lookback_secs
        
        try:
            r = requests.get(
                "https://data-api.polymarket.com/trades",
                params={"user": self.config.api.proxy_address, "limit": 50},
                timeout=10
            )
            
            if r.status_code == 200:
                for t in r.json():
                    ts = float(t.get("timestamp", 0) or 0)
                    if ts >= cutoff_ts:
                        trades.append(t)
        except Exception as e:
            self.log(f"Trades API error: {e}")
        
        return trades
    
    def dump_debug_trades(self, trades: List[Dict]):
        """Dump raw trades for debugging."""
        if not DEBUG_TRADES:
            return
        
        self.log("=" * 60)
        self.log("DEBUG: Raw trades dump (last 20)")
        self.log("=" * 60)
        
        for i, t in enumerate(trades[:20]):
            self.log(f"Trade {i+1}:")
            self.log(f"  txHash: {t.get('transactionHash', 'MISSING')[:20]}...")
            self.log(f"  asset: {t.get('asset', 'MISSING')[-12:]}")
            self.log(f"  side: {t.get('side', 'MISSING')}")
            self.log(f"  size: {t.get('size', 'MISSING')}")
            self.log(f"  price: {t.get('price', 'MISSING')}")
            self.log(f"  timestamp: {t.get('timestamp', 'MISSING')}")
            self.log(f"  proxyWallet: {t.get('proxyWallet', 'MISSING')[:16]}...")
        
        self.log("=" * 60)
    
    def find_matching_fill(self, trades: List[Dict], side: str, expected_price: float, expected_size: float) -> Optional[ConfirmedFill]:
        """
        Find a trade that matches our order.
        """
        for t in trades:
            tx_hash = t.get("transactionHash", "")
            if not tx_hash:
                continue
            
            trade_side = t.get("side", "").upper()
            if trade_side != side:
                continue
            
            token_id = t.get("asset", "")
            if token_id not in self.market_tokens:
                continue
            
            trade_price = float(t.get("price", 0) or 0)
            trade_size = float(t.get("size", 0) or 0)
            timestamp = float(t.get("timestamp", 0) or 0)
            
            # Match within reasonable tolerance
            price_match = abs(trade_price - expected_price) <= 0.02  # 2c tolerance
            size_match = trade_size <= expected_size + 0.5  # Size should be <= posted
            
            if price_match and size_match:
                trade_id = f"{tx_hash}_{token_id[-8:]}_{timestamp}"
                
                if trade_id in self.seen_trade_ids:
                    continue
                
                self.seen_trade_ids.add(trade_id)
                
                return ConfirmedFill(
                    trade_id=trade_id,
                    tx_hash=tx_hash,
                    side=trade_side,
                    price=trade_price,
                    size=trade_size,
                    timestamp=timestamp,
                    token_id=token_id
                )
        
        return None
    
    def check_positions_for_inventory(self) -> float:
        """Check positions endpoint as sanity."""
        try:
            positions = self.clob.get_positions()
            if positions:
                for p in positions:
                    token_id = p.get("token_id", "") or p.get("asset", "")
                    if token_id in self.market_tokens:
                        size = float(p.get("size", 0) or p.get("shares", 0) or 0)
                        if size > 0.01:
                            return size
            return 0.0
        except Exception as e:
            self.log(f"Positions API error: {e}")
            return 0.0
    
    # ========================================================================
    # ENTRY CONDITIONS
    # ========================================================================
    
    def check_entry_conditions(self, mid: float, spread: float, time_to_end: int) -> tuple:
        if mid < ENTRY_MID_MIN or mid > ENTRY_MID_MAX:
            return (False, f"mid {mid:.2f} outside [{ENTRY_MID_MIN}, {ENTRY_MID_MAX}]")
        
        spread_cents = spread * 100
        if spread_cents > MAX_SPREAD_CENTS:
            return (False, f"spread {spread_cents:.1f}c > {MAX_SPREAD_CENTS}c")
        
        vol = self.get_vol_10s()
        if vol > 8:
            return (False, f"vol {vol:.1f}c > 8c")
        
        if time_to_end < MIN_TIME_TO_END_SECS:
            return (False, f"time_to_end {time_to_end}s < {MIN_TIME_TO_END_SECS}s")
        
        return (True, "OK")
    
    # ========================================================================
    # EXIT MANAGEMENT
    # ========================================================================
    
    def manage_exit(self, best_bid: float, best_ask: float, time_to_end: int):
        if self.confirmed_shares < MIN_ORDER_SHARES:
            self.log(f"Dust position: {self.confirmed_shares:.2f} < {MIN_ORDER_SHARES}")
            return
        
        now = time.time()
        
        should_reprice = False
        if not self.exit_order_id:
            should_reprice = True
        elif now - self.exit_posted_at > EXIT_REPRICE_INTERVAL_SECS:
            # Check if exit order still open
            if self.exit_order_id and not self.check_order_still_open(self.exit_order_id):
                # Exit order filled!
                self.log("Exit order disappeared - checking for fill...")
                trades = self.fetch_recent_trades(120.0)
                fill = self.find_matching_fill(trades, "SELL", self.entry_price, self.confirmed_shares)
                if fill:
                    self.exit_fill = fill
                    self.confirmed_shares -= fill.size
                    self.log(f"[FILL] EXIT: SELL {fill.size:.2f} @ {fill.price:.4f} txHash={fill.tx_hash[:16]}...")
                    if self.confirmed_shares <= 0.01:
                        self.state = VerifierState.DONE
                    return
            should_reprice = True
        
        if not should_reprice:
            return
        
        # Cancel existing
        if self.exit_order_id and self.live:
            try:
                self.clob.cancel_order(self.exit_order_id)
            except:
                pass
            self.exit_order_id = None
        
        # Get exit price from ladder
        if time_to_end < 60:
            exit_price = best_bid
            is_taker = True
        elif self.exit_reprice_count == 0:
            exit_price = min(0.99, self.entry_price + 0.01)
            is_taker = False
        elif self.exit_reprice_count == 1:
            exit_price = self.entry_price
            is_taker = False
        elif self.exit_reprice_count == 2:
            exit_price = max(0.01, self.entry_price - 0.01)
            is_taker = False
        else:
            exit_price = best_bid
            is_taker = True
        
        exit_size = int(self.confirmed_shares)
        
        if self.live:
            try:
                result = self.clob.post_order(
                    token_id=self.entry_fill.token_id if self.entry_fill else self.yes_token,
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
            except Exception as e:
                self.log(f"Exit error: {e}")
    
    # ========================================================================
    # MAIN LOOP
    # ========================================================================
    
    def run(self) -> int:
        # Config validation
        if MAX_SHARES < MIN_ORDER_SHARES or QUOTE_SIZE < MIN_ORDER_SHARES:
            self.log(f"INVALID_CONFIG: MAX_SHARES({MAX_SHARES}) or QUOTE_SIZE({QUOTE_SIZE}) < MIN_ORDER_SHARES({MIN_ORDER_SHARES})")
            return 2
        
        print("=" * 60)
        print("  V15 PRODUCTION VERIFICATION")
        print("  Multi-source fill detection")
        print("=" * 60)
        print(f"  MAX_USDC: ${MAX_USDC_LOCKED:.2f}")
        print(f"  MAX_SHARES: {MAX_SHARES}")
        print(f"  QUOTE_SIZE: {QUOTE_SIZE}")
        print(f"  DEBUG_TRADES: {DEBUG_TRADES}")
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
        self.log(f"YES token: {self.yes_token[-12:]}")
        self.log(f"NO token: {self.no_token[-12:]}")
        self.log(f"Proxy: {self.config.api.proxy_address[:16]}...")
        
        # Cleanup
        if self.live:
            self.clob.cancel_all()
            self.log("Cancelled all orders")
        
        # Set timing
        self.script_start_ts = time.time()
        self.start_time = time.time()
        self.last_log_time = time.time()
        
        # Get starting balance
        bal = self.clob.get_balance()
        self.log(f"Start balance: ${bal.get('usdc', 0):.2f}")
        
        while True:
            now = time.time()
            elapsed = now - self.start_time
            
            if self.state == VerifierState.FAILED:
                return 1
            
            if elapsed > MAX_RUNTIME_SECS:
                self.log("Timeout")
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
            
            self.mid_history.append(mid)
            if len(self.mid_history) > 100:
                self.mid_history = self.mid_history[-100:]
            
            # ================================================================
            # STATE: WAIT_ENTRY
            # ================================================================
            if self.state == VerifierState.WAIT_ENTRY:
                # Check if we have a pending entry order
                if self.entry_order_id:
                    # Check if order disappeared (probable fill!)
                    if not self.check_order_still_open(self.entry_order_id):
                        self.log(f"Entry order {self.entry_order_id[:16]}... DISAPPEARED")
                        self.log("Probable fill - entering FILL_PENDING")
                        self.state = VerifierState.FILL_PENDING
                        self.fill_pending_start = now
                        
                        # Dump trades for debug
                        trades = self.fetch_recent_trades(120.0)
                        self.dump_debug_trades(trades)
                        continue
                
                # Check conditions
                can_trade, reason = self.check_entry_conditions(mid, spread, time_to_end)
                
                if not can_trade:
                    if now - self.last_log_time > LOG_INTERVAL_SECS:
                        self.log(f"NO_TRADE: {reason}")
                        self.last_log_time = now
                    
                    if elapsed > 120:
                        self.log("NO_TRADE_SAFE: Conditions never suitable")
                        return 2
                else:
                    # Post entry if not already
                    if not self.entry_order_id and self.live:
                        entry_size = min(QUOTE_SIZE, MAX_SHARES)
                        entry_price = yes_book.best_bid
                        
                        try:
                            result = self.clob.post_order(
                                token_id=self.yes_token,
                                side="BUY",
                                price=entry_price,
                                size=entry_size,
                                post_only=True
                            )
                            if result.success:
                                self.entry_order_id = result.order_id
                                self.entry_order_price = entry_price
                                self.entry_order_size = entry_size
                                self.log(f"ENTRY posted: BUY {entry_size} @ {entry_price:.4f} order_id={result.order_id[:16]}...")
                        except Exception as e:
                            self.log(f"Entry error: {e}")
            
            # ================================================================
            # STATE: FILL_PENDING - Must confirm via trades
            # ================================================================
            elif self.state == VerifierState.FILL_PENDING:
                trades = self.fetch_recent_trades(120.0)  # Wide lookback
                
                fill = self.find_matching_fill(
                    trades, "BUY",
                    self.entry_order_price,
                    self.entry_order_size
                )
                
                if fill:
                    self.entry_fill = fill
                    self.confirmed_shares = fill.size
                    self.entry_price = fill.price
                    
                    self.log(f"[FILL] ENTRY CONFIRMED: BUY {fill.size:.2f} @ {fill.price:.4f}")
                    self.log(f"  txHash: {fill.tx_hash[:40]}...")
                    self.log(f"  token: {fill.token_id[-12:]}")
                    
                    if self.confirmed_shares >= MIN_ORDER_SHARES:
                        self.state = VerifierState.WAIT_EXIT
                        self.log("STATE: FILL_PENDING -> WAIT_EXIT")
                    else:
                        self.log(f"Partial fill: {self.confirmed_shares:.2f} < {MIN_ORDER_SHARES}")
                        # For now, still go to WAIT_EXIT and handle dust there
                        self.state = VerifierState.WAIT_EXIT
                else:
                    # Check positions as fallback
                    pos_shares = self.check_positions_for_inventory()
                    if pos_shares > 0.01:
                        self.log(f"Positions API shows inventory: {pos_shares:.2f}")
                        self.confirmed_shares = pos_shares
                        self.entry_price = self.entry_order_price
                        self.state = VerifierState.WAIT_EXIT
                        self.log("STATE: FILL_PENDING -> WAIT_EXIT (from positions)")
                    else:
                        # Still waiting
                        if now - self.fill_pending_start > FILL_CONFIRM_TIMEOUT_SECS:
                            self.log(f"FILL_CONFIRM_TIMEOUT: Could not confirm fill after {FILL_CONFIRM_TIMEOUT_SECS}s")
                            self.log("STATE_DESYNC: Order disappeared but no fill found")
                            if self.live:
                                self.clob.cancel_all()
                            self.state = VerifierState.FAILED
                            return 1
            
            # ================================================================
            # STATE: WAIT_EXIT
            # ================================================================
            elif self.state == VerifierState.WAIT_EXIT:
                self.manage_exit(yes_book.best_bid, yes_book.best_ask, time_to_end)
            
            # ================================================================
            # STATE: DONE
            # ================================================================
            elif self.state == VerifierState.DONE:
                pnl = 0.0
                if self.entry_fill and self.exit_fill:
                    pnl = (self.exit_fill.price - self.entry_fill.price) * self.exit_fill.size
                
                print("\n" + "=" * 60)
                print("  VERIFICATION COMPLETE")
                print("=" * 60)
                if self.entry_fill:
                    print(f"  Entry: BUY {self.entry_fill.size:.2f} @ {self.entry_fill.price:.4f}")
                    print(f"  Entry txHash: {self.entry_fill.tx_hash[:40]}...")
                if self.exit_fill:
                    print(f"  Exit: SELL {self.exit_fill.size:.2f} @ {self.exit_fill.price:.4f}")
                    print(f"  Exit txHash: {self.exit_fill.tx_hash[:40]}...")
                print(f"  [ROUND-TRIP] PnL = ${pnl:+.4f}")
                print("-" * 60)
                print("  RESULT: PASS")
                print("=" * 60)
                return 0
            
            # Tick log
            if now - self.last_log_time > LOG_INTERVAL_SECS:
                vol = self.get_vol_10s()
                order_status = "pending" if self.entry_order_id else "none"
                self.log(
                    f"TICK: mid={mid:.4f} spread={spread*100:.1f}c vol={vol:.1f}c "
                    f"state={self.state.value} pos={self.confirmed_shares:.1f} "
                    f"order={order_status} time_left={time_to_end}s"
                )
                self.last_log_time = now
            
            time.sleep(TICK_INTERVAL_SECS)
        
        return 1


if __name__ == "__main__":
    verifier = V15Verifier()
    try:
        exit_code = verifier.run()
    except Exception as e:
        print(f"[V15] Error: {e}", flush=True)
        import traceback
        traceback.print_exc()
        exit_code = 1
    
    print(f"\n[EXIT] Code {exit_code}")
    sys.exit(exit_code)
