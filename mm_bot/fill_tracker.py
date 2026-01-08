"""
Fill Tracker - Accurate fill detection and PnL tracking
=========================================================

FIXES (from user feedback):
1. SYNTHETIC ENTRY ON RECONCILE: When reconcile detects inv > internal and no
   entry fill exists, create a synthetic entry fill from position avg/cost basis.
   This eliminates "EXIT (no matching entry)" and makes PnL non-zero.

Also tracks:
- Maker rebates credited
- Taker fees paid
- Exit latency
"""

import time
import requests
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set
from threading import RLock
from datetime import datetime


@dataclass
class Fill:
    """A single fill event"""
    trade_id: str
    order_id: str
    token_id: str
    side: str  # "BUY" or "SELL"
    price: float
    size: float
    timestamp: float
    is_maker: bool = False
    fee: float = 0.0
    rebate: float = 0.0
    source: str = "REST"  # "REST" or "RECONCILE" (synthetic)
    
    @property
    def notional(self) -> float:
        return self.price * self.size
    
    @property
    def cash_delta(self) -> float:
        """Cash change from this fill (negative for buys, positive for sells)"""
        if self.side == "BUY":
            return -(self.notional + self.fee - self.rebate)
        else:
            return self.notional - self.fee + self.rebate


@dataclass
class TradeRoundTrip:
    """A complete entry -> exit round trip"""
    token_id: str
    entry_fill: Fill
    exit_fill: Optional[Fill] = None
    
    @property
    def is_complete(self) -> bool:
        return self.exit_fill is not None
    
    @property
    def realized_pnl(self) -> float:
        if not self.is_complete:
            return 0.0
        return self.entry_fill.cash_delta + self.exit_fill.cash_delta
    
    @property
    def hold_time_seconds(self) -> float:
        if not self.is_complete:
            return time.time() - self.entry_fill.timestamp
        return self.exit_fill.timestamp - self.entry_fill.timestamp


