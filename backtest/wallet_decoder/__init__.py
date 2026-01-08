"""
Wallet Decoder - Analyze Polymarket wallet strategies

Decodes a wallet's trading activity to classify strategy type:
- FULL_SET_ARB: Complete-set arbitrage with MERGE
- MARKET_MAKING: High-frequency, both sides, flat inventory
- DIRECTIONAL_SPIKE: One-sided bets, holds to settlement
- HYBRID: Mixed strategies
"""

__version__ = "1.0.0"


