"""Check current account snapshot - portfolio vs cash"""
import sys
sys.path.insert(0, ".")

from mm_bot.config import Config, RunMode
from mm_bot.clob import ClobWrapper
from mm_bot.balance import BalanceManager

config = Config.from_env('pm_api_config.json')
config.mode = RunMode.LIVE
clob = ClobWrapper(config)

balance_mgr = BalanceManager(clob, config)
snap = balance_mgr.get_snapshot()

print("=" * 60)
print("ACCOUNT SNAPSHOT")
print("=" * 60)
print(f"Cash available (spendable USDC):  ${snap.cash_available_usdc:.2f}")
print(f"Locked in open buys:              ${snap.locked_usdc_in_open_buys:.2f}")
print(f"Positions MTM:                    ${snap.positions_mtm_usdc:.2f}")
print(f"Equity estimate (portfolio):      ${snap.equity_estimate_usdc:.2f}")
print(f"Safety buffer:                    ${snap.safety_buffer:.2f}")
print(f"Spendable (for new orders):       ${snap.spendable_usdc:.2f}")
print("=" * 60)
print()
print("EXPLANATION:")
print(f"  Portfolio (${snap.equity_estimate_usdc:.2f}) = Cash (${snap.cash_available_usdc:.2f}) + Positions (${snap.positions_mtm_usdc:.2f})")
print(f"  Spendable (${snap.spendable_usdc:.2f}) = Cash (${snap.cash_available_usdc:.2f}) - Locked (${snap.locked_usdc_in_open_buys:.2f}) - Buffer (${snap.safety_buffer:.2f})")
print()
print("The bot uses SPENDABLE for order sizing, NOT portfolio value!")

