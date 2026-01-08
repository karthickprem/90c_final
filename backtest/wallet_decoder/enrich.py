"""
Enrichment - Build episodes from events and compute aggregate metrics
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import List, Dict, Optional
from collections import defaultdict

from .normalize import Event
from .config import EPISODE_GAP_MINUTES


@dataclass
class Episode:
    """
    An episode is a coherent sequence of trading activity in one market.
    
    Starts: First event after gap or market change
    Ends: MERGE/REDEEM or gap after last event
    """
    market_id: str
    window_id: Optional[str] = None
    
    start_ts: datetime = None
    end_ts: datetime = None
    
    # Events
    trades: List[Event] = field(default_factory=list)
    actions: List[Event] = field(default_factory=list)  # MERGE/SPLIT/REDEEM/etc
    
    # Aggregates (for binary markets)
    net_up: float = 0.0        # Net shares UP (bought - sold)
    net_down: float = 0.0      # Net shares DOWN
    
    total_up_bought: float = 0.0
    total_up_sold: float = 0.0
    total_down_bought: float = 0.0
    total_down_sold: float = 0.0
    
    cost_up: float = 0.0       # Total spent on UP
    cost_down: float = 0.0     # Total spent on DOWN
    
    # Full-set metrics
    matched_shares: float = 0.0     # min(net_up, net_down)
    avg_cost_matched: float = 0.0   # Weighted avg (p_up + p_down) for matched
    
    # Activity flags
    has_merge: bool = False
    has_split: bool = False
    has_redeem: bool = False
    merge_delay_s: Optional[float] = None  # Seconds from last matched build to merge
    
    # Derived
    total_trades: int = 0
    total_cash_spent: float = 0.0
    total_cash_received: float = 0.0
    
    def compute_aggregates(self) -> None:
        """Compute aggregate metrics from events."""
        self.total_trades = len(self.trades)
        
        if self.trades:
            self.start_ts = min(t.ts for t in self.trades)
            self.end_ts = max(t.ts for t in self.trades + self.actions) if self.actions else max(t.ts for t in self.trades)
        
        # Compute net positions
        for t in self.trades:
            if t.side == "BUY":
                if t.outcome in ("UP", "YES", "A"):
                    self.total_up_bought += t.size or 0
                    self.cost_up += (t.price or 0) * (t.size or 0)
                elif t.outcome in ("DOWN", "NO", "B"):
                    self.total_down_bought += t.size or 0
                    self.cost_down += (t.price or 0) * (t.size or 0)
            elif t.side == "SELL":
                if t.outcome in ("UP", "YES", "A"):
                    self.total_up_sold += t.size or 0
                elif t.outcome in ("DOWN", "NO", "B"):
                    self.total_down_sold += t.size or 0
            
            if t.cash_delta:
                if t.cash_delta < 0:
                    self.total_cash_spent += abs(t.cash_delta)
                else:
                    self.total_cash_received += t.cash_delta
        
        self.net_up = self.total_up_bought - self.total_up_sold
        self.net_down = self.total_down_bought - self.total_down_sold
        
        # Matched shares (for full-set arb)
        self.matched_shares = min(max(0, self.net_up), max(0, self.net_down))
        
        # Compute average cost for matched shares
        if self.matched_shares > 0:
            # Weighted average of (p_up + p_down) per matched share
            avg_up = self.cost_up / self.total_up_bought if self.total_up_bought > 0 else 0
            avg_down = self.cost_down / self.total_down_bought if self.total_down_bought > 0 else 0
            self.avg_cost_matched = avg_up + avg_down
        
        # Check for merge/split/redeem
        last_trade_ts = max((t.ts for t in self.trades), default=None)
        
        for a in self.actions:
            if a.kind == "MERGE":
                self.has_merge = True
                if last_trade_ts:
                    self.merge_delay_s = (a.ts - last_trade_ts).total_seconds()
            elif a.kind == "SPLIT":
                self.has_split = True
            elif a.kind == "REDEEM":
                self.has_redeem = True


def build_episodes(events: List[Event], gap_minutes: int = EPISODE_GAP_MINUTES) -> List[Episode]:
    """
    Build episodes from events.
    
    Algorithm:
    1. Group events by market_id
    2. Within each market, split into episodes by gap
    3. Compute aggregates for each episode
    """
    gap = timedelta(minutes=gap_minutes)
    
    # Group by market_id
    by_market: Dict[str, List[Event]] = defaultdict(list)
    
    for e in events:
        market_key = e.market_id or "unknown"
        by_market[market_key].append(e)
    
    episodes = []
    
    for market_id, market_events in by_market.items():
        if not market_events:
            continue
        
        # Sort by time
        market_events.sort(key=lambda e: e.ts)
        
        # Split into episodes
        current_episode = Episode(market_id=market_id)
        last_ts = None
        
        for e in market_events:
            # Check for gap
            if last_ts and (e.ts - last_ts) > gap:
                # Finalize current episode
                if current_episode.trades or current_episode.actions:
                    current_episode.compute_aggregates()
                    episodes.append(current_episode)
                
                # Start new episode
                current_episode = Episode(market_id=market_id)
            
            # Add event to current episode
            if e.kind == "TRADE":
                current_episode.trades.append(e)
                if e.window_id:
                    current_episode.window_id = e.window_id
            else:
                current_episode.actions.append(e)
            
            last_ts = e.ts
        
        # Finalize last episode
        if current_episode.trades or current_episode.actions:
            current_episode.compute_aggregates()
            episodes.append(current_episode)
    
    # Sort episodes by start time
    episodes.sort(key=lambda ep: ep.start_ts or datetime.min)
    
    return episodes


def find_btc_15m_episodes(episodes: List[Episode]) -> List[Episode]:
    """Filter to only BTC 15m window episodes."""
    btc_episodes = []
    
    for ep in episodes:
        # Check if BTC 15m
        is_btc = False
        
        if ep.window_id and "15m" in ep.window_id.lower():
            is_btc = True
        
        if ep.market_id:
            market_lower = ep.market_id.lower()
            if "btc" in market_lower or "bitcoin" in market_lower:
                if "15" in market_lower:
                    is_btc = True
        
        # Check trade meta for market title
        for t in ep.trades:
            title = str(t.meta.get("title", "") or t.meta.get("question", "")).lower()
            if ("btc" in title or "bitcoin" in title) and "15" in title:
                is_btc = True
                break
        
        if is_btc:
            btc_episodes.append(ep)
    
    return btc_episodes


def compute_episode_stats(episodes: List[Episode]) -> Dict:
    """Compute aggregate stats across episodes."""
    total_trades = sum(ep.total_trades for ep in episodes)
    total_episodes = len(episodes)
    
    merge_count = sum(1 for ep in episodes if ep.has_merge)
    redeem_count = sum(1 for ep in episodes if ep.has_redeem)
    
    # Full-set stats
    full_set_candidates = [ep for ep in episodes if ep.matched_shares > 0]
    
    edges = []
    for ep in full_set_candidates:
        if ep.avg_cost_matched > 0:
            edge = 1.0 - ep.avg_cost_matched
            edges.append(edge)
    
    avg_edge = sum(edges) / len(edges) if edges else 0
    
    # Merge delays
    merge_delays = [ep.merge_delay_s for ep in episodes if ep.merge_delay_s is not None]
    avg_merge_delay = sum(merge_delays) / len(merge_delays) if merge_delays else None
    
    return {
        'total_episodes': total_episodes,
        'total_trades': total_trades,
        'merge_count': merge_count,
        'redeem_count': redeem_count,
        'full_set_candidates': len(full_set_candidates),
        'avg_edge': avg_edge,
        'avg_merge_delay_s': avg_merge_delay,
        'pct_merged': merge_count / total_episodes if total_episodes > 0 else 0,
    }


