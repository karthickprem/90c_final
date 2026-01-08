"""
Data API Client V2 (same as V1, with config updates)
"""

from __future__ import annotations

import json
import time
import os
from pathlib import Path
from typing import List, Dict, Any, Optional
from datetime import datetime

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .config import (
    TRADES_ENDPOINT,
    ACTIVITY_ENDPOINT,
    DEFAULT_TIMEOUT,
    MAX_RETRIES,
    RETRY_BACKOFF_BASE,
    DecoderV2Config,
)


def create_session() -> requests.Session:
    """Create session with retry strategy."""
    session = requests.Session()
    
    retry_strategy = Retry(
        total=MAX_RETRIES,
        backoff_factor=RETRY_BACKOFF_BASE,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    
    session.headers.update({
        "User-Agent": "WalletDecoderV2/2.0",
        "Accept": "application/json",
    })
    
    return session


class DataAPIClient:
    """Polymarket Data API client (READ-ONLY)."""
    
    def __init__(self, config: DecoderV2Config):
        self.config = config
        self.session = create_session()
        self.raw_dir = Path(config.outdir) / "raw"
        self.raw_dir.mkdir(parents=True, exist_ok=True)
    
    def _fetch_paginated(
        self,
        endpoint: str,
        name: str,
        extra_params: Optional[Dict] = None,
    ) -> List[Dict[str, Any]]:
        """Fetch all pages from an endpoint."""
        all_records = []
        offset = 0
        page = 0
        
        while page < self.config.max_pages:
            params = {
                "user": self.config.user_address,
                "limit": self.config.limit,
                "offset": offset,
            }
            
            if extra_params:
                params.update(extra_params)
            
            if self.config.start_date:
                params["startDate"] = self.config.start_date.isoformat()
            if self.config.end_date:
                params["endDate"] = self.config.end_date.isoformat()
            
            if self.config.verbose:
                print(f"  Fetching {name} offset={offset}...")
            
            try:
                resp = self.session.get(
                    endpoint,
                    params=params,
                    timeout=DEFAULT_TIMEOUT,
                )
                resp.raise_for_status()
                data = resp.json()
            except requests.RequestException as e:
                print(f"  ERROR fetching {name} at offset {offset}: {e}")
                break
            
            if isinstance(data, list):
                records = data
            elif isinstance(data, dict):
                records = data.get("data", data.get("results", data.get("trades", data.get("activity", []))))
                if isinstance(records, dict):
                    records = [records]
            else:
                records = []
            
            page_file = self.raw_dir / f"{name}_offset_{offset}.json"
            with open(page_file, "w") as f:
                json.dump(records, f, indent=2, default=str)
            
            if not records:
                if self.config.verbose:
                    print(f"  {name}: No more records at offset {offset}")
                break
            
            all_records.extend(records)
            
            if self.config.verbose:
                print(f"  {name}: Got {len(records)} records (total: {len(all_records)})")
            
            if len(records) < self.config.limit:
                break
            
            offset += self.config.limit
            page += 1
            time.sleep(0.1)
        
        return all_records
    
    def fetch_trades(self) -> List[Dict[str, Any]]:
        """Fetch all trades for the user."""
        print(f"Fetching trades for {self.config.user_address}...")
        return self._fetch_paginated(TRADES_ENDPOINT, "trades")
    
    def fetch_activity(self) -> List[Dict[str, Any]]:
        """Fetch all activity for the user."""
        print(f"Fetching activity for {self.config.user_address}...")
        return self._fetch_paginated(ACTIVITY_ENDPOINT, "activity")
    
    def fetch_all(self) -> tuple[List[Dict], List[Dict]]:
        """Fetch both trades and activity."""
        trades = self.fetch_trades()
        activity = self.fetch_activity()
        print(f"\nTotal: {len(trades)} trades, {len(activity)} activity events")
        return trades, activity


