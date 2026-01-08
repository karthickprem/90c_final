"""
Recorder and Replay Module

Records orderbook snapshots for later deterministic replay.
This is CRITICAL for validating edge before going live.

Key features:
- Record full orderbook depth to compressed JSONL
- Replay with deterministic random seed
- Identical results across runs (same seed)
"""

import gzip
import json
import logging
import time
import random
from typing import Optional, List, Dict, Generator, Any
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .config import ArbConfig, load_config
from .market_discovery import BTC15mMarket
from .orderbook import OrderbookFetcher, OrderBookSnapshot, TickData
from .metrics import RecordingMetrics

logger = logging.getLogger(__name__)


@dataclass
class RecordedWindow:
    """Metadata for a recorded trading window."""
    window_id: str
    market_id: str
    start_ts: datetime
    end_ts: datetime
    tick_count: int
    file_path: str


class Recorder:
    """
    Records orderbook snapshots for a trading window.
    
    Output format: Compressed JSONL with full orderbook depth.
    One file per window for easy management.
    """
    
    def __init__(self, config: ArbConfig = None, metrics: RecordingMetrics = None):
        self.config = config or load_config()
        self.metrics = metrics
        
        self.recording_dir = Path(self.config.recording_dir)
        self.recording_dir.mkdir(parents=True, exist_ok=True)
        
        self.fetcher = OrderbookFetcher(config)
    
    def record_window(self, market: BTC15mMarket) -> RecordedWindow:
        """
        Record orderbook snapshots for an entire trading window.
        
        Polls at config.poll_interval_ms until window ends.
        
        Args:
            market: The BTC 15-min market to record
        
        Returns:
            RecordedWindow with metadata about recording
        """
        # Create recording file
        ts_str = market.start_ts.strftime("%Y%m%d_%H%M%S")
        file_name = f"window_{market.window_id.replace(':', '-')}_{ts_str}.jsonl.gz"
        file_path = self.recording_dir / file_name
        
        logger.info(f"Recording window {market.window_id} to {file_path}")
        
        tick_count = 0
        
        with gzip.open(file_path, "wt", encoding="utf-8") as f:
            # Write header
            header = {
                "type": "header",
                "market_id": market.market_id,
                "window_id": market.window_id,
                "start_ts": market.start_ts.isoformat(),
                "end_ts": market.end_ts.isoformat(),
                "yes_token_id": market.yes_token_id,
                "no_token_id": market.no_token_id,
                "config": {
                    "poll_interval_ms": self.config.poll_interval_ms,
                    "min_edge": self.config.min_edge,
                    "slippage_buffer": self.config.slippage_buffer_per_leg,
                },
                "recorded_at": datetime.now(timezone.utc).isoformat(),
            }
            f.write(json.dumps(header) + "\n")
            
            # Record until window ends
            while market.seconds_remaining > 0:
                try:
                    # Fetch orderbooks
                    yes_book, no_book = self.fetcher.fetch_full_depth(
                        market.yes_token_id,
                        market.no_token_id
                    )
                    
                    if yes_book and no_book:
                        tick = {
                            "type": "tick",
                            "seq": tick_count,
                            "ts": datetime.now(timezone.utc).isoformat(),
                            "seconds_remaining": market.seconds_remaining,
                            "yes_book": yes_book.to_dict(),
                            "no_book": no_book.to_dict(),
                        }
                        f.write(json.dumps(tick) + "\n")
                        tick_count += 1
                        
                        # Log tick for metrics
                        if self.metrics:
                            self.metrics.log_tick_for_recording(
                                market.market_id,
                                market.window_id,
                                yes_book.to_dict(),
                                no_book.to_dict()
                            )
                        
                        if tick_count % 50 == 0:
                            logger.debug(f"Recorded {tick_count} ticks, {market.seconds_remaining:.0f}s remaining")
                    
                    # Wait for next poll
                    time.sleep(self.config.poll_interval_seconds)
                
                except KeyboardInterrupt:
                    logger.info("Recording interrupted")
                    break
                
                except Exception as e:
                    logger.warning(f"Error recording tick: {e}")
                    time.sleep(1)
            
            # Write footer
            footer = {
                "type": "footer",
                "tick_count": tick_count,
                "completed_at": datetime.now(timezone.utc).isoformat(),
            }
            f.write(json.dumps(footer) + "\n")
        
        logger.info(f"Recording complete: {tick_count} ticks in {file_path}")
        
        return RecordedWindow(
            window_id=market.window_id,
            market_id=market.market_id,
            start_ts=market.start_ts,
            end_ts=market.end_ts,
            tick_count=tick_count,
            file_path=str(file_path),
        )
    
    def list_recordings(self) -> List[RecordedWindow]:
        """List all recorded windows."""
        recordings = []
        
        for file_path in self.recording_dir.glob("window_*.jsonl.gz"):
            try:
                with gzip.open(file_path, "rt", encoding="utf-8") as f:
                    header_line = f.readline()
                    header = json.loads(header_line)
                    
                    if header.get("type") != "header":
                        continue
                    
                    recordings.append(RecordedWindow(
                        window_id=header.get("window_id", ""),
                        market_id=header.get("market_id", ""),
                        start_ts=datetime.fromisoformat(header.get("start_ts", "")),
                        end_ts=datetime.fromisoformat(header.get("end_ts", "")),
                        tick_count=0,  # Would need to scan file
                        file_path=str(file_path),
                    ))
            except Exception as e:
                logger.warning(f"Could not read {file_path}: {e}")
        
        # Sort by start time
        recordings.sort(key=lambda r: r.start_ts, reverse=True)
        
        return recordings


