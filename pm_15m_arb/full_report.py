"""
Full Report Generator

Generates a comprehensive report of all paper trading results
suitable for sharing with ChatGPT or other analysis.
"""

import json
from pathlib import Path
from datetime import datetime, timezone
from typing import List, Dict, Optional
from collections import defaultdict
import statistics


def load_jsonl(path: Path) -> List[dict]:
    """Load JSONL file."""
    data = []
    try:
        with open(path) as f:
            for line in f:
                if line.strip():
                    data.append(json.loads(line))
    except Exception as e:
        print(f"Error loading {path}: {e}")
    return data


def load_json(path: Path) -> Optional[dict]:
    """Load JSON file."""
    try:
        with open(path) as f:
            return json.load(f)
    except:
        return None


def analyze_v1_results(results_dir: Path) -> dict:
    """Analyze V1 (broken) results for comparison."""
    analysis = {
        "version": "V1 (unconstrained)",
        "windows": 0,
        "trades": 0,
        "hedged_windows": 0,
        "unhedged_windows": 0,
        "total_unhedged_exposure": 0,
        "issues": [],
    }
    
    # Find trade logs
    trade_files = list(results_dir.glob("trades_*.jsonl"))
    
    for tf in trade_files:
        events = load_jsonl(tf)
        
        trades = [e for e in events if e.get("event", "").startswith("TRADE")]
        window_ends = [e for e in events if e.get("event") == "WINDOW_END"]
        
        analysis["trades"] += len(trades)
        analysis["windows"] += len(window_ends)
        
        for we in window_ends:
            pos = we.get("position", {})
            q_up = pos.get("q_up", 0)
            q_down = pos.get("q_down", 0)
            
            if q_up > 0 and q_down > 0:
                analysis["hedged_windows"] += 1
            else:
                analysis["unhedged_windows"] += 1
                analysis["total_unhedged_exposure"] += pos.get("total_cost", 0)
                
                # Document the issue
                if q_up > 0 and q_down == 0:
                    analysis["issues"].append({
                        "window": we.get("slug"),
                        "problem": f"Bought {q_up} Up shares, 0 Down (unhedged)",
                        "exposure": pos.get("total_cost", 0),
                    })
                elif q_down > 0 and q_up == 0:
                    analysis["issues"].append({
                        "window": we.get("slug"),
                        "problem": f"Bought {q_down} Down shares, 0 Up (unhedged)",
                        "exposure": pos.get("total_cost", 0),
                    })
    
    return analysis


def analyze_v2_results(results_dir: Path) -> dict:
    """Analyze V2 (constrained) results."""
    analysis = {
        "version": "V2 (constrained)",
        "strategy": None,
        "windows": 0,
        "ticks_total": 0,
        
        # Maker stats
        "bids_posted": 0,
        "bids_filled_up": 0,
        "bids_filled_down": 0,
        "pairs_completed": 0,
        
        # Legging stats
        "entry_blocked_feasibility": 0,
        "entry_blocked_risk_cap": 0,
        "rescues_triggered": 0,
        
        # Outcomes
        "hedged_windows": 0,
        "unhedged_windows": 0,
        "total_guaranteed_profit": 0,
        "total_max_loss": 0,
        
        # Distributions
        "pair_costs": [],
        "guaranteed_profits": [],
        
        # Per-window details
        "window_details": [],
    }
    
    # Find result files
    result_files = list(results_dir.glob("results_v2_*.json"))
    
    for rf in result_files:
        data = load_json(rf)
        if not data:
            continue
        
        analysis["strategy"] = data.get("strategy")
        
        for r in data.get("results", []):
            analysis["windows"] += 1
            analysis["ticks_total"] += r.get("ticks_seen", 0)
            
            # Maker stats
            analysis["bids_posted"] += r.get("bids_posted", 0)
            analysis["bids_filled_up"] += r.get("bids_filled_up", 0)
            analysis["bids_filled_down"] += r.get("bids_filled_down", 0)
            analysis["pairs_completed"] += r.get("pairs_completed", 0)
            
            # Legging stats
            analysis["entry_blocked_feasibility"] += r.get("entry_blocked_by_feasibility", 0)
            analysis["entry_blocked_risk_cap"] += r.get("entry_blocked_by_risk_cap", 0)
            analysis["rescues_triggered"] += r.get("rescue_triggered", 0)
            
            # Outcomes
            if r.get("hedged"):
                analysis["hedged_windows"] += 1
                analysis["total_guaranteed_profit"] += r.get("guaranteed_profit", 0)
                
                if r.get("achieved_pair_cost", 0) > 0:
                    analysis["pair_costs"].append(r["achieved_pair_cost"])
                if r.get("guaranteed_profit", 0) > 0:
                    analysis["guaranteed_profits"].append(r["guaranteed_profit"])
            else:
                analysis["unhedged_windows"] += 1
                analysis["total_max_loss"] += r.get("max_loss", 0)
            
            analysis["window_details"].append({
                "slug": r.get("slug"),
                "hedged": r.get("hedged"),
                "pair_cost": r.get("achieved_pair_cost"),
                "gp": r.get("guaranteed_profit"),
                "max_loss": r.get("max_loss"),
                "pairs": r.get("pairs_completed", 0),
            })
    
    # Also check trade logs
    trade_files = list(results_dir.glob("trades_v2_*.jsonl"))
    
    for tf in trade_files:
        events = load_jsonl(tf)
        
        for e in events:
            if e.get("event") == "MAKER_FILL":
                # Log maker fills
                pass
            elif e.get("event") == "PAIR_COMPLETED":
                # Log pair completions
                pass
    
    return analysis


