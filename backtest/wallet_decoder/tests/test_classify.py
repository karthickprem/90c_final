"""Tests for classify.py"""

import pytest
from datetime import datetime, timedelta

from wallet_decoder.normalize import Event
from wallet_decoder.enrich import Episode
from wallet_decoder.classify import (
    classify_episode,
    classify_all,
    compute_label_distribution,
    Classification,
)


def make_trade(
    ts: datetime,
    outcome: str = "UP",
    side: str = "BUY",
    price: float = 0.5,
    size: float = 100,
) -> Event:
    """Create a test trade event."""
    return Event(
        ts=ts,
        kind="TRADE",
        market_id="test_market",
        outcome=outcome,
        side=side,
        price=price,
        size=size,
    )


def make_action(ts: datetime, kind: str = "MERGE") -> Event:
    """Create a test action event."""
    return Event(
        ts=ts,
        kind=kind,
        market_id="test_market",
    )


class TestClassifyEpisode:
    """Tests for episode classification."""
    
    def test_full_set_arb(self):
        """Test FULL_SET_ARB classification."""
        ts = datetime(2024, 12, 15, 10, 30, 0)
        
        ep = Episode(market_id="test")
        ep.trades = [
            make_trade(ts, "UP", "BUY", 0.45, 100),
            make_trade(ts + timedelta(seconds=5), "DOWN", "BUY", 0.52, 100),
        ]
        ep.actions = [
            make_action(ts + timedelta(seconds=10), "MERGE"),
        ]
        
        # Compute aggregates
        ep.total_up_bought = 100
        ep.total_down_bought = 100
        ep.cost_up = 45  # 0.45 * 100
        ep.cost_down = 52  # 0.52 * 100
        ep.net_up = 100
        ep.net_down = 100
        ep.matched_shares = 100
        ep.avg_cost_matched = 0.97  # 0.45 + 0.52
        ep.has_merge = True
        ep.merge_delay_s = 10
        ep.total_trades = 2
        
        cls = classify_episode(ep)
        
        assert cls.label == "FULL_SET_ARB"
        assert cls.confidence > 0.5
        assert "match_ratio" in str(cls.reasons) or "has_merge" in str(cls.reasons)
    
    def test_directional_spike(self):
        """Test DIRECTIONAL_SPIKE classification."""
        ts = datetime(2024, 12, 15, 10, 30, 0)
        
        ep = Episode(market_id="test")
        ep.trades = [
            make_trade(ts, "UP", "BUY", 0.90, 500),
        ]
        ep.actions = [
            make_action(ts + timedelta(minutes=15), "REDEEM"),
        ]
        
        ep.total_up_bought = 500
        ep.net_up = 500
        ep.net_down = 0
        ep.matched_shares = 0
        ep.has_merge = False
        ep.has_redeem = True
        ep.total_trades = 1
        
        cls = classify_episode(ep)
        
        assert cls.label == "DIRECTIONAL_SPIKE"
        assert "directional_UP" in str(cls.reasons) or "redeem_without_merge" in str(cls.reasons)
    
    def test_market_making(self):
        """Test MARKET_MAKING classification."""
        ts = datetime(2024, 12, 15, 10, 30, 0)
        
        # Many trades, alternating, flat inventory
        trades = []
        for i in range(30):
            side = "BUY" if i % 2 == 0 else "SELL"
            outcome = "UP" if i % 2 == 0 else "DOWN"
            t = make_trade(ts + timedelta(seconds=i*10), outcome, side, 0.5, 10)
            t.meta = {"isMaker": True}
            trades.append(t)
        
        ep = Episode(market_id="test")
        ep.trades = trades
        ep.total_up_bought = 150
        ep.total_up_sold = 150
        ep.total_down_bought = 150
        ep.total_down_sold = 150
        ep.net_up = 0
        ep.net_down = 0
        ep.matched_shares = 0
        ep.has_merge = False
        ep.total_trades = 30
        
        cls = classify_episode(ep)
        
        # Should have market making signals
        assert cls.scores['market_making'] > 0
    
    def test_unknown_insufficient_data(self):
        """Test UNKNOWN when not enough data."""
        ep = Episode(market_id="test")
        ep.trades = []
        ep.total_trades = 0
        
        cls = classify_episode(ep)
        
        assert cls.label == "UNKNOWN" or cls.confidence < 0.3


class TestClassifyAll:
    """Tests for batch classification."""
    
    def test_empty_list(self):
        result = classify_all([])
        assert result == []
    
    def test_multiple_episodes(self):
        ts = datetime(2024, 12, 15, 10, 30, 0)
        
        ep1 = Episode(market_id="test1")
        ep1.trades = [make_trade(ts, "UP", "BUY", 0.9, 100)]
        ep1.net_up = 100
        ep1.total_trades = 1
        
        ep2 = Episode(market_id="test2")
        ep2.trades = [make_trade(ts, "DOWN", "BUY", 0.5, 100)]
        ep2.net_down = 100
        ep2.total_trades = 1
        
        results = classify_all([ep1, ep2])
        
        assert len(results) == 2
        assert all(isinstance(r[1], Classification) for r in results)


class TestLabelDistribution:
    """Tests for label distribution."""
    
    def test_empty(self):
        dist = compute_label_distribution([])
        assert dist['total'] == 0
    
    def test_counts(self):
        ts = datetime(2024, 12, 15, 10, 30, 0)
        
        episodes = []
        for i in range(3):
            ep = Episode(market_id=f"test{i}")
            ep.trades = [make_trade(ts)]
            ep.total_trades = 1
            episodes.append(ep)
        
        results = classify_all(episodes)
        dist = compute_label_distribution(results)
        
        assert dist['total'] == 3
        assert sum(dist['counts'].values()) == 3


