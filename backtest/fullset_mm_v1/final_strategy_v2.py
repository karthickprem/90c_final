"""
FINAL STRATEGY V2 - With Correct Fee Thresholds

Based on fee analysis:
- Full-set: combined cost <= 95c (need 5c+ edge to beat fees)
- Directional: 92c+ entry with reversal score <= -1 (need 92%+ win rate)
"""
from dataclasses import dataclass
from typing import List
from collections import defaultdict
import os
import csv
import sys

from .parse import find_window_ids, load_window_ticks
from .stream import merge_tick_streams
from .config import DEFAULT_BUY_DIR, DEFAULT_SELL_DIR


def polymarket_fee(price_cents: int, size_dollars: float) -> float:
    """Calculate Polymarket taker fee."""
    if price_cents <= 0 or price_cents >= 100:
        return 0
    p = price_cents / 100.0
    shares = size_dollars / (price_cents / 100.0)
    fee_per_share = 0.25 * (p * (1 - p)) ** 2
    return shares * fee_per_share


def compute_reversal_score(merged, idx, spike_side):
    """Compute reversal risk score."""
    tick = merged[idx]
    t = tick.elapsed_secs
    
    if spike_side == "UP":
        spike_price = tick.up_ask
        opp_price = tick.down_ask
        spread = tick.up_ask - tick.up_bid
    else:
        spike_price = tick.down_ask
        opp_price = tick.up_ask
        spread = tick.down_ask - tick.down_bid
    
    speed_5s = 0
    for j in range(idx - 1, -1, -1):
        if t - merged[j].elapsed_secs >= 5:
            if spike_side == "UP":
                speed_5s = (spike_price - merged[j].up_ask) / 5
            else:
                speed_5s = (spike_price - merged[j].down_ask) / 5
            break
    
    opp_trend = 0
    for j in range(idx - 1, -1, -1):
        if t - merged[j].elapsed_secs >= 5:
            if spike_side == "UP":
                opp_trend = opp_price - merged[j].down_ask
            else:
                opp_trend = opp_price - merged[j].up_ask
            break
    
    time_remaining = 900 - t
    combined = tick.up_ask + tick.down_ask
    
    score = 0
    if speed_5s > 5:
        score += 2
    elif speed_5s > 3:
        score += 1
    if opp_trend > 0:
        score += 2
    if spread > 3:
        score += 1
    if time_remaining > 300:
        score += 1
    if combined >= 100:
        score += 1
    
    if opp_trend < -5:
        score -= 2
    elif opp_trend < -2:
        score -= 1
    if combined < 98:
        score -= 1
    if spread < 2:
        score -= 1
    
    return score


@dataclass
class Trade:
    window_id: str
    trade_type: str
    side: str
    entry_price: int
    entry_time: float
    reversal_score: int
    won: bool
    gross_pnl: float
    fee: float
    net_pnl: float


def run_backtest(
    dir_entry_threshold: int = 92,
    dir_max_score: int = -1,
    fullset_max_cost: int = 95,  # CORRECTED: was 98
    size_per_trade: float = 10.0
) -> List[Trade]:
    """Run the optimized strategy."""
    buy_ids = set(find_window_ids(DEFAULT_BUY_DIR))
    sell_ids = set(find_window_ids(DEFAULT_SELL_DIR))
    common = sorted(buy_ids & sell_ids)
    
    sys.stdout.reconfigure(encoding='utf-8')
    
    print("=" * 70)
    print("FINAL STRATEGY V2 (Corrected Thresholds)")
    print("=" * 70)
    print(f"\nConfig:")
    print(f"  Directional: {dir_entry_threshold}c+ with score <= {dir_max_score}")
    print(f"  Full-set: combined <= {fullset_max_cost}c")
    print(f"  Size: ${size_per_trade}")
    print(f"\nProcessing {len(common)} windows...")
    
    all_trades = []
    
    for i, wid in enumerate(common):
        if i % 1000 == 0:
            print(f"  {i}/{len(common)}...")
        
        buy_ticks, sell_ticks = load_window_ticks(wid, DEFAULT_BUY_DIR, DEFAULT_SELL_DIR)
        if len(buy_ticks) < 20 or len(sell_ticks) < 20:
            continue
        
        merged = merge_tick_streams(buy_ticks, sell_ticks)
        if len(merged) < 20:
            continue
        
        final = merged[-1]
        
        did_fullset = False
        did_up = False
        did_down = False
        
        for idx, tick in enumerate(merged):
            t = tick.elapsed_secs
            if t < 10 or t > 570:
                continue
            
            # Full-set (only if edge is big enough)
            if not did_fullset:
                combined = tick.up_ask + tick.down_ask
                if combined <= fullset_max_cost:
                    edge = 100 - combined
                    gross = edge / 100 * size_per_trade * 2
                    up_fee = polymarket_fee(tick.up_ask, size_per_trade)
                    down_fee = polymarket_fee(tick.down_ask, size_per_trade)
                    fee = up_fee + down_fee
                    net = gross - fee
                    
                    # Only take if actually profitable!
                    if net > 0:
                        trade = Trade(
                            window_id=wid,
                            trade_type="fullset",
                            side="BOTH",
                            entry_price=combined,
                            entry_time=t,
                            reversal_score=0,
                            won=True,
                            gross_pnl=gross,
                            fee=fee,
                            net_pnl=net
                        )
                        all_trades.append(trade)
                        did_fullset = True
                        continue
            
            # Directional
            if not did_up and not did_fullset:
                if tick.up_ask >= dir_entry_threshold:
                    score = compute_reversal_score(merged, idx, "UP")
                    if score <= dir_max_score:
                        won = final.up_ask >= 97
                        if won:
                            gross = (100 - tick.up_ask) / 100 * size_per_trade
                        else:
                            gross = -tick.up_ask / 100 * size_per_trade
                        
                        fee = polymarket_fee(tick.up_ask, size_per_trade)
                        
                        trade = Trade(
                            window_id=wid,
                            trade_type="directional",
                            side="UP",
                            entry_price=tick.up_ask,
                            entry_time=t,
                            reversal_score=score,
                            won=won,
                            gross_pnl=gross,
                            fee=fee,
                            net_pnl=gross - fee
                        )
                        all_trades.append(trade)
                        did_up = True
            
            if not did_down and not did_fullset:
                if tick.down_ask >= dir_entry_threshold:
                    score = compute_reversal_score(merged, idx, "DOWN")
                    if score <= dir_max_score:
                        won = final.down_ask >= 97
                        if won:
                            gross = (100 - tick.down_ask) / 100 * size_per_trade
                        else:
                            gross = -tick.down_ask / 100 * size_per_trade
                        
                        fee = polymarket_fee(tick.down_ask, size_per_trade)
                        
                        trade = Trade(
                            window_id=wid,
                            trade_type="directional",
                            side="DOWN",
                            entry_price=tick.down_ask,
                            entry_time=t,
                            reversal_score=score,
                            won=won,
                            gross_pnl=gross,
                            fee=fee,
                            net_pnl=gross - fee
                        )
                        all_trades.append(trade)
                        did_down = True
    
    return all_trades


