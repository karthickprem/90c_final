"""
V13 VOLATILITY MODULE
=====================

Production-grade volatility calculation using time-based window (not tick-count).

Definition:
- Maintain a deque of (timestamp, mid) for the last 10 seconds
- vol_10s_cents = (max(mid) - min(mid)) * 100
- move_10s_cents = abs(mid_now - mid_10s_ago) * 100
"""

import time
from collections import deque
from dataclasses import dataclass
from typing import Optional, Tuple


# Default window size in seconds
VOL_WINDOW_SECS = 10.0


@dataclass
class VolatilitySnapshot:
    """Snapshot of volatility metrics at a point in time."""
    mid_now: float
    mid_min: float
    mid_max: float
    mid_oldest: float
    vol_10s_cents: float  # Range-based: (max - min) * 100
    move_10s_cents: float  # Point-to-point: abs(now - oldest) * 100
    sample_count: int
    window_secs: float


class VolatilityTracker:
    """
    Time-based volatility tracker using wall-clock timestamps.
    
    NOT tick-count based - tick rate varies, so we use real time.
    """
    
    def __init__(self, window_secs: float = VOL_WINDOW_SECS):
        self.window_secs = window_secs
        # Deque of (timestamp, mid) tuples
        self._samples: deque = deque()
    
    def update(self, mid: float, timestamp: Optional[float] = None) -> VolatilitySnapshot:
        """
        Add a new mid price sample and compute volatility.
        
        Args:
            mid: Current mid price (0.0 to 1.0)
            timestamp: Unix timestamp (defaults to now)
        
        Returns:
            VolatilitySnapshot with current metrics
        """
        if timestamp is None:
            timestamp = time.time()
        
        # Add new sample
        self._samples.append((timestamp, mid))
        
        # Prune old samples outside window
        cutoff = timestamp - self.window_secs
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()
        
        # Compute metrics
        return self._compute_snapshot(mid, timestamp)
    
    def _compute_snapshot(self, mid_now: float, timestamp: float) -> VolatilitySnapshot:
        """Compute volatility snapshot from current samples."""
        if not self._samples:
            return VolatilitySnapshot(
                mid_now=mid_now,
                mid_min=mid_now,
                mid_max=mid_now,
                mid_oldest=mid_now,
                vol_10s_cents=0.0,
                move_10s_cents=0.0,
                sample_count=0,
                window_secs=self.window_secs
            )
        
        # Extract mids from samples
        mids = [m for _, m in self._samples]
        
        mid_min = min(mids)
        mid_max = max(mids)
        mid_oldest = self._samples[0][1]
        
        # Range-based volatility (catches spikes even if delta is small)
        vol_10s_cents = (mid_max - mid_min) * 100
        
        # Point-to-point move
        move_10s_cents = abs(mid_now - mid_oldest) * 100
        
        return VolatilitySnapshot(
            mid_now=mid_now,
            mid_min=mid_min,
            mid_max=mid_max,
            mid_oldest=mid_oldest,
            vol_10s_cents=vol_10s_cents,
            move_10s_cents=move_10s_cents,
            sample_count=len(self._samples),
            window_secs=self.window_secs
        )
    
    def get_current(self) -> Optional[VolatilitySnapshot]:
        """Get current volatility without adding a sample."""
        if not self._samples:
            return None
        
        latest_ts, latest_mid = self._samples[-1]
        return self._compute_snapshot(latest_mid, latest_ts)
    
    def reset(self):
        """Clear all samples."""
        self._samples.clear()


def compute_vol_distribution(ticks: list, window_secs: float = 10.0) -> dict:
    """
    Compute volatility distribution from historical tick data.
    
    Args:
        ticks: List of (timestamp, mid) tuples sorted by timestamp
        window_secs: Window size in seconds
    
    Returns:
        Dict with P50, P75, P90, P95, P99 percentiles
    """
    if len(ticks) < 2:
        return {'P50': 0, 'P75': 0, 'P90': 0, 'P95': 0, 'P99': 0}
    
    tracker = VolatilityTracker(window_secs)
    vol_readings = []
    
    for ts, mid in ticks:
        snapshot = tracker.update(mid, ts)
        if snapshot.sample_count >= 2:  # Need at least 2 samples
            vol_readings.append(snapshot.vol_10s_cents)
    
    if not vol_readings:
        return {'P50': 0, 'P75': 0, 'P90': 0, 'P95': 0, 'P99': 0}
    
    vol_readings.sort()
    n = len(vol_readings)
    
    def percentile(p):
        idx = int(n * p / 100)
        return vol_readings[min(idx, n - 1)]
    
    return {
        'P50': percentile(50),
        'P75': percentile(75),
        'P90': percentile(90),
        'P95': percentile(95),
        'P99': percentile(99),
        'count': n
    }

