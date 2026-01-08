"""
Wallet Decoder V2 - Infer trading edge from public activity

Key improvements over V1:
1. Full-set detection via HOLD_TO_SETTLEMENT (not just MERGE)
2. MAKER vs TAKER inference per trade
3. Pairing engine to detect full-set pairs within windows
4. Fee/rebate modeling
5. Quantified "best hypothesis" output
"""

__version__ = "2.0.0"


