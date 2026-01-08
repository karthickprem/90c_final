"""
Fill Tracker - Production-Grade Fill Detection
===============================================

V11 PRODUCTION FIXES:
1. Stable dedupe key: txHash + asset + side + size + price + timestamp
2. If txHash missing, use hash fallback BUT set UNSAFE flag
3. NO synthetic entries - fills come ONLY from trades API
4. Parse both int and string timestamps
5. Track safety state for LIVE gating

This is the SOURCE OF TRUTH for fill tracking.
Reconcile is sanity-only and CANNOT create positions.
"""

import time
import hashlib
import requests
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple
from threading import RLock
from datetime import datetime


@dataclass
class Fill:
    """A single fill event from trades API"""
    trade_id: str           # Unique dedupe key
    order_id: str           # Order that generated this fill
    token_id: str           # Asset/token
    side: str               # "BUY" or "SELL"
    price: float
    size: float
    timestamp: float        # Unix timestamp
    tx_hash: str = ""       # Transaction hash (if available)
    is_maker: bool = True
    fee: float = 0.0
    rebate: float = 0.0
    source: str = "REST"    # Always "REST" now (no synthetic)
    
    @property
    def notional(self) -> float:
        return self.price * self.size
    
    @property
    def has_valid_tx_hash(self) -> bool:
        return bool(self.tx_hash) and len(self.tx_hash) > 10


@dataclass
class OpenPosition:
    """Tracks an open position from confirmed fills"""
    token_id: str
    shares: float
    avg_entry_price: float
    entry_time: float
    entry_fills: List[Fill] = field(default_factory=list)
    
    @property
    def cost_basis(self) -> float:
        return self.shares * self.avg_entry_price