class Replayer:
    """
    Replays recorded orderbook data deterministically.
    
    Key properties:
    - Same seed = identical results
    - Simulates time progression
    - Can be used as orderbook source for strategy
    """
    
    def __init__(self, config: ArbConfig = None, recording_path: str = None):
        self.config = config or load_config()
        self.recording_path = Path(recording_path) if recording_path else None
        
        # Set random seed for determinism
        random.seed(self.config.replay_seed)
        
        # Loaded data
        self.header: Optional[Dict] = None
        self.ticks: List[Dict] = []
        self.tick_index = 0
        
        # Market info (from header)
        self.market_id: str = ""
        self.window_id: str = ""
        self.yes_token_id: str = ""
        self.no_token_id: str = ""
        self.start_ts: Optional[datetime] = None
        self.end_ts: Optional[datetime] = None
        
        if recording_path:
            self._load_recording()
    
    def _load_recording(self):
        """Load recording from file."""
        if not self.recording_path or not self.recording_path.exists():
            raise FileNotFoundError(f"Recording not found: {self.recording_path}")
        
        logger.info(f"Loading recording: {self.recording_path}")
        
        self.ticks = []
        
        with gzip.open(self.recording_path, "rt", encoding="utf-8") as f:
            for line in f:
                data = json.loads(line.strip())
                
                if data.get("type") == "header":
                    self.header = data
                    self.market_id = data.get("market_id", "")
                    self.window_id = data.get("window_id", "")
                    self.yes_token_id = data.get("yes_token_id", "")
                    self.no_token_id = data.get("no_token_id", "")
                    self.start_ts = datetime.fromisoformat(data.get("start_ts", ""))
                    self.end_ts = datetime.fromisoformat(data.get("end_ts", ""))
                
                elif data.get("type") == "tick":
                    self.ticks.append(data)
                
                elif data.get("type") == "footer":
                    pass  # Ignore footer
        
        logger.info(f"Loaded {len(self.ticks)} ticks from {self.window_id}")
    
    def reset(self):
        """Reset to beginning of recording."""
        self.tick_index = 0
        random.seed(self.config.replay_seed)
    
    def has_more_ticks(self) -> bool:
        """Check if more ticks available."""
        return self.tick_index < len(self.ticks)
    
    def next_tick(self) -> Optional[TickData]:
        """Get next tick from recording."""
        if not self.has_more_ticks():
            return None
        
        tick_data = self.ticks[self.tick_index]
        self.tick_index += 1
        
        # Parse orderbooks
        yes_book = OrderBookSnapshot.from_dict(tick_data.get("yes_book", {}))
        no_book = OrderBookSnapshot.from_dict(tick_data.get("no_book", {}))
        
        if not yes_book.best_ask or not no_book.best_ask:
            return self.next_tick()  # Skip invalid ticks
        
        return TickData(
            timestamp=datetime.fromisoformat(tick_data.get("ts", "")),
            market_id=self.market_id,
            window_id=self.window_id,
            # YES side
            ask_yes=yes_book.best_ask.price,
            ask_yes_size=yes_book.best_ask.size,
            bid_yes=yes_book.best_bid.price if yes_book.best_bid else 0,
            bid_yes_size=yes_book.best_bid.size if yes_book.best_bid else 0,
            yes_book=yes_book,
            # NO side
            ask_no=no_book.best_ask.price,
            ask_no_size=no_book.best_ask.size,
            bid_no=no_book.best_bid.price if no_book.best_bid else 0,
            bid_no_size=no_book.best_bid.size if no_book.best_bid else 0,
            no_book=no_book,
        )
    
    def all_ticks(self) -> Generator[TickData, None, None]:
        """Generator yielding all ticks."""
        self.reset()
        while self.has_more_ticks():
            tick = self.next_tick()
            if tick:
                yield tick
    
    def fetch_top_of_book(self, yes_token_id: str = None, no_token_id: str = None,
                          market_id: str = "", window_id: str = "") -> Optional[TickData]:
        """
        Fetch next tick (compatible with OrderbookFetcher interface).
        
        Token IDs are ignored - returns recorded data.
        """
        return self.next_tick()
    
    def get_simulated_seconds_remaining(self) -> float:
        """Get simulated seconds remaining based on tick position."""
        if not self.ticks or not self.end_ts:
            return 0
        
        if self.tick_index >= len(self.ticks):
            return 0
        
        tick_data = self.ticks[self.tick_index]
        return tick_data.get("seconds_remaining", 0)
    
    def replay_all(self, strategy) -> List[Any]:
        """
        Replay all ticks through strategy engine.
        
        Args:
            strategy: StrategyEngine instance
        
        Returns:
            List of window results
        """
        from .market_discovery import BTC15mMarket
        
        results = []
        
        # Create market object from header
        market = BTC15mMarket(
            market_id=self.market_id,
            event_id="",
            slug=self.window_id,
            question=f"BTC 15-min replay {self.window_id}",
            yes_token_id=self.yes_token_id,
            no_token_id=self.no_token_id,
            window_id=self.window_id,
            start_ts=self.start_ts,
            end_ts=self.end_ts,
            active=True,
            closed=False,
        )
        
        # Run strategy on recorded data
        self.reset()
        result = strategy.trade_window(market, self)
        results.append(result)
        
        return results