class FillTracker:
    """
    Tracks fills from REST trade history.
    
    This is the source of truth for fill counting and PnL.
    
    NEW: Creates synthetic entry fills when reconcile detects inventory
    that wasn't tracked (e.g., fills during order cancel race).
    """
    
    def __init__(self, config):
        self.config = config
        self._lock = RLock()
        
        # API config
        self._data_api = "https://data-api.polymarket.com"
        self._proxy_address = config.api.proxy_address
        
        # Fill storage
        self._fills: Dict[str, Fill] = {}  # trade_id -> Fill
        self._seen_trade_ids: Set[str] = set()
        
        # Round trip tracking
        self._open_entries: Dict[str, Fill] = {}  # token_id -> entry fill
        self._round_trips: List[TradeRoundTrip] = []
        
        # Metrics
        self.entry_fills = 0
        self.exit_fills = 0
        self.synthetic_entries = 0  # NEW: count of synthetic entries
        self.total_rebates = 0.0
        self.total_fees = 0.0
        self.realized_pnl = 0.0
        
        # Timing
        self._last_poll = 0.0
        self._poll_interval = 5.0  # Poll every 5 seconds
        
        # Session tracking
        self._session_start = time.time()
        self._session_start_cash = 0.0
    
    def set_session_start_cash(self, cash: float):
        """Record starting cash for session PnL"""
        self._session_start_cash = cash
        self._session_start = time.time()
    
    def create_synthetic_entry(self, token_id: str, shares: float, avg_price: float):
        """
        Create a synthetic entry fill when reconcile detects inventory we missed.
        
        This fixes the "EXIT (no matching entry)" issue where fills happened
        during order cancel race and weren't tracked.
        
        Args:
            token_id: The token with untracked inventory
            shares: Number of shares from REST position
            avg_price: Average price from REST position (cost basis)
        """
        with self._lock:
            # Only create if we don't already have an open entry
            if token_id in self._open_entries:
                # Update existing entry's shares if needed
                existing = self._open_entries[token_id]
                if existing.size < shares:
                    diff = shares - existing.size
                    existing.size = shares
                    print(f"[FILL] SYNTHETIC: Updated entry {token_id[:20]}... +{diff:.2f} shares", flush=True)
                return
            
            # Create synthetic entry
            synthetic_id = f"synthetic_{token_id}_{int(time.time()*1000)}"
            
            fill = Fill(
                trade_id=synthetic_id,
                order_id="",
                token_id=token_id,
                side="BUY",
                price=avg_price,
                size=shares,
                timestamp=time.time() - 5,  # Assume happened ~5s ago
                is_maker=True,  # Assume maker since we're a maker bot
                fee=0.0,
                rebate=0.0,
                source="RECONCILE"  # Mark as synthetic
            )
            
            # Track
            self._seen_trade_ids.add(synthetic_id)
            self._fills[synthetic_id] = fill
            self._open_entries[token_id] = fill
            
            self.entry_fills += 1
            self.synthetic_entries += 1
            
            print(f"[FILL] SYNTHETIC ENTRY: {token_id[:20]}... "
                  f"{shares:.2f} shares @ {avg_price:.4f} (from RECONCILE)", flush=True)
    
    def poll_fills(self, market_tokens: Set[str]) -> List[Fill]:
        """
        Poll REST API for new fills.
        Returns list of new fills since last poll.
        """
        now = time.time()
        if now - self._last_poll < self._poll_interval:
            return []
        
        self._last_poll = now
        new_fills = []
        
        try:
            # Fetch recent trades
            r = requests.get(
                f"{self._data_api}/trades",
                params={
                    "user": self._proxy_address,
                    "limit": 50
                },
                timeout=10
            )
            
            if r.status_code != 200:
                return []
            
            trades = r.json()
            
            with self._lock:
                for t in trades:
                    trade_id = t.get("id", "")
                    if trade_id in self._seen_trade_ids:
                        continue
                    
                    # Only track fills for our market
                    token_id = t.get("asset", "")
                    if token_id not in market_tokens:
                        continue
                    
                    # Parse fill
                    fill = Fill(
                        trade_id=trade_id,
                        order_id=t.get("orderId", ""),
                        token_id=token_id,
                        side=t.get("side", "").upper(),
                        price=float(t.get("price", 0)),
                        size=float(t.get("size", 0)),
                        timestamp=self._parse_timestamp(t.get("timestamp", "")),
                        is_maker=t.get("maker", False),
                        fee=float(t.get("fee", 0)),
                        rebate=float(t.get("rebate", 0)),
                        source="REST"
                    )
                    
                    # Track
                    self._seen_trade_ids.add(trade_id)
                    self._fills[trade_id] = fill
                    new_fills.append(fill)
                    
                    # Update metrics
                    self._process_fill(fill)
        
        except Exception as e:
            print(f"[FILLS] Error polling trades: {e}", flush=True)
        
        return new_fills
    
    def _process_fill(self, fill: Fill):
        """Process a new fill for metrics and round trip tracking"""
        # Update totals
        self.total_fees += fill.fee
        self.total_rebates += fill.rebate
        
        if fill.side == "BUY":
            self.entry_fills += 1
            
            # Track as open entry (may replace synthetic)
            if fill.token_id in self._open_entries:
                existing = self._open_entries[fill.token_id]
                # If existing is synthetic and this is real, prefer real
                if existing.source == "RECONCILE" and fill.source == "REST":
                    # Update synthetic with real data
                    existing.trade_id = fill.trade_id
                    existing.order_id = fill.order_id
                    existing.price = fill.price
                    existing.size = fill.size
                    existing.timestamp = fill.timestamp
                    existing.is_maker = fill.is_maker
                    existing.fee = fill.fee
                    existing.rebate = fill.rebate
                    existing.source = "REST"
                    print(f"[FILL] Updated synthetic entry with real fill: {fill.token_id[:20]}...", flush=True)
                else:
                    # Accumulate
                    existing.size += fill.size
            else:
                self._open_entries[fill.token_id] = fill
            
            print(f"[FILL] ENTRY: {fill.side} {fill.size:.2f} @ {fill.price:.4f} "
                  f"{'MAKER' if fill.is_maker else 'TAKER'} "
                  f"fee=${fill.fee:.4f} rebate=${fill.rebate:.4f}", flush=True)
        
        elif fill.side == "SELL":
            self.exit_fills += 1
            
            # Try to match with open entry
            if fill.token_id in self._open_entries:
                entry = self._open_entries[fill.token_id]
                
                # Partial fill handling
                if fill.size >= entry.size:
                    # Complete exit
                    del self._open_entries[fill.token_id]
                    trip = TradeRoundTrip(
                        token_id=fill.token_id,
                        entry_fill=entry,
                        exit_fill=fill
                    )
                    self._round_trips.append(trip)
                    self.realized_pnl += trip.realized_pnl
                    
                    print(f"[FILL] EXIT: {fill.side} {fill.size:.2f} @ {fill.price:.4f} "
                          f"{'MAKER' if fill.is_maker else 'TAKER'} "
                          f"PnL=${trip.realized_pnl:.4f} hold={trip.hold_time_seconds:.1f}s", flush=True)
                else:
                    # Partial exit - reduce entry size
                    entry.size -= fill.size
                    # Compute partial PnL
                    partial_entry_cost = fill.size * entry.price
                    partial_exit_proceeds = fill.size * fill.price
                    partial_pnl = partial_exit_proceeds - partial_entry_cost - fill.fee + fill.rebate
                    self.realized_pnl += partial_pnl
                    
                    print(f"[FILL] PARTIAL EXIT: {fill.side} {fill.size:.2f} @ {fill.price:.4f} "
                          f"PnL=${partial_pnl:.4f} remaining={entry.size:.2f}", flush=True)
            else:
                print(f"[FILL] EXIT (no matching entry): {fill.side} {fill.size:.2f} @ {fill.price:.4f}", flush=True)
    
    def _parse_timestamp(self, ts_str: str) -> float:
        """Parse timestamp string to unix time"""
        if not ts_str:
            return time.time()
        try:
            # Try ISO format
            dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            return dt.timestamp()
        except:
            return time.time()
    
    def get_metrics(self) -> Dict:
        """Get fill metrics for reporting"""
        with self._lock:
            complete_trips = [t for t in self._round_trips if t.is_complete]
            
            exit_latencies = [t.hold_time_seconds for t in complete_trips]
            
            return {
                "entry_fills": self.entry_fills,
                "exit_fills": self.exit_fills,
                "synthetic_entries": self.synthetic_entries,
                "total_fills": len(self._fills),
                "total_rebates": self.total_rebates,
                "total_fees": self.total_fees,
                "realized_pnl": self.realized_pnl,
                "complete_round_trips": len(complete_trips),
                "open_entries": len(self._open_entries),
                "exit_latency_p50": sorted(exit_latencies)[len(exit_latencies)//2] if exit_latencies else 0,
                "exit_latency_p95": sorted(exit_latencies)[int(len(exit_latencies)*0.95)] if len(exit_latencies) > 1 else 0,
                "session_duration_s": time.time() - self._session_start,
                "session_start_cash": self._session_start_cash
            }
    
    def has_open_entry(self, token_id: str) -> bool:
        """Check if we have an open entry for this token"""
        with self._lock:
            return token_id in self._open_entries
    
    def get_open_entry(self, token_id: str) -> Optional[Fill]:
        """Get open entry for a token"""
        with self._lock:
            return self._open_entries.get(token_id)
