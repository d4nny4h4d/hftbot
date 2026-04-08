"""FastAPI REST server -- provides data endpoints for the Streamlit dashboard."""

import logging
import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.db.database import Database

logger = logging.getLogger(__name__)


def create_app(db_path: str = "data/hft_london_1pct.db", bot_name: str = "HFTBot") -> FastAPI:
    app = FastAPI(title=f"{bot_name} API", version="1.0.0")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    db = Database(db_path)

    @app.get("/api/status")
    def get_status():
        return {"status": "running", "bot_name": bot_name, "version": "1.0.0"}

    @app.get("/api/trades/open")
    def get_open_trades():
        return db.get_open_trades()

    @app.get("/api/trades/closed")
    def get_closed_trades(limit: int = 50):
        return db.get_closed_trades(limit)

    @app.get("/api/trades/today")
    def get_today_trades():
        return db.get_today_trades()

    @app.get("/api/performance")
    def get_performance():
        return db.get_performance_stats()

    @app.get("/api/daily-summaries")
    def get_daily_summaries(days: int = 30):
        return db.get_daily_summaries(days)

    @app.get("/api/equity-curve")
    def get_equity_curve(limit: int = 1000):
        return db.get_equity_curve(limit)

    return app


app = create_app()

if __name__ == "__main__":
    import argparse
    import uvicorn
    import yaml

    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config/bot1_london_1pct.yaml")
    args = parser.parse_args()

    with open(args.config) as f:
        cfg = yaml.safe_load(f)

    db_path = cfg["general"]["db_path"]
    port = cfg["general"].get("api_port", 8001)
    bot_name = cfg["general"].get("bot_name", "HFTBot")

    app = create_app(db_path=db_path, bot_name=bot_name)
    uvicorn.run(app, host="0.0.0.0", port=port)
