"""
Pairing Engine - Detect full-set pairs within market windows

A full-set pair is:
- BUY YES + BUY NO within a short time window
- pair_cost = price_YES + price_NO
- If pair_cost < 1.0, there's locked-in profit

Key insight: You don't need MERGE to profit from full-sets.
You can hold to settlement and REDEEM the winning side for $1.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Tuple
from collections import defaultdict

from .normalize import TradeEvent, ActivityEvent
from .config import DecoderV2Config


@dataclass
class FullSetPair:
    """A detected full-set pair (BUY YES + BUY NO)."""
    market_id: str
    
    # YES leg
    yes_trade: TradeEvent
    yes_price: float
    yes_size: float
    yes_ts: datetime
    
    # NO leg
    no_trade: TradeEvent
    no_price: float
    no_size: float
    no_ts: datetime
    
    # Pair metrics
    pair_size: float = 0.0        # min(yes_size, no_size) = matched
    pair_cost: float = 0.0        # yes_price + no_price
    pair_edge: float = 0.0        # 1.0 - pair_cost (before fees)
    pair_delay_secs: float = 0.0  # Time between legs
    
    # Resolution
    has_redeem: bool = False
    redeem_ts: Optional[datetime] = None
    hold_time_secs: Optional[float] = None
    realized_pnl: Optional[float] = None
    
    # Fees
    yes_fee: float = 0.0
    no_fee: float = 0.0
    total_fees: float = 0.0
    net_edge: float = 0.0         # pair_edge - fees
    
    def compute_metrics(self, config: DecoderV2Config) -> None:
        """Compute derived metrics."""
        self.pair_size = min(self.yes_size, self.no_size)
        self.pair_cost = self.yes_price + self.no_price
        self.pair_edge = 1.0 - self.pair_cost
        self.pair_delay_secs = abs((self.no_ts - self.yes_ts).total_seconds())
        
        # Fee estimation
        yes_notional = self.yes_price * self.pair_size
        no_notional = self.no_price * self.pair_size
        
        if self.yes_trade.liquidity == "TAKER":
            self.yes_fee = yes_notional * config.taker_fee_rate
        elif self.yes_trade.liquidity == "MAKER":
            self.yes_fee = -yes_notional * config.maker_rebate_rate  # Rebate
        
        if self.no_trade.liquidity == "TAKER":
            self.no_fee = no_notional * config.taker_fee_rate
        elif self.no_trade.liquidity == "MAKER":
            self.no_fee = -no_notional * config.maker_rebate_rate
        
        self.total_fees = self.yes_fee + self.no_fee
        self.net_edge = self.pair_edge - (self.total_fees / self.pair_size if self.pair_size > 0 else 0)
    
    @property
    def is_profitable(self) -> bool:
        return self.net_edge > 0


@dataclass
class MarketWindow:
    """All activity within a single market window."""
    market_id: str
    
    # Trades by outcome
    yes_buys: List[TradeEvent] = field(default_factory=list)
    yes_sells: List[TradeEvent] = field(default_factory=list)
    no_buys: List[TradeEvent] = field(default_factory=list)
    no_sells: List[TradeEvent] = field(default_factory=list)
    
    # Activity
    redeems: List[ActivityEvent] = field(default_factory=list)
    merges: List[ActivityEvent] = field(default_factory=list)
    
    # Detected pairs
    fullset_pairs: List[FullSetPair] = field(default_factory=list)
    
    # Aggregates
    total_yes_bought: float = 0.0
    total_no_bought: float = 0.0
    total_yes_sold: float = 0.0
    total_no_sold: float = 0.0
    
    avg_yes_buy_price: float = 0.0
    avg_no_buy_price: float = 0.0
    
    net_yes: float = 0.0
    net_no: float = 0.0
    matched_size: float = 0.0
    
    # Timing
    first_trade_ts: Optional[datetime] = None
    last_trade_ts: Optional[datetime] = None
    
    def add_trade(self, trade: TradeEvent) -> None:
        """Add a trade to the window."""
        if trade.outcome == "YES":
            if trade.side == "BUY":
                self.yes_buys.append(trade)
            else:
                self.yes_sells.append(trade)
        elif trade.outcome == "NO":
            if trade.side == "BUY":
                self.no_buys.append(trade)
            else:
                self.no_sells.append(trade)
        
        if self.first_trade_ts is None or trade.ts < self.first_trade_ts:
            self.first_trade_ts = trade.ts
        if self.last_trade_ts is None or trade.ts > self.last_trade_ts:
            self.last_trade_ts = trade.ts
    
    def add_activity(self, activity: ActivityEvent) -> None:
        """Add an activity event."""
        if activity.kind == "REDEEM":
            self.redeems.append(activity)
        elif activity.kind == "MERGE":
            self.merges.append(activity)
    
    def compute_aggregates(self) -> None:
        """Compute aggregate metrics."""
        # YES
        self.total_yes_bought = sum(t.size for t in self.yes_buys)
        self.total_yes_sold = sum(t.size for t in self.yes_sells)
        if self.total_yes_bought > 0:
            self.avg_yes_buy_price = (
                sum(t.price * t.size for t in self.yes_buys) / self.total_yes_bought
            )
        
        # NO
        self.total_no_bought = sum(t.size for t in self.no_buys)
        self.total_no_sold = sum(t.size for t in self.no_sells)
        if self.total_no_bought > 0:
            self.avg_no_buy_price = (
                sum(t.price * t.size for t in self.no_buys) / self.total_no_bought
            )
        
        # Net positions
        self.net_yes = self.total_yes_bought - self.total_yes_sold
        self.net_no = self.total_no_bought - self.total_no_sold
        
        # Matched (full-set potential)
        self.matched_size = min(max(0, self.net_yes), max(0, self.net_no))


def detect_fullset_pairs(
    window: MarketWindow,
    config: DecoderV2Config,
) -> List[FullSetPair]:
    """
    Detect full-set pairs within a market window.
    
    Algorithm:
    1. Sort all YES buys and NO buys by time
    2. For each YES buy, find closest NO buy within pair_window_secs
    3. Match greedily (each trade can only be in one pair)
    """
    pairs = []
    
    yes_buys = sorted(window.yes_buys, key=lambda t: t.ts)
    no_buys = sorted(window.no_buys, key=lambda t: t.ts)
    
    if not yes_buys or not no_buys:
        return pairs
    
    # Track which trades are already paired
    used_yes = set()
    used_no = set()
    
    max_delay = timedelta(seconds=config.pair_window_secs)
    
    # Greedy matching
    for yes_trade in yes_buys:
        if id(yes_trade) in used_yes:
            continue
        
        best_no = None
        best_delay = None
        
        for no_trade in no_buys:
            if id(no_trade) in used_no:
                continue
            
            delay = abs(no_trade.ts - yes_trade.ts)
            if delay <= max_delay:
                if best_delay is None or delay < best_delay:
                    best_no = no_trade
                    best_delay = delay
        
        if best_no is not None:
            pair = FullSetPair(
                market_id=window.market_id,
                yes_trade=yes_trade,
                yes_price=yes_trade.price,
                yes_size=yes_trade.size,
                yes_ts=yes_trade.ts,
                no_trade=best_no,
                no_price=best_no.price,
                no_size=best_no.size,
                no_ts=best_no.ts,
            )
            pair.compute_metrics(config)
            
            # Check if redeem exists
            if window.redeems:
                pair.has_redeem = True
                pair.redeem_ts = min(r.ts for r in window.redeems)
                pair.hold_time_secs = (pair.redeem_ts - max(yes_trade.ts, best_no.ts)).total_seconds()
                
                # Realized PnL (assuming $1 payout on winning side)
                pair.realized_pnl = pair.pair_size * pair.net_edge
            
            pairs.append(pair)
            used_yes.add(id(yes_trade))
            used_no.add(id(best_no))
    
    return pairs


def build_market_windows(
    trades: List[TradeEvent],
    activity: List[ActivityEvent],
) -> Dict[str, MarketWindow]:
    """Build MarketWindow objects from trades and activity."""
    windows = {}
    
    for trade in trades:
        if not trade.market_id:
            continue
        
        if trade.market_id not in windows:
            windows[trade.market_id] = MarketWindow(market_id=trade.market_id)
        
        windows[trade.market_id].add_trade(trade)
    
    for act in activity:
        if not act.market_id:
            continue
        
        if act.market_id not in windows:
            windows[act.market_id] = MarketWindow(market_id=act.market_id)
        
        windows[act.market_id].add_activity(act)
    
    # Compute aggregates and detect pairs
    for window in windows.values():
        window.compute_aggregates()
    
    return windows


def run_pairing_engine(
    trades: List[TradeEvent],
    activity: List[ActivityEvent],
    config: DecoderV2Config,
) -> Tuple[Dict[str, MarketWindow], List[FullSetPair]]:
    """
    Run the full pairing engine.
    
    Returns:
        - Dict of MarketWindow by market_id
        - List of all detected FullSetPair
    """
    windows = build_market_windows(trades, activity)
    
    all_pairs = []
    
    for window in windows.values():
        pairs = detect_fullset_pairs(window, config)
        window.fullset_pairs = pairs
        all_pairs.extend(pairs)
    
    return windows, all_pairs


def compute_pairing_stats(pairs: List[FullSetPair]) -> Dict:
    """Compute aggregate statistics for all pairs."""
    if not pairs:
        return {
            'total_pairs': 0,
            'total_paired_size': 0,
            'avg_pair_cost': 0,
            'avg_pair_edge': 0,
            'avg_net_edge': 0,
            'profitable_pairs': 0,
            'profitable_pct': 0,
            'total_gross_edge': 0,
            'total_net_edge': 0,
            'avg_delay_secs': 0,
            'pairs_with_redeem': 0,
            'avg_hold_time_secs': None,
        }
    
    profitable = [p for p in pairs if p.is_profitable]
    with_redeem = [p for p in pairs if p.has_redeem]
    hold_times = [p.hold_time_secs for p in pairs if p.hold_time_secs is not None]
    
    return {
        'total_pairs': len(pairs),
        'total_paired_size': sum(p.pair_size for p in pairs),
        'avg_pair_cost': sum(p.pair_cost for p in pairs) / len(pairs),
        'avg_pair_edge': sum(p.pair_edge for p in pairs) / len(pairs),
        'avg_net_edge': sum(p.net_edge for p in pairs) / len(pairs),
        'profitable_pairs': len(profitable),
        'profitable_pct': len(profitable) / len(pairs) * 100,
        'total_gross_edge': sum(p.pair_edge * p.pair_size for p in pairs),
        'total_net_edge': sum(p.net_edge * p.pair_size for p in pairs),
        'avg_delay_secs': sum(p.pair_delay_secs for p in pairs) / len(pairs),
        'pairs_with_redeem': len(with_redeem),
        'avg_hold_time_secs': sum(hold_times) / len(hold_times) if hold_times else None,
    }


