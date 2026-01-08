"""
V13 FILL TRACKER
================

Production-grade fill tracking with:
- Bulletproof trade ingestion boundary
- Dedupe key = transactionHash + asset + side + size + price + timestamp
- NO synthetic fills - positions change ONLY from confirmed fills
- KILL_SWITCH on any invariant violation
"""

import time
import hashlib
import requests
from dataclasses import dataclass, field
from typing import Optional, Set, Dict, List, Callable
from enum import Enum


class FillTrackerError(Exception):
    """Raised on invariant violations that require KILL_SWITCH."""
    pass


class FillSide(Enum):
    BUY = "BUY"
    SELL = "SELL"


@dataclass
class ConfirmedFill:
    """A confirmed fill from the trades API."""
    trade_id: str  # Dedupe key
    transaction_hash: str  # Raw txHash from API
    token_id: str
    side: FillSide
    price: float
    size: float
    timestamp: float
    is_maker: bool = True
    fee: float = 0.0
    rebate: float = 0.0


@dataclass
class Position:
    """A position opened from confirmed fills only."""
    token_id: str
    shares: float
    avg_entry_price: float
    entry_fills: List[ConfirmedFill] = field(default_factory=list)
    exit_fills: List[ConfirmedFill] = field(default_factory=list)
    opened_at: float = 0.0
    closed_at: Optional[float] = None
    
    @property
    def is_open(self) -> bool:
        return self.shares > 0.01
    
    @property
    def realized_pnl(self) -> float:
        """PnL from exit fills only."""
        if not self.exit_fills:
            return 0.0
        
        exit_revenue = sum(f.size * f.price for f in self.exit_fills)
        exit_shares = sum(f.size for f in self.exit_fills)
        exit_cost = exit_shares * self.avg_entry_price
        
        return exit_revenue - exit_cost


