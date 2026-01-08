#!/usr/bin/env python3
from bot.gamma import GammaClient

g = GammaClient()
m = g.discover_bucket_markets(locations=['london'])

jan3 = [x for x in m if x.target_date.day == 3]
print(f"Jan 3: {len(jan3)} markets")
for x in jan3:
    print(f"  {x.tmin_f:.0f}-{x.tmax_f:.0f}F closed={x.closed}")