def analyze_trades(trades: List[Trade], days: int = 51):
    """Analyze results."""
    print("\n" + "=" * 70)
    print("BACKTEST RESULTS")
    print("=" * 70)
    
    if not trades:
        print("No trades!")
        return {}
    
    n = len(trades)
    wins = sum(1 for t in trades if t.won)
    gross = sum(t.gross_pnl for t in trades)
    fees = sum(t.fee for t in trades)
    net = sum(t.net_pnl for t in trades)
    
    print(f"\nTotal trades: {n}")
    print(f"Win/Loss: {wins}/{n-wins} ({wins/n*100:.1f}%)")
    print(f"\nGross PnL: ${gross:.2f}")
    print(f"Fees: ${fees:.2f}")
    print(f"NET PnL: ${net:.2f}")
    
    # By type
    by_type = defaultdict(lambda: {"n": 0, "wins": 0, "gross": 0, "fee": 0, "net": 0})
    for t in trades:
        by_type[t.trade_type]["n"] += 1
        if t.won:
            by_type[t.trade_type]["wins"] += 1
        by_type[t.trade_type]["gross"] += t.gross_pnl
        by_type[t.trade_type]["fee"] += t.fee
        by_type[t.trade_type]["net"] += t.net_pnl
    
    print("\n--- By Trade Type ---")
    for ttype in ["directional", "fullset"]:
        if ttype in by_type:
            s = by_type[ttype]
            wr = s["wins"] / s["n"] * 100 if s["n"] > 0 else 0
            print(f"{ttype}: {s['n']} trades, {wr:.1f}% win, net ${s['net']:.2f}")
    
    print(f"\n--- Projections ---")
    print(f"Period: {days} days")
    print(f"Trades/day: {n/days:.2f}")
    print(f"Net/day: ${net/days:.2f}")
    print(f"Net/month: ${net/days*30:.2f}")
    print(f"Net/year: ${net/days*365:.2f}")
    
    print(f"\n--- With Different Capital ---")
    for capital in [100, 500, 1000]:
        scale = capital / 10.0
        print(f"  ${capital}: ${net*scale/days*30:.2f}/month")
    
    return {"net": net, "trades": n}


def main():
    trades = run_backtest(
        dir_entry_threshold=92,
        dir_max_score=-1,
        fullset_max_cost=95,  # Need 5c+ edge
        size_per_trade=10.0
    )
    
    analyze_trades(trades, 51)
    
    # Save
    os.makedirs("out_final_v2", exist_ok=True)
    with open("out_final_v2/trades.csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["window_id", "type", "side", "entry", "time", "score", "won", "gross", "fee", "net"])
        for t in trades:
            writer.writerow([
                t.window_id, t.trade_type, t.side, t.entry_price, f"{t.entry_time:.1f}",
                t.reversal_score, t.won, f"{t.gross_pnl:.4f}", f"{t.fee:.4f}", f"{t.net_pnl:.4f}"
            ])
    
    print("\nSaved to out_final_v2/")
    
    print("\n" + "=" * 70)
    print("FINAL VERDICT")
    print("=" * 70)


if __name__ == "__main__":
    main()

