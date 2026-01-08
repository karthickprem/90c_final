import time
import requests
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"

# --- Tuning knobs ---
DEFAULT_LIMIT = 100
SLEEP_SECS = 3.0

# Safety buffer for slippage / stale quotes (in probability points, e.g. 0.003 = 0.3%)
EDGE_BUFFER = 0.003

# If Polymarket fees are non-zero for takers, include them here.
# Docs show fee schedule "subject to change" and currently shows 0 bps in that table,
# but do NOT assume it stays 0. Keep this configurable. :contentReference[oaicite:3]{index=3}
TAKER_FEE_BPS = 0.0  # example: 5 = 5 bps


@dataclass
class BestAsk:
    price: float
    size: float


def http_get(url: str, params: Optional[dict] = None, timeout: float = 10.0):
    r = requests.get(url, params=params, timeout=timeout)
    r.raise_for_status()
    return r.json()


def gamma_list_events(limit=DEFAULT_LIMIT, offset=0, closed=False, active=True) -> List[dict]:
    # Gamma docs recommend scanning via /events because events contain their markets :contentReference[oaicite:4]{index=4}
    params = {
        "order": "id",
        "ascending": "false",
        "closed": str(closed).lower(),
        "active": str(active).lower(),
        "limit": limit,
        "offset": offset,
    }
    return http_get(f"{GAMMA}/events", params=params)


def clob_best_ask(token_id: str) -> Optional[BestAsk]:
    # CLOB orderbook endpoint :contentReference[oaicite:5]{index=5}
    book = http_get(f"{CLOB}/book", params={"token_id": token_id})
    asks = book.get("asks") or []
    if not asks:
        return None
    best = asks[0]
    return BestAsk(price=float(best["price"]), size=float(best["size"]))


def get_yes_token_id(market: dict) -> Optional[str]:
    """
    Gamma market has a pair of token IDs (Yes/No). :contentReference[oaicite:6]{index=6}
    In most Polymarket responses, clobTokenIds is the Yes/No pair.
    We'll default to index 0 as "Yes".
    """
    tids = market.get("clobTokenIds") or market.get("clob_token_ids")  # handle both casings
    if not tids or len(tids) < 1:
        return None
    return str(tids[0])


def is_moneyline_market(m: dict) -> bool:
    # Gamma schema includes sportsMarketType :contentReference[oaicite:7]{index=7}
    smt = (m.get("sportsMarketType") or "").lower()
    return smt == "moneyline"


def find_moneyline_triplets(event: dict) -> List[List[dict]]:
    """
    Polymarket sports events often contain 3 moneyline markets:
      - Team A wins (Yes/No)
      - Draw (Yes/No)
      - Team B wins (Yes/No)
    We group moneyline markets in the same event; if exactly 3, treat as a triplet.
    """
    markets = event.get("markets") or []
    ml = [m for m in markets if is_moneyline_market(m) and (m.get("enableOrderBook", True) is True)]
    if len(ml) == 3:
        return [ml]
    return []


def implied_complete_set_cost(asks: List[BestAsk]) -> Tuple[float, float]:
    """
    Cost to buy 1 share of each outcome at best ask levels.
    Returns (sum_price, min_size).
    min_size = the max size you can do at top-of-book across all legs.
    """
    sum_price = sum(a.price for a in asks)
    min_size = min(a.size for a in asks)
    return sum_price, min_size


def fee_fraction_from_bps(bps: float) -> float:
    return bps / 10000.0


def scan_once(max_pages: int = 3):
    """
    Scans newest events first, looking for 3-way moneyline complete-set arbitrage.
    """
    opps = []
    offset = 0
    for _ in range(max_pages):
        events = gamma_list_events(limit=DEFAULT_LIMIT, offset=offset, closed=False, active=True)
        if not events:
            break

        for ev in events:
            triplets = find_moneyline_triplets(ev)
            if not triplets:
                continue

            for group in triplets:
                legs = []
                leg_info = []
                ok = True

                for m in group:
                    yes_tid = get_yes_token_id(m)
                    if not yes_tid:
                        ok = False
                        break

                    ba = clob_best_ask(yes_tid)
                    if not ba:
                        ok = False
                        break

                    legs.append(ba)
                    leg_info.append({
                        "question": m.get("question"),
                        "slug": m.get("slug"),
                        "yes_token_id": yes_tid,
                        "best_ask": ba.price,
                        "ask_size": ba.size,
                    })

                if not ok or len(legs) != 3:
                    continue

                sum_price, max_size_top = implied_complete_set_cost(legs)

                # conservative check: include fee + buffer
                fee = fee_fraction_from_bps(TAKER_FEE_BPS)
                threshold = 1.0 - EDGE_BUFFER - fee

                if sum_price < threshold:
                    opps.append({
                        "event": ev.get("title") or ev.get("slug") or ev.get("id"),
                        "sum_price": sum_price,
                        "edge": (1.0 - sum_price),
                        "top_size": max_size_top,
                        "legs": leg_info,
                    })

        offset += DEFAULT_LIMIT

    return opps


def main():
    while True:
        try:
            opps = scan_once(max_pages=3)
            if opps:
                print("\n=== ARB OPPORTUNITIES FOUND ===")
                for o in opps[:10]:
                    print(f"\nEvent: {o['event']}")
                    print(f"Complete-set cost: {o['sum_price']:.4f}  | Edge: {o['edge']:.4f}")
                    print(f"Top-of-book max size (per leg): {o['top_size']:.2f}")
                    for leg in o["legs"]:
                        print(f"  - {leg['question']} | ask={leg['best_ask']:.4f} size={leg['ask_size']:.2f}")
                        print(f"    slug={leg['slug']} yes_token_id={leg['yes_token_id']}")
            else:
                print("No obvious complete-set arbs in scanned pages.")
        except Exception as e:
            print(f"Scan error: {e}")

        time.sleep(SLEEP_SECS)


if __name__ == "__main__":
    main()
