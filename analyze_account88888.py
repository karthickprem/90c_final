"""
Analyze Account88888 trading activity.
"""

import requests
from datetime import datetime
from collections import defaultdict

# Account88888 wallet address
WALLET = '0x8c74b4eef9a894433B8126aA11d1345efb2B0488'

print('=' * 80)
print('  ACCOUNT88888 TRADING ACTIVITY ANALYSIS')
print('=' * 80)
print(f'  Wallet: {WALLET}')
print(f'  Analysis Time: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}')
print('=' * 80)
print()

# Fetch trades
r = requests.get('https://data-api.polymarket.com/trades', params={
    'user': WALLET,
    'limit': 200
}, timeout=30)

trades = r.json()
print(f'Total trades fetched: {len(trades)}')
print()

# Group by market
markets = defaultdict(list)
for t in trades:
    title = t.get('title', 'Unknown')
    markets[title].append(t)

# Analyze each market
total_buys = 0
total_sells = 0
total_buy_cost = 0
total_sell_revenue = 0

print('=' * 80)
print('  TRADES BY MARKET (Most Recent First)')
print('=' * 80)

for title, trades_list in sorted(markets.items(), key=lambda x: max(t.get('timestamp', 0) for t in x[1]), reverse=True):
    print(f'\nMARKET: {title}')
    print('-' * 80)
    
    # Group by outcome within market
    outcomes = defaultdict(list)
    for t in trades_list:
        outcome = t.get('outcome', 'Unknown')
        outcomes[outcome].append(t)
    
    market_summary = []
    
    for outcome, outcome_trades in outcomes.items():
        buys = []
        sells = []
        
        for t in sorted(outcome_trades, key=lambda x: x.get('timestamp', 0)):
            ts = int(t.get('timestamp', 0))
            dt = datetime.fromtimestamp(ts)
            side = t.get('side', '').upper()
            size = float(t.get('size', 0))
            price = float(t.get('price', 0))
            cost = size * price
            
            print(f'  {dt.strftime("%H:%M:%S")} | {side:4} | {outcome:6} | {size:7.2f} @ {price:.4f} | ${cost:7.2f}')
            
            if side == 'BUY':
                buys.append({'size': size, 'price': price, 'cost': cost})
                total_buys += 1
                total_buy_cost += cost
            else:
                sells.append({'size': size, 'price': price, 'revenue': cost})
                total_sells += 1
                total_sell_revenue += cost
        
        buy_shares = sum(b['size'] for b in buys)
        buy_cost = sum(b['cost'] for b in buys)
        sell_shares = sum(s['size'] for s in sells)
        sell_revenue = sum(s['revenue'] for s in sells)
        
        if buy_shares > 0 or sell_shares > 0:
            market_summary.append({
                'outcome': outcome,
                'buy_shares': buy_shares,
                'buy_cost': buy_cost,
                'sell_shares': sell_shares,
                'sell_revenue': sell_revenue
            })
    
    # Print market summary
    print()
    for s in market_summary:
        net_shares = s['buy_shares'] - s['sell_shares']
        avg_price = s['buy_cost'] / s['buy_shares'] if s['buy_shares'] > 0 else 0
        status = "LONG" if net_shares > 0.1 else "FLAT" if abs(net_shares) < 0.1 else "SHORT"
        print(f'  {s["outcome"]:6}: BUY {s["buy_shares"]:7.2f} (${s["buy_cost"]:7.2f}) | SELL {s["sell_shares"]:7.2f} (${s["sell_revenue"]:7.2f}) | {status} {abs(net_shares):.2f}')

# Pattern analysis
print()
print('=' * 80)
print('  STRATEGY PATTERN ANALYSIS')
print('=' * 80)

# Find markets where they bought both sides
both_sides_markets = []
for title, trades_list in markets.items():
    outcomes = set(t.get('outcome', '') for t in trades_list)
    sides = set(t.get('side', '').upper() for t in trades_list)
    
    if len(outcomes) >= 2 and 'BUY' in sides:
        up_buys = sum(float(t.get('size', 0)) * float(t.get('price', 0)) 
                      for t in trades_list 
                      if t.get('outcome') == 'Up' and t.get('side', '').upper() == 'BUY')
        down_buys = sum(float(t.get('size', 0)) * float(t.get('price', 0)) 
                        for t in trades_list 
                        if t.get('outcome') == 'Down' and t.get('side', '').upper() == 'BUY')
        
        if up_buys > 0 and down_buys > 0:
            both_sides_markets.append({
                'title': title,
                'up_cost': up_buys,
                'down_cost': down_buys,
                'total': up_buys + down_buys
            })

if both_sides_markets:
    print('\nMarkets where BOTH UP and DOWN were bought:')
    for m in sorted(both_sides_markets, key=lambda x: x['total'], reverse=True)[:10]:
        print(f'  {m["title"][:50]}')
        print(f'    UP: ${m["up_cost"]:.2f} | DOWN: ${m["down_cost"]:.2f} | TOTAL: ${m["total"]:.2f}')

# Time pattern
print()
print('=' * 80)
print('  TIMING ANALYSIS')
print('=' * 80)

# Group by hour
hourly = defaultdict(lambda: {'count': 0, 'volume': 0})
for t in trades:
    ts = int(t.get('timestamp', 0))
    hour = datetime.fromtimestamp(ts).hour
    size = float(t.get('size', 0))
    price = float(t.get('price', 0))
    hourly[hour]['count'] += 1
    hourly[hour]['volume'] += size * price

print('\nTrades by hour (UTC):')
for hour in sorted(hourly.keys()):
    data = hourly[hour]
    print(f'  {hour:02d}:00 - {data["count"]:3d} trades, ${data["volume"]:8.2f} volume')

# Price range analysis
print()
print('=' * 80)
print('  PRICE RANGE ANALYSIS')
print('=' * 80)

price_ranges = {
    '0.00-0.10': {'count': 0, 'volume': 0},
    '0.10-0.30': {'count': 0, 'volume': 0},
    '0.30-0.50': {'count': 0, 'volume': 0},
    '0.50-0.70': {'count': 0, 'volume': 0},
    '0.70-0.90': {'count': 0, 'volume': 0},
    '0.90-1.00': {'count': 0, 'volume': 0},
}

for t in trades:
    if t.get('side', '').upper() != 'BUY':
        continue
    price = float(t.get('price', 0))
    size = float(t.get('size', 0))
    cost = size * price
    
    if price < 0.10:
        key = '0.00-0.10'
    elif price < 0.30:
        key = '0.10-0.30'
    elif price < 0.50:
        key = '0.30-0.50'
    elif price < 0.70:
        key = '0.50-0.70'
    elif price < 0.90:
        key = '0.70-0.90'
    else:
        key = '0.90-1.00'
    
    price_ranges[key]['count'] += 1
    price_ranges[key]['volume'] += cost

print('\nBUY orders by price range:')
for range_key, data in price_ranges.items():
    pct = (data['volume'] / total_buy_cost * 100) if total_buy_cost > 0 else 0
    print(f'  {range_key}: {data["count"]:4d} orders, ${data["volume"]:10.2f} ({pct:5.1f}%)')

# Summary
print()
print('=' * 80)
print('  SUMMARY')
print('=' * 80)
print(f'  Total BUY orders:  {total_buys:5d} (${total_buy_cost:,.2f})')
print(f'  Total SELL orders: {total_sells:5d} (${total_sell_revenue:,.2f})')
print(f'  Net Cash Flow:     ${total_sell_revenue - total_buy_cost:,.2f}')
print(f'  Markets traded:    {len(markets)}')
print('=' * 80)