class FillTracker:
    """
    Production-grade fill tracker.
    
    SAFETY RULES:
    1. Fills come ONLY from trades API, never from reconcile
    2. If any trade has missing txHash, set UNSAFE_MODE
    3. Dedupe using stable composite key
    4. Track all safety violations for reporting
    """
    
    def __init__(self, config):
        self.config = config
        self._lock = RLock()
        
        # API config
        self._data_api = "https://data-api.polymarket.com"
        self._proxy_address = config.api.proxy_address
        
        # Fill storage
        self._fills: Dict[str, Fill] = {}
        self._seen_trade_ids: Set[str] = set()
        
        # Position tracking (from confirmed fills only)
        self._positions: Dict[str, OpenPosition] = {}  # token_id -> position
        
        # Safety state
        self.unsafe_mode = False  # Set True if any txHash missing
        self.safety_violations: List[str] = []
        
        # Metrics
        self.entry_fills = 0
        self.exit_fills = 0
        self.total_rebates = 0.0
        self.total_fees = 0.0
        self.realized_pnl = 0.0
        self.round_trips_completed = 0
        
        # Timing
        self._last_poll = 0.0
        self._poll_interval = 2.0  # Poll every 2 seconds for faster detection
        
        # Session
        self._session_start = time.time()
        self._session_start_cash = 0.0
    
    def set_session_start_cash(self, cash: float):
        """Record starting cash for session PnL"""
        self._session_start_cash = cash
        self._session_start = time.time()
    
    def _create_dedupe_key(self, tx_hash: str, token_id: str, side: str, 
                           size: float, price: float, timestamp) -> Tuple[str, bool]:
        """
        Create stable dedupe key for a trade.
        
        Returns: (trade_id, is_safe)
        is_safe=False if txHash was missing and we had to use fallback
        """
        if tx_hash and len(tx_hash) > 10:
            # Stable key with txHash
            key = f"{tx_hash}_{token_id[-12:]}_{side}_{size}_{price}_{timestamp}"
            return (key, True)
        else:
            # FALLBACK: hash of other fields (less reliable)
            fallback_data = f"{token_id}_{side}_{size}_{price}_{timestamp}"
            fallback_hash = hashlib.sha256(fallback_data.encode()).hexdigest()[:32]
            key = f"FALLBACK_{fallback_hash}"
            return (key, False)
    
    def _parse_timestamp(self, ts) -> float:
        """Parse timestamp (int or string) to unix float"""
        if isinstance(ts, (int, float)):
            return float(ts)
        if isinstance(ts, str):
            if not ts:
                return time.time()
            try:
                # Try ISO format
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                return dt.timestamp()
            except:
                try:
                    return float(ts)
                except:
                    return time.time()
        return time.time()
    
    def poll_fills(self, market_tokens: Set[str]) -> List[Fill]:
        """
        Poll trades API for new fills.
        
        Returns list of NEW fills (not seen before).
        Sets unsafe_mode if any trade missing txHash.
        """
        now = time.time()
        if now - self._last_poll < self._poll_interval:
            return []
        
        self._last_poll = now
        new_fills = []
        
        try:
            r = requests.get(
                f"{self._data_api}/trades",
                params={
                    "user": self._proxy_address,
                    "limit": 50
                },
                timeout=10
            )
            
            if r.status_code != 200:
                self._log_violation(f"Trades API returned {r.status_code}")
                return []
            
            trades = r.json()
            
            with self._lock:
                for t in trades:
                    # Extract fields
                    tx_hash = t.get("transactionHash", "")
                    token_id = t.get("asset", "")
                    side = t.get("side", "").upper()
                    size = float(t.get("size", 0) or 0)
                    price = float(t.get("price", 0) or 0)
                    timestamp = self._parse_timestamp(t.get("timestamp", 0))
                    
                    # Skip if not our market
                    if token_id not in market_tokens:
                        continue
                    
                    # Create dedupe key
                    trade_id, is_safe = self._create_dedupe_key(
                        tx_hash, token_id, side, size, price, timestamp
                    )
                    
                    # Check safety
                    if not is_safe:
                        self.unsafe_mode = True
                        self._log_violation(f"Trade missing txHash: {side} {size} @ {price}")
                    
                    # Dedupe
                    if trade_id in self._seen_trade_ids:
                        continue
                    
                    # Create fill
                    fill = Fill(
                        trade_id=trade_id,
                        order_id=t.get("orderId", "") or tx_hash[:16] if tx_hash else "unknown",
                        token_id=token_id,
                        side=side,
                        price=price,
                        size=size,
                        timestamp=timestamp,
                        tx_hash=tx_hash,
                        is_maker=t.get("maker", True),
                        fee=float(t.get("fee", 0) or 0),
                        rebate=float(t.get("rebate", 0) or 0),
                        source="REST"
                    )
                    
                    # Track
                    self._seen_trade_ids.add(trade_id)
                    self._fills[trade_id] = fill
                    new_fills.append(fill)
                    
                    # Process for position tracking
                    self._process_fill(fill)
        
        except Exception as e:
            self._log_violation(f"Trades API error: {e}")
        
        return new_fills
    
    def _process_fill(self, fill: Fill):
        """Process fill for position and PnL tracking"""
        self.total_fees += fill.fee
        self.total_rebates += fill.rebate
        
        maker_str = "MAKER" if fill.is_maker else "TAKER"
        
        if fill.side == "BUY":
            self.entry_fills += 1
            
            # Update or create position
            if fill.token_id in self._positions:
                pos = self._positions[fill.token_id]
                # Weighted average price
                total_cost = pos.cost_basis + (fill.price * fill.size)
                total_shares = pos.shares + fill.size
                pos.avg_entry_price = total_cost / total_shares
                pos.shares = total_shares
                pos.entry_fills.append(fill)
            else:
                self._positions[fill.token_id] = OpenPosition(
                    token_id=fill.token_id,
                    shares=fill.size,
                    avg_entry_price=fill.price,
                    entry_time=fill.timestamp,
                    entry_fills=[fill]
                )
            
            print(f"[FILL] ENTRY: BUY {fill.size:.2f} @ {fill.price:.4f} ({maker_str}) "
                  f"trade_id={fill.trade_id[:30]}...", flush=True)
        
        elif fill.side == "SELL":
            self.exit_fills += 1
            
            if fill.token_id in self._positions:
                pos = self._positions[fill.token_id]
                entry_price = pos.avg_entry_price
                
                # Calculate PnL
                pnl = (fill.price - entry_price) * fill.size - fill.fee + fill.rebate
                self.realized_pnl += pnl
                pnl_str = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
                
                # Update position
                pos.shares -= fill.size
                
                print(f"[FILL] EXIT: SELL {fill.size:.2f} @ {fill.price:.4f} ({maker_str}) "
                      f"trade_id={fill.trade_id[:30]}...", flush=True)
                print(f"[ROUND-TRIP] Entry={entry_price:.4f} Exit={fill.price:.4f} "
                      f"Size={fill.size:.2f} PnL={pnl_str} (from fills)", flush=True)
                
                if pos.shares <= 0.01:
                    del self._positions[fill.token_id]
                    self.round_trips_completed += 1
            else:
                print(f"[FILL] EXIT (no matching entry): SELL {fill.size:.2f} @ {fill.price:.4f} "
                      f"({maker_str}) trade_id={fill.trade_id[:30]}...", flush=True)
                self._log_violation(f"Exit without matching entry: {fill.size} @ {fill.price}")
    
    def _log_violation(self, msg: str):
        """Log a safety violation"""
        timestamp = time.strftime("%H:%M:%S")
        violation = f"[{timestamp}] {msg}"
        self.safety_violations.append(violation)
        print(f"[SAFETY] {msg}", flush=True)
    
    # === Position Queries ===
    
    def get_confirmed_shares(self, token_id: str) -> float:
        """Get confirmed shares from fills (SOURCE OF TRUTH)"""
        with self._lock:
            pos = self._positions.get(token_id)
            return pos.shares if pos else 0.0
    
    def get_confirmed_position(self, token_id: str) -> Optional[OpenPosition]:
        """Get confirmed position from fills"""
        with self._lock:
            return self._positions.get(token_id)
    
    def has_confirmed_inventory(self) -> bool:
        """Check if we have any confirmed inventory from fills"""
        with self._lock:
            return any(pos.shares > 0.01 for pos in self._positions.values())
    
    def get_total_confirmed_shares(self) -> float:
        """Get total confirmed shares across all tokens"""
        with self._lock:
            return sum(pos.shares for pos in self._positions.values())
    
    # === Safety Checks ===
    
    def is_safe_for_live(self) -> Tuple[bool, List[str]]:
        """
        Check if safe to run in LIVE mode.
        Returns (is_safe, list_of_issues)
        """
        issues = []
        
        if self.unsafe_mode:
            issues.append("Trades with missing txHash detected")
        
        if self.safety_violations:
            issues.append(f"{len(self.safety_violations)} safety violations logged")
        
        return (len(issues) == 0, issues)
    
    def get_metrics(self) -> Dict:
        """Get fill metrics for reporting"""
        with self._lock:
            return {
                "entry_fills": self.entry_fills,
                "exit_fills": self.exit_fills,
                "total_fills": len(self._fills),
                "total_rebates": self.total_rebates,
                "total_fees": self.total_fees,
                "realized_pnl": self.realized_pnl,
                "round_trips_completed": self.round_trips_completed,
                "open_positions": len(self._positions),
                "total_confirmed_shares": self.get_total_confirmed_shares(),
                "unsafe_mode": self.unsafe_mode,
                "safety_violations": len(self.safety_violations),
                "session_duration_s": time.time() - self._session_start,
                "session_start_cash": self._session_start_cash
            }
