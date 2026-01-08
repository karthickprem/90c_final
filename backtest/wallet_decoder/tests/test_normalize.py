"""Tests for normalize.py"""

import json
from pathlib import Path
from datetime import datetime

import pytest

from wallet_decoder.normalize import (
    Event,
    parse_timestamp,
    parse_price,
    parse_outcome,
    normalize_trade,
    normalize_activity,
    normalize_all,
    extract_window_id,
)


FIXTURES_DIR = Path(__file__).parent / "fixtures"


def load_fixture(name: str):
    """Load a fixture file."""
    with open(FIXTURES_DIR / name) as f:
        return json.load(f)


class TestParseTimestamp:
    """Tests for timestamp parsing."""
    
    def test_iso_string(self):
        ts = parse_timestamp("2024-12-15T10:30:00Z")
        assert ts is not None
        assert ts.year == 2024
        assert ts.month == 12
        assert ts.day == 15
    
    def test_unix_seconds(self):
        ts = parse_timestamp(1702637400)  # Some unix timestamp
        assert ts is not None
        assert isinstance(ts, datetime)
    
    def test_unix_milliseconds(self):
        ts = parse_timestamp(1702637400000)
        assert ts is not None
    
    def test_none(self):
        assert parse_timestamp(None) is None
    
    def test_invalid(self):
        assert parse_timestamp("not a date") is None


class TestParsePrice:
    """Tests for price parsing."""
    
    def test_float_in_range(self):
        assert parse_price(0.45) == 0.45
    
    def test_cents_to_dollars(self):
        # If > 1.0, assume cents
        assert parse_price(45) == 0.45
        assert parse_price(90) == 0.90
    
    def test_clamp(self):
        assert parse_price(-0.1) == 0.0
        assert parse_price(150) == 1.0
    
    def test_none(self):
        assert parse_price(None) is None


class TestParseOutcome:
    """Tests for outcome parsing."""
    
    def test_up(self):
        assert parse_outcome("Up", "BTC market") == "UP"
        assert parse_outcome("up", "") == "UP"
    
    def test_down(self):
        assert parse_outcome("Down", "BTC market") == "DOWN"
        assert parse_outcome("down", "") == "DOWN"
    
    def test_yes_no(self):
        assert parse_outcome("Yes", "") == "YES"
        assert parse_outcome("No", "") == "NO"
    
    def test_none(self):
        assert parse_outcome(None, "") is None


class TestNormalizeTrade:
    """Tests for trade normalization."""
    
    def test_basic_trade(self):
        raw = {
            "timestamp": "2024-12-15T10:30:00Z",
            "conditionId": "0xabc123",
            "outcome": "Up",
            "side": "BUY",
            "price": 0.45,
            "size": 100,
        }
        
        event = normalize_trade(raw)
        
        assert event.kind == "TRADE"
        assert event.market_id == "0xabc123"
        assert event.outcome == "UP"
        assert event.side == "BUY"
        assert event.price == 0.45
        assert event.size == 100
    
    def test_missing_fields(self):
        # Should not crash with minimal data
        raw = {"timestamp": "2024-12-15T10:30:00Z"}
        event = normalize_trade(raw)
        assert event.kind == "TRADE"
    
    def test_price_in_cents(self):
        raw = {
            "timestamp": "2024-12-15T10:30:00Z",
            "price": 45,  # Cents
        }
        event = normalize_trade(raw)
        assert event.price == 0.45


class TestNormalizeActivity:
    """Tests for activity normalization."""
    
    def test_merge(self):
        raw = {
            "timestamp": "2024-12-15T10:30:00Z",
            "type": "merge",
            "conditionId": "0xabc123",
        }
        
        event = normalize_activity(raw)
        
        assert event.kind == "MERGE"
        assert event.market_id == "0xabc123"
    
    def test_redeem(self):
        raw = {
            "timestamp": "2024-12-15T10:30:00Z",
            "type": "redeem",
            "cashDelta": 50.0,
        }
        
        event = normalize_activity(raw)
        
        assert event.kind == "REDEEM"
        assert event.cash_delta == 50.0
    
    def test_unknown_type(self):
        raw = {
            "timestamp": "2024-12-15T10:30:00Z",
            "type": "something_new",
        }
        
        event = normalize_activity(raw)
        assert event.kind == "UNKNOWN"


class TestNormalizeAll:
    """Tests for full normalization pipeline."""
    
    def test_with_fixtures(self):
        trades = load_fixture("trades_page.json")
        activity = load_fixture("activity_page.json")
        
        events = normalize_all(trades, activity)
        
        assert len(events) == len(trades) + len(activity)
        
        # Should be sorted by time
        for i in range(1, len(events)):
            assert events[i].ts >= events[i-1].ts
        
        # Check we got expected types
        kinds = set(e.kind for e in events)
        assert "TRADE" in kinds
        assert "MERGE" in kinds
    
    def test_empty_inputs(self):
        events = normalize_all([], [])
        assert events == []


class TestExtractWindowId:
    """Tests for window ID extraction."""
    
    def test_from_slug(self):
        market_id = "btc-updown-15m-1702637400"
        assert extract_window_id(market_id, "") == "btc-updown-15m-1702637400"
    
    def test_from_timestamp(self):
        market_id = "0xabc123"
        title = "Bitcoin Up or Down 15m"
        ts = datetime(2024, 12, 15, 10, 30, 0)
        
        window_id = extract_window_id(market_id, title, ts)
        
        # Should compute window start from timestamp
        assert window_id is not None
        assert "15m" in window_id


