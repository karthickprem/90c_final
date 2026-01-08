"""
Classification - Assign strategy labels to episodes
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Literal, Tuple
from datetime import timedelta

from .enrich import Episode
from .config import (
    FULL_SET_MATCH_RATIO,
    FULL_SET_MIN_EDGE,
    MERGE_HORIZON_MINUTES,
    MM_MIN_TRADES_PER_DAY,
    MM_MAX_INVENTORY_RATIO,
)


StrategyLabel = Literal[
    "FULL_SET_ARB",
    "MARKET_MAKING", 
    "DIRECTIONAL_SPIKE",
    "HYBRID",
    "UNKNOWN",
]


@dataclass
class Classification:
    """Classification result for an episode."""
    label: StrategyLabel
    confidence: float  # 0..1
    reasons: List[str] = field(default_factory=list)
    scores: dict = field(default_factory=dict)  # Individual signal scores


def classify_episode(episode: Episode) -> Classification:
    """
    Classify a single episode.
    
    Returns classification with label, confidence, and evidence.
    """
    scores = {
        'full_set': 0.0,
        'market_making': 0.0,
        'directional': 0.0,
    }
    reasons = []
    
    # =========================================================================
    # FULL_SET_ARB indicators
    # =========================================================================
    
    # 1. Match ratio: matched_shares / max(net_up, net_down)
    max_net = max(abs(episode.net_up), abs(episode.net_down), 0.001)
    match_ratio = episode.matched_shares / max_net if max_net > 0 else 0
    
    if match_ratio >= FULL_SET_MATCH_RATIO:
        scores['full_set'] += 0.4
        reasons.append(f"match_ratio={match_ratio:.2f} >= {FULL_SET_MATCH_RATIO}")
    
    # 2. Edge: 1 - avg_cost_matched
    edge = 1.0 - episode.avg_cost_matched if episode.avg_cost_matched > 0 else 0
    
    if edge >= FULL_SET_MIN_EDGE and episode.matched_shares > 0:
        scores['full_set'] += 0.3
        reasons.append(f"edge={edge:.4f} (${edge*100:.2f}c per share)")
    
    # 3. MERGE within horizon
    if episode.has_merge:
        scores['full_set'] += 0.2
        reasons.append("has_merge=True")
        
        if episode.merge_delay_s is not None:
            horizon_secs = MERGE_HORIZON_MINUTES * 60
            if episode.merge_delay_s <= horizon_secs:
                scores['full_set'] += 0.1
                reasons.append(f"merge_delay={episode.merge_delay_s:.0f}s <= {horizon_secs}s")
    
    # =========================================================================
    # MARKET_MAKING indicators
    # =========================================================================
    
    # 1. High trade count
    if episode.total_trades >= 20:
        scores['market_making'] += 0.2
        reasons.append(f"high_trade_count={episode.total_trades}")
    
    # 2. Both sides traded
    has_both_sides = (
        (episode.total_up_bought > 0 or episode.total_up_sold > 0) and
        (episode.total_down_bought > 0 or episode.total_down_sold > 0)
    )
    
    if has_both_sides:
        scores['market_making'] += 0.1
    
    # 3. Frequent alternation (check meta for maker flags if available)
    maker_count = 0
    for t in episode.trades:
        is_maker = t.meta.get("isMaker") or t.meta.get("is_maker") or t.meta.get("maker")
        if is_maker:
            maker_count += 1
    
    maker_ratio = maker_count / episode.total_trades if episode.total_trades > 0 else 0
    if maker_ratio > 0.5:
        scores['market_making'] += 0.2
        reasons.append(f"maker_ratio={maker_ratio:.2f}")
    
    # 4. Low net inventory at end
    total_position = abs(episode.net_up) + abs(episode.net_down)
    total_volume = (
        episode.total_up_bought + episode.total_up_sold + 
        episode.total_down_bought + episode.total_down_sold
    )
    
    if total_volume > 0:
        inventory_ratio = total_position / total_volume
        if inventory_ratio < MM_MAX_INVENTORY_RATIO:
            scores['market_making'] += 0.2
            reasons.append(f"low_inventory_ratio={inventory_ratio:.2f}")
    
    # =========================================================================
    # DIRECTIONAL_SPIKE indicators
    # =========================================================================
    
    # 1. Large net exposure one side
    if episode.net_up > 0 and episode.net_down <= 0:
        scores['directional'] += 0.3
        reasons.append(f"directional_UP: net_up={episode.net_up:.2f}")
    elif episode.net_down > 0 and episode.net_up <= 0:
        scores['directional'] += 0.3
        reasons.append(f"directional_DOWN: net_down={episode.net_down:.2f}")
    
    # 2. No merges, resolves via redeem
    if episode.has_redeem and not episode.has_merge:
        scores['directional'] += 0.2
        reasons.append("redeem_without_merge")
    
    # 3. Few trades, large sizes
    if episode.total_trades <= 5 and total_volume > 100:
        scores['directional'] += 0.2
        reasons.append("few_large_trades")
    
    # =========================================================================
    # Determine final label
    # =========================================================================
    
    max_score = max(scores.values())
    
    if max_score < 0.2:
        label = "UNKNOWN"
        confidence = 0.0
    elif scores['full_set'] >= max_score and scores['full_set'] >= 0.3:
        label = "FULL_SET_ARB"
        confidence = min(1.0, scores['full_set'])
    elif scores['market_making'] >= max_score and scores['market_making'] >= 0.3:
        label = "MARKET_MAKING"
        confidence = min(1.0, scores['market_making'])
    elif scores['directional'] >= max_score and scores['directional'] >= 0.3:
        label = "DIRECTIONAL_SPIKE"
        confidence = min(1.0, scores['directional'])
    else:
        # Multiple signals - hybrid
        label = "HYBRID"
        confidence = max_score
    
    return Classification(
        label=label,
        confidence=confidence,
        reasons=reasons[:5],  # Top 5 reasons
        scores=scores,
    )


def classify_all(episodes: List[Episode]) -> List[Tuple[Episode, Classification]]:
    """Classify all episodes."""
    results = []
    
    for ep in episodes:
        classification = classify_episode(ep)
        results.append((ep, classification))
    
    return results


def compute_label_distribution(classifications: List[Tuple[Episode, Classification]]) -> dict:
    """Compute distribution of labels."""
    counts = {
        "FULL_SET_ARB": 0,
        "MARKET_MAKING": 0,
        "DIRECTIONAL_SPIKE": 0,
        "HYBRID": 0,
        "UNKNOWN": 0,
    }
    
    for _, cls in classifications:
        counts[cls.label] += 1
    
    total = len(classifications)
    
    return {
        'counts': counts,
        'percentages': {k: v/total*100 if total > 0 else 0 for k, v in counts.items()},
        'total': total,
    }


def get_dominant_strategy(classifications: List[Tuple[Episode, Classification]]) -> str:
    """Get the dominant strategy label."""
    dist = compute_label_distribution(classifications)
    
    max_label = max(dist['counts'].items(), key=lambda x: x[1])[0]
    max_pct = dist['percentages'][max_label]
    
    return f"{max_label} ({max_pct:.1f}%)"


