#!/usr/bin/env python3
"""Debug the price inversion bug."""

from bot.gamma import GammaClient
from bot.clob import CLOBClient

g = GammaClient()
m = g.discover_bucket_markets(locations=['london'])

# Get the suspicious bucket
jan3_36 = [x for x in m if x.target_date.day == 3 and x.tmin_f > 35 and x.tmin_f < 38 and not x.is_tail_bucket][0]

print(f"Bucket: {jan3_36.tmin_f:.1f}-{jan3_36.tmax_f:.1f}F")
print(f"Question: {jan3_36.question}")
print(f"YES Token: {jan3_36.yes_token_id[:40]}...")
print()

clob = CLOBClient()

# Get book
print("Getting orderbook...")
book = clob.get_book(jan3_36.yes_token_id)

print(f"best_ask: {book.best_ask}")
print(f"best_bid: {book.best_bid}")
print()

print("All asks (first 5):")
for a in book.asks[:5]:
    print(f"  {a.price:.4f} x {a.size:.2f}")

print()
print("All bids (first 5):")
for b in book.bids[:5]:
    print(f"  {b.price:.4f} x {b.size:.2f}")

print()
print("Fill cost for 10 shares:")
fill = clob.fill_cost_for_shares(jan3_36.yes_token_id, 10)
print(f"  avg_price: {fill.avg_price:.4f}")
print(f"  total_cost: {fill.total_cost:.4f}")
print(f"  can_fill: {fill.can_fill}")