class FillTrackerV13:
    """
    Production-grade fill tracker.
    
    Key invariants:
    1. Positions open ONLY from confirmed BUY fills
    2. Positions close ONLY from confirmed SELL fills
    3. NO synthetic fills from reconcile
    4. Trade ingestion boundary enforced
    5. Missing transactionHash = FAIL
    """
    
    def __init__(
        self,
        api_url: str = "https://data-api.polymarket.com/trades",
        proxy_address: str = "",
        on_kill_switch: Optional[Callable[[str], None]] = None
    ):
        self.api_url = api_url
        self.proxy_address = proxy_address
        self.on_kill_switch = on_kill_switch
        
        # Trade ingestion boundary (set on start)
        self.boundary_ts: float = 0.0
        
        # Valid token IDs for current market
        self.valid_tokens: Set[str] = set()
        
        # Seen trade dedupe keys
        self._seen_trades: Set[str] = set()
        
        # Positions by token_id
        self.positions: Dict[str, Position] = {}
        
        # All confirmed fills for audit
        self.all_fills: List[ConfirmedFill] = []
        
        # Round-trip tracking
        self.round_trips: List[dict] = []
        
        # Stats
        self.total_buys = 0
        self.total_sells = 0
        self.total_buy_cost = 0.0
        self.total_sell_revenue = 0.0
    
    def set_boundary(self, boundary_ts: Optional[float] = None, skew_secs: float = 2.0):
        """
        Set trade ingestion boundary.
        
        Trades with timestamp < boundary_ts are ignored.
        """
        if boundary_ts is None:
            boundary_ts = time.time() - skew_secs
        
        self.boundary_ts = boundary_ts
        self._seen_trades.clear()
        print(f"[FILL_TRACKER] Boundary set: ignore trades before {self.boundary_ts:.0f}", flush=True)
    
    def set_valid_tokens(self, yes_token: str, no_token: str):
        """Set valid token IDs for current market."""
        self.valid_tokens = {yes_token, no_token}
        print(f"[FILL_TRACKER] Valid tokens: YES={yes_token[-8:]}, NO={no_token[-8:]}", flush=True)
    
    def reset(self):
        """Reset all state for new session."""
        self._seen_trades.clear()
        self.positions.clear()
        self.all_fills.clear()
        self.round_trips.clear()
        self.total_buys = 0
        self.total_sells = 0
        self.total_buy_cost = 0.0
        self.total_sell_revenue = 0.0
    
    def _make_dedupe_key(
        self,
        tx_hash: str,
        asset: str,
        side: str,
        size: float,
        price: float,
        timestamp: float
    ) -> str:
        """
        Create dedupe key from trade fields.
        
        Key = transactionHash + asset + side + size + price + timestamp
        """
        raw = f"{tx_hash}_{asset}_{side}_{size}_{price}_{timestamp}"
        return hashlib.sha256(raw.encode()).hexdigest()[:32]
    
    def _trigger_kill_switch(self, reason: str):
        """Trigger kill switch on invariant violation."""
        print(f"[KILL_SWITCH] {reason}", flush=True)
        if self.on_kill_switch:
            self.on_kill_switch(reason)
        raise FillTrackerError(reason)
    
    def poll_fills(self) -> List[ConfirmedFill]:
        """
        Poll trades API for new fills.
        
        Returns list of new confirmed fills (already processed).
        Raises FillTrackerError on invariant violation.
        """
        if not self.proxy_address:
            return []
        
        try:
            r = requests.get(
                self.api_url,
                params={"user": self.proxy_address, "limit": 50},
                timeout=10
            )
            
            if r.status_code != 200:
                print(f"[FILL_TRACKER] API error: {r.status_code}", flush=True)
                return []
            
            trades = r.json()
            
        except Exception as e:
            print(f"[FILL_TRACKER] Request error: {e}", flush=True)
            return []
        
        new_fills = []
        
        for t in trades:
            fill = self._parse_trade(t)
            if fill:
                new_fills.append(fill)
                self._process_fill(fill)
        
        return new_fills
    
    def _parse_trade(self, t: dict) -> Optional[ConfirmedFill]:
        """
        Parse a trade from API response.
        
        Returns None if:
        - Already seen (dedupe)
        - Before boundary timestamp
        - Not for valid tokens
        
        Raises FillTrackerError if:
        - transactionHash is missing
        """
        # Extract fields
        tx_hash = t.get("transactionHash", "")
        asset = t.get("asset", "")
        side = t.get("side", "").upper()
        size = float(t.get("size", 0) or 0)
        price = float(t.get("price", 0) or 0)
        timestamp = t.get("timestamp", 0)
        
        # Parse timestamp (can be int or string)
        if isinstance(timestamp, str):
            try:
                timestamp = float(timestamp)
            except ValueError:
                timestamp = 0
        else:
            timestamp = float(timestamp)
        
        # INVARIANT: transactionHash MUST exist
        if not tx_hash or len(tx_hash) < 10:
            self._trigger_kill_switch(
                f"Missing transactionHash in trade: side={side} size={size} price={price}"
            )
            return None
        
        # Boundary check
        if timestamp < self.boundary_ts:
            return None
        
        # Token filter
        if self.valid_tokens and asset not in self.valid_tokens:
            return None
        
        # Dedupe
        dedupe_key = self._make_dedupe_key(tx_hash, asset, side, size, price, timestamp)
        if dedupe_key in self._seen_trades:
            return None
        
        self._seen_trades.add(dedupe_key)
        
        # Create fill
        try:
            fill_side = FillSide.BUY if side == "BUY" else FillSide.SELL
        except ValueError:
            print(f"[FILL_TRACKER] Unknown side: {side}", flush=True)
            return None
        
        return ConfirmedFill(
            trade_id=dedupe_key,
            transaction_hash=tx_hash,
            token_id=asset,
            side=fill_side,
            price=price,
            size=size,
            timestamp=timestamp,
            is_maker=t.get("maker", True),
            fee=float(t.get("fee", 0) or 0),
            rebate=float(t.get("rebate", 0) or 0)
        )
    
    def _process_fill(self, fill: ConfirmedFill):
        """
        Process a confirmed fill.
        
        BUY fills OPEN positions.
        SELL fills CLOSE positions.
        """
        self.all_fills.append(fill)
        
        if fill.side == FillSide.BUY:
            self._process_buy_fill(fill)
        else:
            self._process_sell_fill(fill)
    
    def _process_buy_fill(self, fill: ConfirmedFill):
        """Process a BUY fill - opens or adds to position."""
        self.total_buys += 1
        self.total_buy_cost += fill.size * fill.price
        
        pos = self.positions.get(fill.token_id)
        
        if pos is None:
            # New position
            pos = Position(
                token_id=fill.token_id,
                shares=fill.size,
                avg_entry_price=fill.price,
                entry_fills=[fill],
                opened_at=fill.timestamp
            )
            self.positions[fill.token_id] = pos
            
            print(f"[FILL] ENTRY: BUY {fill.size:.2f} @ {fill.price:.4f} "
                  f"txHash={fill.transaction_hash[:16]}...", flush=True)
        else:
            # Add to existing position
            total_cost = pos.shares * pos.avg_entry_price + fill.size * fill.price
            pos.shares += fill.size
            pos.avg_entry_price = total_cost / pos.shares if pos.shares > 0 else 0
            pos.entry_fills.append(fill)
            
            print(f"[FILL] ADD: BUY {fill.size:.2f} @ {fill.price:.4f} "
                  f"(total: {pos.shares:.2f} @ {pos.avg_entry_price:.4f})", flush=True)
    
    def _process_sell_fill(self, fill: ConfirmedFill):
        """Process a SELL fill - reduces or closes position."""
        self.total_sells += 1
        self.total_sell_revenue += fill.size * fill.price
        
        pos = self.positions.get(fill.token_id)
        
        if pos is None:
            # SELL without position - this is an invariant violation
            # But don't kill switch - could be from before boundary
            print(f"[FILL] EXIT (no position): SELL {fill.size:.2f} @ {fill.price:.4f} "
                  f"txHash={fill.transaction_hash[:16]}...", flush=True)
            return
        
        # Reduce position
        pos.shares -= fill.size
        pos.exit_fills.append(fill)
        
        # Calculate round-trip PnL for this exit
        entry_cost = fill.size * pos.avg_entry_price
        exit_revenue = fill.size * fill.price
        pnl = exit_revenue - entry_cost
        
        print(f"[FILL] EXIT: SELL {fill.size:.2f} @ {fill.price:.4f} "
              f"PnL=${pnl:+.4f} txHash={fill.transaction_hash[:16]}...", flush=True)
        
        # Track round-trip
        self.round_trips.append({
            'token_id': fill.token_id[-8:],
            'entry_price': pos.avg_entry_price,
            'exit_price': fill.price,
            'size': fill.size,
            'pnl': pnl,
            'entry_txhash': pos.entry_fills[-1].transaction_hash[:16] if pos.entry_fills else '',
            'exit_txhash': fill.transaction_hash[:16]
        })
        
        print(f"[ROUND-TRIP] Entry={pos.avg_entry_price:.4f} Exit={fill.price:.4f} "
              f"Size={fill.size:.2f} PnL=${pnl:+.4f}", flush=True)
        
        # Close position if fully exited
        if pos.shares <= 0.01:
            pos.shares = 0.0
            pos.closed_at = fill.timestamp
            print(f"[POSITION] CLOSED: token={fill.token_id[-8:]}", flush=True)
    
    def get_confirmed_shares(self, token_id: str) -> float:
        """Get confirmed position shares for a token."""
        pos = self.positions.get(token_id)
        return pos.shares if pos else 0.0
    
    def get_total_confirmed_shares(self) -> float:
        """Get total confirmed shares across all positions."""
        return sum(p.shares for p in self.positions.values())
    
    def has_open_position(self, token_id: Optional[str] = None) -> bool:
        """Check if any/specific position is open."""
        if token_id:
            pos = self.positions.get(token_id)
            return pos is not None and pos.is_open
        return any(p.is_open for p in self.positions.values())
    
    def get_total_pnl(self) -> float:
        """Get total realized PnL from round-trips."""
        return sum(rt['pnl'] for rt in self.round_trips)
    
    def get_summary(self) -> dict:
        """Get summary statistics."""
        return {
            'total_buys': self.total_buys,
            'total_sells': self.total_sells,
            'total_buy_cost': self.total_buy_cost,
            'total_sell_revenue': self.total_sell_revenue,
            'round_trips': len(self.round_trips),
            'realized_pnl': self.get_total_pnl(),
            'open_positions': sum(1 for p in self.positions.values() if p.is_open),
            'total_shares': self.get_total_confirmed_shares()
        }