def generate_report():
    """Generate the full report."""
    print("="*80)
    print("POLYMARKET BTC 15-MIN UP/DOWN ARBITRAGE - COMPREHENSIVE REPORT")
    print("="*80)
    print(f"\nGenerated: {datetime.now(timezone.utc).isoformat()}")
    print("\n")
    
    # Analyze V1 results
    v1_dir = Path("pm_results")
    v1_analysis = None
    if v1_dir.exists():
        v1_analysis = analyze_v1_results(v1_dir)
    
    # Analyze V2 results
    v2_dir = Path("pm_results_v2")
    v2_analysis = None
    if v2_dir.exists():
        v2_analysis = analyze_v2_results(v2_dir)
    
    # Print V1 Analysis (the broken version)
    print("="*80)
    print("SECTION 1: V1 ANALYSIS (Unconstrained - BROKEN)")
    print("="*80)
    
    if v1_analysis and v1_analysis["windows"] > 0:
        print(f"\nWindows analyzed: {v1_analysis['windows']}")
        print(f"Total trades: {v1_analysis['trades']}")
        print(f"Hedged windows: {v1_analysis['hedged_windows']}")
        print(f"Unhedged windows: {v1_analysis['unhedged_windows']}")
        print(f"Total unhedged exposure: ${v1_analysis['total_unhedged_exposure']:.2f}")
        
        if v1_analysis["issues"]:
            print(f"\nISSUES FOUND ({len(v1_analysis['issues'])}):")
            for issue in v1_analysis["issues"][:5]:
                print(f"  - {issue['window']}: {issue['problem']}")
        
        print(f"\nVERDICT: V1 was BROKEN - accumulated unhedged directional exposure")
    else:
        print("\nNo V1 results found.")
    
    # Print V2 Analysis (the fixed version)
    print("\n")
    print("="*80)
    print("SECTION 2: V2 ANALYSIS (Constrained - FIXED)")
    print("="*80)
    
    if v2_analysis and v2_analysis["windows"] > 0:
        print(f"\nStrategy: {v2_analysis['strategy']}")
        print(f"Windows analyzed: {v2_analysis['windows']}")
        print(f"Total ticks processed: {v2_analysis['ticks_total']}")
        
        print(f"\n--- Hedge Success Rate ---")
        hedge_rate = v2_analysis['hedged_windows'] / v2_analysis['windows'] * 100 if v2_analysis['windows'] > 0 else 0
        print(f"Hedged windows: {v2_analysis['hedged_windows']} / {v2_analysis['windows']} ({hedge_rate:.1f}%)")
        print(f"Unhedged windows: {v2_analysis['unhedged_windows']}")
        
        if v2_analysis['strategy'] == 'M':
            print(f"\n--- Maker Stats ---")
            print(f"Bids posted: {v2_analysis['bids_posted']}")
            print(f"Up fills: {v2_analysis['bids_filled_up']}")
            print(f"Down fills: {v2_analysis['bids_filled_down']}")
            print(f"Pairs completed: {v2_analysis['pairs_completed']}")
            
            fill_rate_up = v2_analysis['bids_filled_up'] / max(1, v2_analysis['bids_posted']) * 100
            fill_rate_down = v2_analysis['bids_filled_down'] / max(1, v2_analysis['bids_posted']) * 100
            print(f"\nFill rates: Up={fill_rate_up:.1f}%, Down={fill_rate_down:.1f}%")
        else:
            print(f"\n--- Legging Stats ---")
            print(f"Entries blocked by feasibility: {v2_analysis['entry_blocked_feasibility']}")
            print(f"Entries blocked by risk cap: {v2_analysis['entry_blocked_risk_cap']}")
            print(f"Rescues triggered: {v2_analysis['rescues_triggered']}")
        
        print(f"\n--- P&L Summary ---")
        print(f"Total guaranteed profit: ${v2_analysis['total_guaranteed_profit']:.2f}")
        print(f"Total max loss (unhedged): ${v2_analysis['total_max_loss']:.2f}")
        print(f"Net expected P&L: ${v2_analysis['total_guaranteed_profit'] - v2_analysis['total_max_loss']:.2f}")
        
        if v2_analysis['pair_costs']:
            print(f"\n--- Pair Cost Distribution ---")
            print(f"Min: {min(v2_analysis['pair_costs']):.4f}")
            print(f"Max: {max(v2_analysis['pair_costs']):.4f}")
            print(f"Mean: {statistics.mean(v2_analysis['pair_costs']):.4f}")
            if len(v2_analysis['pair_costs']) > 1:
                print(f"Stdev: {statistics.stdev(v2_analysis['pair_costs']):.4f}")
        
        print(f"\n--- Per-Window Details ---")
        for wd in v2_analysis['window_details'][-10:]:
            status = "HEDGED" if wd['hedged'] else "UNHEDGED"
            if wd['hedged']:
                print(f"  {wd['slug']}: {status}, pairs={wd['pairs']}, cost={wd['pair_cost']:.4f}, GP=${wd['gp']:.2f}")
            else:
                print(f"  {wd['slug']}: {status}, max_loss=${wd['max_loss']:.2f}")
    else:
        print("\nNo V2 results found or no complete windows.")
    
    # Conclusions
    print("\n")
    print("="*80)
    print("SECTION 3: CONCLUSIONS")
    print("="*80)
    
    print("""
1. INSTANT ARBITRAGE (Variant A - Taker Full-Set):
   Status: DOES NOT EXIST
   Evidence: Ask sum consistently >= $1.00 (usually $1.01-$1.02)
   Reason: Market makers are efficient; no free lunch
   
2. LEGGING ARBITRAGE (Variant B - Original Implementation):
   Status: BROKEN / NOT ARBITRAGE
   Evidence: Accumulated 250 Up shares @ $0.02 with no hedge
   Problem: Buying cheap side without hedge path = directional bet, not arb
   
3. CONSTRAINED LEGGING (Variant L - V2 with rescue bounds):
   Status: REQUIRES MORE DATA
   Issue: Hedge feasibility gate blocks most entries when market trends
   Result: Either hedged (profit) or rescue triggered (bounded loss)
   
4. MAKER SPREAD CAPTURE (Variant M - Post bids on both sides):
   Status: THEORETICAL BEST CANDIDATE
   Approach: bid_up + bid_down <= 1 - edge_target
   Challenge: Need market to oscillate enough to fill both sides
   Fill Model: Conservative cross-through (ask <= bid)
   
5. REALISTIC ASSESSMENT:
   - Taker arb: Dead
   - Legging without constraints: Guaranteed to blow up
   - Legging with constraints: Works only if market mean-reverts
   - Maker spread capture: Best theoretical edge, but requires oscillation
   
6. RECOMMENDATIONS:
   a) Do NOT deploy any live capital until 200+ windows show positive edge
   b) Maker mode (Variant M) is the only viable path
   c) Success depends entirely on BTC intra-window volatility
   d) If fill_both_legs < 30% after 500 windows, kill the idea
""")
    
    # Data for ChatGPT
    print("\n")
    print("="*80)
    print("SECTION 4: RAW DATA FOR CHATGPT ANALYSIS")
    print("="*80)
    
    summary_data = {
        "report_timestamp": datetime.now(timezone.utc).isoformat(),
        "market": "Polymarket BTC 15-min Up/Down",
        "v1_analysis": v1_analysis,
        "v2_analysis": {k: v for k, v in (v2_analysis or {}).items() if k != "window_details"} if v2_analysis else None,
        "conclusions": {
            "instant_arb_exists": False,
            "legging_without_constraints_viable": False,
            "maker_spread_capture_viable": "UNKNOWN - NEED MORE DATA",
            "recommended_next_step": "Run Variant M for 200+ windows",
        }
    }
    
    print(f"\n```json")
    print(json.dumps(summary_data, indent=2, default=str))
    print(f"```")
    
    print("\n")
    print("="*80)
    print("END OF REPORT")
    print("="*80)
    
    # Save report
    report_path = Path("btc_15m_arb_report.json")
    with open(report_path, "w") as f:
        json.dump(summary_data, f, indent=2, default=str)
    print(f"\nReport saved to: {report_path}")
    
    return summary_data


if __name__ == "__main__":
    generate_report()

