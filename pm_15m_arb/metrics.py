"""
Metrics Module - JSONL Logging

Logs every tick, decision, and fill to JSONL for analysis.
This is critical for validating edge and debugging.

Event types:
- TICK: Orderbook snapshot
- SIGNAL: Trade signal detected
- ORDER_SUBMIT: Order submitted
- FILL: Order filled
- CANCEL: Order cancelled
- LEG_EVENT: Legging event (timeout, unwind)
- WINDOW_START: New trading window started
- WINDOW_END: Trading window ended
- ERROR: Error occurred
"""

import json
import logging
import gzip
from typing import Dict, Any, Optional
from datetime import datetime
from pathlib import Path
from enum import Enum

from .config import ArbConfig, load_config

logger = logging.getLogger(__name__)


class EventType(Enum):
    """Event types for metrics logging."""
    TICK = "TICK"
    SIGNAL = "SIGNAL"
    ORDER_SUBMIT = "ORDER_SUBMIT"
    FILL = "FILL"
    CANCEL = "CANCEL"
    LEG_EVENT = "LEG_EVENT"
    WINDOW_START = "WINDOW_START"
    WINDOW_END = "WINDOW_END"
    POSITION_UPDATE = "POSITION_UPDATE"
    ERROR = "ERROR"


class MetricsLogger:
    """
    Logs all bot activity to JSONL files for analysis.
    
    File format: One JSON object per line, with timestamp and event type.
    Can optionally compress to gzip for storage efficiency.
    """
    
    def __init__(self, config: ArbConfig = None):
        self.config = config or load_config()
        self.metrics_file = Path(self.config.metrics_file)
        
        # Ensure directory exists
        self.metrics_file.parent.mkdir(parents=True, exist_ok=True)
        
        # Event counter for ordering
        self._event_seq = 0
        
        # Batch buffer for high-frequency events
        self._buffer: list = []
        self._buffer_size = 100
    
    def _write_event(self, event_type: EventType, data: Dict[str, Any]):
        """Write an event to the JSONL file."""
        self._event_seq += 1
        
        event = {
            "seq": self._event_seq,
            "ts": datetime.utcnow().isoformat() + "Z",
            "event": event_type.value,
            **data
        }
        
        with open(self.metrics_file, "a") as f:
            f.write(json.dumps(event, default=str) + "\n")
    
    def _write_buffered(self, event_type: EventType, data: Dict[str, Any]):
        """Buffer event and flush when buffer is full."""
        self._event_seq += 1
        
        event = {
            "seq": self._event_seq,
            "ts": datetime.utcnow().isoformat() + "Z",
            "event": event_type.value,
            **data
        }
        
        self._buffer.append(event)
        
        if len(self._buffer) >= self._buffer_size:
            self.flush()
    
    def flush(self):
        """Flush buffered events to file."""
        if not self._buffer:
            return
        
        with open(self.metrics_file, "a") as f:
            for event in self._buffer:
                f.write(json.dumps(event, default=str) + "\n")
        
        self._buffer.clear()
    
    def log_tick(self, market_id: str, window_id: str,
                 ask_yes: float, ask_yes_size: float,
                 ask_no: float, ask_no_size: float,
                 bid_yes: Optional[float] = None,
                 bid_no: Optional[float] = None,
                 depth_yes: Optional[list] = None,
                 depth_no: Optional[list] = None):
        """Log orderbook tick (buffered for high frequency)."""
        self._write_buffered(EventType.TICK, {
            "market_id": market_id,
            "window_id": window_id,
            "ask_yes": ask_yes,
            "ask_yes_size": ask_yes_size,
            "ask_no": ask_no,
            "ask_no_size": ask_no_size,
            "bid_yes": bid_yes,
            "bid_no": bid_no,
            "sum_asks": ask_yes + ask_no,
            # Include depth arrays if provided (for replay)
            "depth_yes": depth_yes[:5] if depth_yes else None,
            "depth_no": depth_no[:5] if depth_no else None,
        })
    
    def log_signal(self, market_id: str, window_id: str,
                   pair_cost: float, edge: float,
                   is_actionable: bool, reason: Optional[str] = None,
                   qty: Optional[float] = None):
        """Log trade signal detection."""
        self._write_event(EventType.SIGNAL, {
            "market_id": market_id,
            "window_id": window_id,
            "pair_cost": pair_cost,
            "edge": edge,
            "is_actionable": is_actionable,
            "reject_reason": reason,
            "qty": qty,
        })
    
    def log_order_submit(self, order_id: str, market_id: str,
                         side: str, token: str,
                         price: float, qty: float,
                         order_type: str = "LIMIT"):
        """Log order submission."""
        self._write_event(EventType.ORDER_SUBMIT, {
            "order_id": order_id,
            "market_id": market_id,
            "side": side,  # "YES" or "NO"
            "token": token,
            "price": price,
            "qty": qty,
            "order_type": order_type,
        })
    
    def log_fill(self, order_id: str, market_id: str,
                 side: str, filled_qty: float, fill_price: float,
                 slippage: float = 0, partial: bool = False):
        """Log order fill."""
        self._write_event(EventType.FILL, {
            "order_id": order_id,
            "market_id": market_id,
            "side": side,
            "filled_qty": filled_qty,
            "fill_price": fill_price,
            "slippage": slippage,
            "partial": partial,
        })
    
    def log_cancel(self, order_id: str, market_id: str,
                   side: str, reason: str):
        """Log order cancellation."""
        self._write_event(EventType.CANCEL, {
            "order_id": order_id,
            "market_id": market_id,
            "side": side,
            "reason": reason,
        })
    
    def log_leg_event(self, market_id: str, window_id: str,
                      event_subtype: str,  # "TIMEOUT", "UNWIND", "COMPLETE_MISSING"
                      filled_leg: str,  # "YES" or "NO"
                      action: str,  # "cancel", "unwind", "retry"
                      loss: Optional[float] = None,
                      details: Optional[dict] = None):
        """Log legging event."""
        self._write_event(EventType.LEG_EVENT, {
            "market_id": market_id,
            "window_id": window_id,
            "event_subtype": event_subtype,
            "filled_leg": filled_leg,
            "action": action,
            "loss": loss,
            "details": details or {},
        })
    
    def log_window_start(self, market_id: str, window_id: str,
                         start_ts: datetime, end_ts: datetime,
                         yes_token_id: str, no_token_id: str):
        """Log new trading window start."""
        self._write_event(EventType.WINDOW_START, {
            "market_id": market_id,
            "window_id": window_id,
            "start_ts": start_ts.isoformat(),
            "end_ts": end_ts.isoformat(),
            "yes_token_id": yes_token_id,
            "no_token_id": no_token_id,
        })
    
    def log_window_end(self, market_id: str, window_id: str,
                       qty_yes: float, qty_no: float,
                       cost_yes: float, cost_no: float,
                       safe_profit_net: float,
                       trades_count: int,
                       legging_events: int):
        """Log trading window end with summary."""
        self.flush()  # Flush any buffered ticks first
        
        self._write_event(EventType.WINDOW_END, {
            "market_id": market_id,
            "window_id": window_id,
            "qty_yes": qty_yes,
            "qty_no": qty_no,
            "cost_yes": cost_yes,
            "cost_no": cost_no,
            "total_cost": cost_yes + cost_no,
            "safe_profit_net": safe_profit_net,
            "trades_count": trades_count,
            "legging_events": legging_events,
        })
    
    def log_position_update(self, market_id: str, window_id: str,
                            qty_yes: float, qty_no: float,
                            cost_yes: float, cost_no: float,
                            safe_profit_net: float):
        """Log position state update."""
        self._write_buffered(EventType.POSITION_UPDATE, {
            "market_id": market_id,
            "window_id": window_id,
            "qty_yes": qty_yes,
            "qty_no": qty_no,
            "cost_yes": cost_yes,
            "cost_no": cost_no,
            "safe_profit_net": safe_profit_net,
        })
    
    def log_error(self, error: str, context: Dict[str, Any] = None):
        """Log an error."""
        self.flush()  # Ensure we don't lose events before error
        
        self._write_event(EventType.ERROR, {
            "error": error,
            "context": context or {},
        })
    
    def close(self):
        """Flush and close the logger."""
        self.flush()


