"""Run Bot 1: London Session, 1% Risk -- BB Mean Reversion."""

import os
import sys
import traceback
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
os.chdir(PROJECT_ROOT)
sys.path.insert(0, str(PROJECT_ROOT))

if __name__ == "__main__":
    try:
        from src.main import HFTBot

        bot = HFTBot(
            config_path="config/bot1_london_1pct.yaml",
            env_path=".env.bot1",
        )
        bot.start()
    except Exception:
        traceback.print_exc()
    finally:
        input("\nPress Enter to exit...")
