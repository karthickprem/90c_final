"""
Check Last Completion - Offline validator for PAIR_COMPLETE events

Finds the latest trades log, parses PAIR_COMPLETE events, and validates:
- invariant_ok (p_comp <= completion_cap)
- edge_locked >= 0 for completed pairs
- Rescue mode behavior

Usage:
    python scripts/check_last_completion.py
"""

import json
from pathlib import Path
from typing import List, Dict, Optional


def find_latest_log(specific_file: str = None) -> Optional[Path]:
    """Find the most recent trades_v4_*.jsonl file with completions."""
    if specific_file:
        return Path(specific_file)
    
    results_dir = Path("pm_results_v4")
    if not results_dir.exists():
        print(f"ERROR: {results_dir} does not exist")
        return None
    
    logs = sorted(results_dir.glob("trades_v4_*.jsonl"), reverse=True)
    if not logs:
        print(f"ERROR: No trades_v4_*.jsonl files found in {results_dir}")
        return None
    
    # Find the first log with completions
    for log in logs:
        try:
            with open(log, 'r') as f:
                content = f.read()
                if "PAIR_COMPLETE" in content:
                    return log
        except:
            pass
    
    # If no completions, return newest non-empty
    for log in logs:
        try:
            if log.stat().st_size > 100:
                return log
        except:
            pass
    
    return logs[0] if logs else None


def parse_log(log_path: Path, max_lines: int = 500) -> List[dict]:
    """Parse the last N lines of a JSONL log file."""
    events = []
    
    with open(log_path, 'r') as f:
        lines = f.readlines()
    
    # Take last N lines
    lines = lines[-max_lines:]
    
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    
    return events


def analyze_completions(events: List[dict]) -> Dict:
    """Analyze PAIR_COMPLETE events."""
    completions = {"L": [], "M": [], "Q": []}
    rescues = []
    first_leg_fills = {"L": [], "M": [], "Q": []}
    
    for e in events:
        event_type = e.get("event", "")
        
        if event_type == "PAIR_COMPLETE":
            model = e.get("model", "?")
            if model in completions:
                completions[model].append(e)
        
        if event_type in ("RESCUE_MODE_ENTER", "RESCUE_MODE"):
            rescues.append(e)
        
        if event_type == "FIRST_LEG_FILL":
            model = e.get("model", "?")
            if model in first_leg_fills:
                first_leg_fills[model].append(e)
    
    return {
        "completions": completions,
        "rescues": rescues,
        "first_leg_fills": first_leg_fills,
    }


def validate_completion(c: dict) -> dict:
    """Validate a single completion event."""
    model = c.get("model", "?")
    window_mode = c.get("window_mode", "unknown")  # May be missing in old logs
    
    # New detailed fields (may be missing in old logs)
    p_first = c.get("p_first", None)
    p_comp = c.get("p_comp", None)
    q_matched = c.get("q_matched", None)
    cost_total = c.get("cost_total", None)
    payout_locked = c.get("payout_locked", None)
    edge_locked = c.get("edge_locked", None)
    pair_cost = c.get("pair_cost", None)
    
    completion_cap_normal = c.get("completion_cap_normal", None)
    completion_cap_used = c.get("completion_cap_used", None)
    invariant_ok = c.get("invariant_ok", None)
    
    # Old format only has edge_net
    edge_net = c.get("edge_net", None)
    
    # Use old format if new fields missing
    if edge_locked is None and edge_net is not None:
        edge_locked = edge_net
    
    # Recompute to verify (only if we have the data)
    recomputed_edge = None
    recomputed_pair_cost = None
    recomputed_invariant = None
    
    if payout_locked is not None and cost_total is not None:
        recomputed_edge = payout_locked - cost_total
    if p_first is not None and p_comp is not None:
        recomputed_pair_cost = p_first + p_comp
    if p_comp is not None and completion_cap_used is not None:
        recomputed_invariant = p_comp <= completion_cap_used + 1e-9
    
    return {
        "model": model,
        "window_mode": window_mode,
        "p_first": p_first,
        "p_comp": p_comp,
        "completion_cap_normal": completion_cap_normal,
        "completion_cap_used": completion_cap_used,
        "invariant_ok": invariant_ok,
        "recomputed_invariant": recomputed_invariant,
        "q_matched": q_matched,
        "cost_total": cost_total,
        "payout_locked": payout_locked,
        "edge_locked": edge_locked,
        "recomputed_edge": recomputed_edge,
        "pair_cost": pair_cost,
        "recomputed_pair_cost": recomputed_pair_cost,
        "has_detailed_fields": p_first is not None,
    }