class RecordingMetrics(MetricsLogger):
    """
    Extended metrics logger that also writes to a recording file
    for later replay. Includes full orderbook depth.
    """
    
    def __init__(self, config: ArbConfig = None, recording_path: str = None):
        super().__init__(config)
        
        self.recording_dir = Path(config.recording_dir if config else "pm_15m_recordings")
        self.recording_dir.mkdir(parents=True, exist_ok=True)
        
        # Recording file (compressed)
        if recording_path:
            self.recording_path = Path(recording_path)
        else:
            ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
            self.recording_path = self.recording_dir / f"recording_{ts}.jsonl.gz"
        
        self._recording_file = None
    
    def _get_recording_file(self):
        """Get or create recording file handle."""
        if self._recording_file is None:
            self._recording_file = gzip.open(self.recording_path, "wt", encoding="utf-8")
        return self._recording_file
    
    def log_tick_for_recording(self, market_id: str, window_id: str,
                               yes_book: dict, no_book: dict):
        """
        Log full orderbook snapshot for recording/replay.
        
        Args:
            yes_book: Full orderbook dict with 'asks' and 'bids' arrays
            no_book: Full orderbook dict with 'asks' and 'bids' arrays
        """
        event = {
            "seq": self._event_seq + 1,
            "ts": datetime.utcnow().isoformat() + "Z",
            "event": "TICK_FULL",
            "market_id": market_id,
            "window_id": window_id,
            "yes_book": yes_book,
            "no_book": no_book,
        }
        
        f = self._get_recording_file()
        f.write(json.dumps(event, default=str) + "\n")
    
    def close(self):
        """Close all file handles."""
        super().close()
        
        if self._recording_file:
            self._recording_file.close()
            self._recording_file = None
            logger.info(f"Recording saved to {self.recording_path}")


if __name__ == "__main__":
    # Test metrics logger
    logging.basicConfig(level=logging.INFO)
    
    config = ArbConfig()
    config.metrics_file = "test_pm_15m_metrics.jsonl"
    
    metrics = MetricsLogger(config)
    
    # Log some test events
    metrics.log_window_start(
        market_id="test_market",
        window_id="2024-01-01_12:00",
        start_ts=datetime.utcnow(),
        end_ts=datetime.utcnow(),
        yes_token_id="yes_123",
        no_token_id="no_456"
    )
    
    for i in range(5):
        metrics.log_tick(
            market_id="test_market",
            window_id="2024-01-01_12:00",
            ask_yes=0.48 + i * 0.001,
            ask_yes_size=100,
            ask_no=0.50 + i * 0.001,
            ask_no_size=80,
        )
    
    metrics.log_signal(
        market_id="test_market",
        window_id="2024-01-01_12:00",
        pair_cost=0.97,
        edge=0.03,
        is_actionable=True,
        qty=10,
    )
    
    metrics.close()
    
    print(f"Wrote test events to {config.metrics_file}")
    
    # Read and display
    with open(config.metrics_file) as f:
        for line in f:
            print(line.strip())

