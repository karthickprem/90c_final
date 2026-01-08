"""Merge BUY and SELL tick streams into unified quotes."""
from dataclasses import dataclass
from typing import List, Optional
from .parse import RawTick


@dataclass
class QuoteTick:
    """A merged tick with both bid and ask for UP and DOWN."""
    elapsed_secs: float
    up_ask: int  # From market_logs (BUY price)
    up_bid: int  # From market_logs_sell (SELL price)
    down_ask: int  # From market_logs (BUY price)
    down_bid: int  # From market_logs_sell (SELL price)
    
    @property
    def up_mid(self) -> float:
        return (self.up_ask + self.up_bid) / 2.0
    
    @property
    def down_mid(self) -> float:
        return (self.down_ask + self.down_bid) / 2.0
    
    @property
    def up_spread(self) -> int:
        return self.up_ask - self.up_bid
    
    @property
    def down_spread(self) -> int:
        return self.down_ask - self.down_bid
    
    def is_valid(self) -> bool:
        """Check if all prices are valid (0-100 range)."""
        return (
            0 <= self.up_ask <= 100 and
            0 <= self.up_bid <= 100 and
            0 <= self.down_ask <= 100 and
            0 <= self.down_bid <= 100 and
            self.up_bid <= self.up_ask and
            self.down_bid <= self.down_ask
        )


def merge_tick_streams(
    buy_ticks: List[RawTick],
    sell_ticks: List[RawTick]
) -> List[QuoteTick]:
    """Merge BUY (ASK) and SELL (BID) tick streams with forward-fill.
    
    Creates a unified stream at union of all timestamps.
    Forward-fills last known values when one side is missing.
    """
    if not buy_ticks and not sell_ticks:
        return []
    
    # Build timeline with all unique timestamps
    all_times = set()
    for t in buy_ticks:
        all_times.add(round(t.elapsed_secs, 3))
    for t in sell_ticks:
        all_times.add(round(t.elapsed_secs, 3))
    
    sorted_times = sorted(all_times)
    
    # Index into each stream
    buy_idx = 0
    sell_idx = 0
    
    # Last known values (forward-fill)
    last_up_ask: Optional[int] = None
    last_down_ask: Optional[int] = None
    last_up_bid: Optional[int] = None
    last_down_bid: Optional[int] = None
    
    result = []
    
    for t in sorted_times:
        # Update from buy stream (ASK prices)
        while buy_idx < len(buy_ticks) and round(buy_ticks[buy_idx].elapsed_secs, 3) <= t:
            tick = buy_ticks[buy_idx]
            last_up_ask = tick.up_cents
            last_down_ask = tick.down_cents
            buy_idx += 1
        
        # Update from sell stream (BID prices)
        while sell_idx < len(sell_ticks) and round(sell_ticks[sell_idx].elapsed_secs, 3) <= t:
            tick = sell_ticks[sell_idx]
            last_up_bid = tick.up_cents
            last_down_bid = tick.down_cents
            sell_idx += 1
        
        # Only emit if we have all values
        if all(v is not None for v in [last_up_ask, last_up_bid, last_down_ask, last_down_bid]):
            quote = QuoteTick(
                elapsed_secs=t,
                up_ask=last_up_ask,
                up_bid=last_up_bid,
                down_ask=last_down_ask,
                down_bid=last_down_bid
            )
            # Only add if valid
            if quote.is_valid():
                result.append(quote)
    
    return result


def load_window_stream(
    window_id: str,
    buy_dir: str,
    sell_dir: str
) -> List[QuoteTick]:
    """Load and merge a complete window stream."""
    from .parse import load_window_ticks
    
    buy_ticks, sell_ticks = load_window_ticks(window_id, buy_dir, sell_dir)
    return merge_tick_streams(buy_ticks, sell_ticks)


@dataclass
class WindowData:
    """Complete data for a single window."""
    window_id: str
    ticks: List[QuoteTick]
    
    @property
    def num_ticks(self) -> int:
        return len(self.ticks)
    
    @property
    def duration_secs(self) -> float:
        if not self.ticks:
            return 0.0
        return self.ticks[-1].elapsed_secs - self.ticks[0].elapsed_secs
    
    @property
    def first_tick(self) -> Optional[QuoteTick]:
        return self.ticks[0] if self.ticks else None
    
    @property
    def last_tick(self) -> Optional[QuoteTick]:
        return self.ticks[-1] if self.ticks else None
    
    def get_winner(self) -> Optional[str]:
        """Determine window winner from final tick."""
        if not self.ticks:
            return None
        final = self.ticks[-1]
        # Resolved if one side >= 97 and other <= 3
        if final.up_ask >= 97 and final.down_ask <= 3:
            return "UP"
        if final.down_ask >= 97 and final.up_ask <= 3:
            return "DOWN"
        # Fallback to higher side
        if final.up_ask > final.down_ask:
            return "UP"
        elif final.down_ask > final.up_ask:
            return "DOWN"
        return None


def load_all_windows(buy_dir: str, sell_dir: str) -> List[WindowData]:
    """Load all windows from data directories."""
    from .parse import find_window_ids
    
    window_ids = find_window_ids(buy_dir)
    windows = []
    
    for wid in window_ids:
        ticks = load_window_stream(wid, buy_dir, sell_dir)
        if ticks:  # Only include windows with valid ticks
            windows.append(WindowData(window_id=wid, ticks=ticks))
    
    return windows