def print_report(analysis: dict, validations: dict):
    """Print a formatted report."""
    print("\n" + "="*70)
    print("COMPLETION VALIDATION REPORT")
    print("="*70)
    
    # First leg fills (new detailed logging)
    first_leg = analysis.get("first_leg_fills", {})
    for model in ["L", "M", "Q"]:
        fills = first_leg.get(model, [])
        if fills:
            print(f"\n--- First Leg Fills (Model {model}): {len(fills)} ---")
            for f in fills[-3:]:
                side = f.get("side", "?")
                price = f.get("price", 0)
                max_comp = f.get("max_completion_at_edge")
                other_ask = f.get("other_side_ask")
                can_complete = f.get("can_complete_at_edge", False)
                max_comp_str = f"{max_comp:.4f}" if max_comp is not None else "N/A"
                other_ask_str = f"{other_ask:.4f}" if other_ask is not None else "N/A"
                print(f"  {side} @ {price:.4f}, max_comp={max_comp_str}, other_ask={other_ask_str}, can_complete={can_complete}")
    
    # Rescue events
    rescues = analysis.get("rescues", [])
    print(f"\n--- Rescue events: {len(rescues)} ---")
    for r in rescues[-3:]:  # Last 3
        reason = r.get("reason", "?")
        max_p = r.get("max_completion_price", 0)
        ask = r.get("other_best_ask", 0)
        print(f"  - {reason}")
        if max_p:
            print(f"    max_completion={max_p:.4f}, best_ask={ask:.4f}")
    
    # Per-model completions
    for model in ["L", "M", "Q"]:
        vals = validations.get(model, [])
        print(f"\n--- Model {model} ({len(vals)} completions) ---")
        
        if not vals:
            print("  No completions")
            continue
        
        # Show last 3
        for v in vals[-3:]:
            has_detail = v.get("has_detailed_fields", False)
            
            if has_detail:
                ok = "OK" if v["invariant_ok"] and v["recomputed_invariant"] else "FAIL"
                print(f"\n  [{ok}] mode={v['window_mode']}")
                print(f"    p_first={v['p_first']:.4f}, p_comp={v['p_comp']:.4f}")
                print(f"    completion_cap_used={v['completion_cap_used']:.4f}")
                print(f"    invariant_ok={v['invariant_ok']} (recomputed={v['recomputed_invariant']})")
                print(f"    q_matched={v['q_matched']:.2f}")
                print(f"    cost_total={v['cost_total']:.4f}, payout={v['payout_locked']:.4f}")
                print(f"    edge_locked={v['edge_locked']:.4f} (recomputed={v['recomputed_edge']:.4f})")
                print(f"    pair_cost={v['pair_cost']:.4f}")
            else:
                # Old format - just show edge_net
                edge = v.get("edge_locked")
                edge_str = f"{edge:.4f}" if edge is not None else "N/A"
                print(f"\n  [OLD FORMAT] edge_locked={edge_str}")
        
        # Summary
        vals_with_invariant = [v for v in vals if v["invariant_ok"] is not None]
        if vals_with_invariant:
            all_invariant_ok = all(v["invariant_ok"] and (v["recomputed_invariant"] if v["recomputed_invariant"] is not None else True) for v in vals_with_invariant)
        else:
            all_invariant_ok = "N/A (old log format)"
        
        vals_with_edge = [v for v in vals if v["edge_locked"] is not None]
        positive_edges = sum(1 for v in vals_with_edge if v["edge_locked"] > 1e-9)
        zero_edges = sum(1 for v in vals_with_edge if abs(v["edge_locked"]) < 1e-9)
        negative_edges = sum(1 for v in vals_with_edge if v["edge_locked"] < -1e-9)
        
        print(f"\n  Summary:")
        print(f"    All invariants OK: {all_invariant_ok}")
        print(f"    Positive edge: {positive_edges}")
        print(f"    Zero edge: {zero_edges}")
        print(f"    Negative edge (BUG!): {negative_edges}")
    
    # Overall verdict
    print("\n" + "="*70)
    print("VERDICT")
    print("="*70)
    
    all_Q = validations.get("Q", [])
    if not all_Q:
        print("No Model Q completions to analyze.")
    else:
        # Check for invariant violations (only for entries with new format)
        vals_with_invariant = [v for v in all_Q if v["invariant_ok"] is not None]
        invariant_fails = sum(1 for v in vals_with_invariant if v["invariant_ok"] is False)
        
        vals_with_edge = [v for v in all_Q if v["edge_locked"] is not None]
        negative = sum(1 for v in vals_with_edge if v["edge_locked"] < -1e-9)
        
        if invariant_fails > 0 or negative > 0:
            print(f"BUG DETECTED: {invariant_fails} invariant violations, {negative} negative edges")
        elif vals_with_edge:
            zero_pct = sum(1 for v in vals_with_edge if abs(v["edge_locked"]) < 1e-9) / len(vals_with_edge) * 100
            print(f"No bugs. {zero_pct:.0f}% of completions have zero edge (rescue mode).")
            
            if zero_pct > 50:
                print("NOTE: High rescue rate means market is too lopsided for positive-edge completion.")
                print("      The strategy correctly identified it couldn't complete at edge_floor=0.5%")
                print("      and switched to break-even rescue mode.")
        else:
            print("No edge data available (old log format).")


def main():
    log_path = find_latest_log()
    if not log_path:
        return
    
    print(f"Analyzing: {log_path}")
    
    events = parse_log(log_path)
    print(f"Parsed {len(events)} events")
    
    analysis = analyze_completions(events)
    
    # Validate each completion
    validations = {}
    for model, completions in analysis["completions"].items():
        validations[model] = [validate_completion(c) for c in completions]
    
    print_report(analysis, validations)


if __name__ == "__main__":
    main()

