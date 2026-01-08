"""
MM Bot Dry Run
===============
Prints best bid/ask, computed quotes, and what would be posted.
No real orders are placed.
"""

import sys
import os

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mm_bot.config import Config, RunMode
from mm_bot.runner import MMRunner


def main():
    import argparse
    
    parser = argparse.ArgumentParser(description="MM Bot Dry Run")
    parser.add_argument("--seconds", type=float, default=30, help="Run duration (default 30)")
    parser.add_argument("--config", default="pm_api_config.json", help="Config file")
    args = parser.parse_args()
    
    # Force DRYRUN mode
    os.environ["LIVE"] = "0"
    os.environ["PAPER"] = "0"
    
    config = Config.from_env(args.config)
    config.mode = RunMode.DRYRUN  # Force dryrun
    config.verbose = True
    
    print("=" * 60)
    print("MM BOT DRY RUN")
    print("=" * 60)
    print("This will NOT place any real orders.")
    print("It shows what the bot WOULD do.")
    print("=" * 60)
    print()
    
    runner = MMRunner(config)
    runner.run(duration_seconds=args.seconds)


if __name__ == "__main__":
    main()

