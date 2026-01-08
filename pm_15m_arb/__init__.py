"""
Polymarket BTC 15-Minute Up/Down Arbitrage Bot

Paper trading system for BTC 15-minute interval markets.
Implements Variant A (paired full-set arb) with optional B overlay.

Strategy:
- Variant A: PairCost = AskYES + AskNO + buffers <= 1 - min_edge
- Lock condition: SafeProfitNet = min(QtyYES, QtyNO) - (CostYES + CostNO) - buffers >= target_profit
"""

__version__ = "1.0.0"