def verify_determinism(recording_path: str, runs: int = 3) -> bool:
    """
    Verify that replay produces identical results across runs.
    
    This is crucial for validating edge.
    """
    from .strategy import StrategyEngine
    from .executor_paper import PaperExecutor
    from .ledger import Ledger
    from .metrics import MetricsLogger
    
    config = load_config()
    
    all_results = []
    
    for run in range(runs):
        logger.info(f"Verification run {run + 1}/{runs}")
        
        # Fresh instances each run
        metrics = MetricsLogger(config)
        ledger = Ledger(config)
        replayer = Replayer(config, recording_path)
        
        from .orderbook import OrderbookFetcher
        executor = PaperExecutor(config, replayer, metrics, ledger)
        strategy = StrategyEngine(config, executor, metrics, ledger)
        
        results = replayer.replay_all(strategy)
        
        # Extract key metrics
        if results:
            r = results[0]
            key_metrics = {
                "safe_profit_net": round(r.safe_profit_net, 6),
                "trades_count": r.trades_count,
                "qty_yes": round(r.qty_yes, 6),
                "qty_no": round(r.qty_no, 6),
            }
            all_results.append(key_metrics)
        
        metrics.close()
    
    # Check all results are identical
    if len(all_results) < 2:
        logger.error("Not enough results to verify")
        return False
    
    first = all_results[0]
    for i, result in enumerate(all_results[1:], 2):
        if result != first:
            logger.error(f"Run {i} differs from run 1!")
            logger.error(f"Run 1: {first}")
            logger.error(f"Run {i}: {result}")
            return False
    
    logger.info(f"Determinism verified: {runs} runs produced identical results")
    logger.info(f"Result: {first}")
    return True


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    
    recorder = Recorder()
    
    print("\n=== Recorder/Replayer Module ===\n")
    print(f"Recording directory: {recorder.recording_dir}")
    
    # List existing recordings
    recordings = recorder.list_recordings()
    
    if recordings:
        print(f"\nFound {len(recordings)} recordings:")
        for rec in recordings[:5]:
            print(f"  - {rec.window_id}: {rec.tick_count} ticks")
    else:
        print("\nNo recordings found yet.")
        print("Run in --mode record to start recording.")

