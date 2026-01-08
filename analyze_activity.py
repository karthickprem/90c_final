"""
Analyze bot trading activity.
"""

import requests
from datetime import datetime
from collections import defaultdict

# Correct proxy address
PROXY = '0x3C008F983c1d1097a1304e38B683B018aC589500'

# Fetch trades
r = requests.get('https://data-api.polymarket.com/trades', params={
    'user': PROXY,
    'limit': 100
}, timeout=15)

trades = r.json()
print('=' * 80)
print('  BOT TRADING ACTIVITY ANALYSIS')
print('=' * 80)
print(f'  Account: {PROXY}')
print(f'  Analysis Time: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}')
print('=' * 80)
print()

# Group by market (title)
markets = defaultdict(list)
for t in trades:
    title = t.get('title', 'Unknown')
    markets[title].append(t)

total_pnl = 0
total_trades = 0
total_buy_cost = 0
total_sell_revenue = 0

for title, trades_list in sorted(markets.items(), key=lambda x: max(t.get('timestamp', 0) for t in x[1]), reverse=True):
    print(f'MARKET: {title}')
    print('-' * 80)
    
    buys = []
    sells = []
    
    for t in sorted(trades_list, key=lambda x: x.get('timestamp', 0)):
        ts = int(t.get('timestamp', 0))
        dt = datetime.fromtimestamp(ts)
        side = t.get('side', '').upper()
        size = float(t.get('size', 0))
        price = float(t.get('price', 0))
        cost = size * price
        outcome = t.get('outcome', 'Unknown')
        
        print(f'  {dt.strftime("%Y-%m-%d %H:%M:%S")} | {side:4} {outcome:4} | {size:6.2f} @ {price:.4f} | ${cost:6.2f}')
        total_trades += 1
        
        if side == 'BUY':
            buys.append({'size': size, 'price': price, 'cost': cost, 'outcome': outcome})
            total_buy_cost += cost
        else:
            sells.append({'size': size, 'price': price, 'revenue': cost, 'outcome': outcome})
            total_sell_revenue += cost
    
    # Calculate PnL for this market
    mkt_buy_cost = sum(b['cost'] for b in buys)
    mkt_buy_shares = sum(b['size'] for b in buys)
    mkt_sell_revenue = sum(s['revenue'] for s in sells)
    mkt_sell_shares = sum(s['size'] for s in sells)
    
    avg_buy = mkt_buy_cost / mkt_buy_shares if mkt_buy_shares > 0 else 0
    avg_sell = mkt_sell_revenue / mkt_sell_shares if mkt_sell_shares > 0 else 0
    
    print()
    if mkt_buy_shares > 0:
        print(f'  BUYS:  {mkt_buy_shares:6.2f} shares @ avg ${avg_buy:.4f} = ${mkt_buy_cost:.2f}')
    if mkt_sell_shares > 0:
        print(f'  SELLS: {mkt_sell_shares:6.2f} shares @ avg ${avg_sell:.4f} = ${mkt_sell_revenue:.2f}')
    
    # Simple PnL: sell revenue - buy cost (for matched shares)
    matched = min(mkt_buy_shares, mkt_sell_shares)
    if matched > 0 and mkt_buy_shares > 0:
        # Realized PnL on matched shares
        pnl = mkt_sell_revenue - (avg_buy * mkt_sell_shares)
        total_pnl += pnl
        pnl_str = f'+${pnl:.2f}' if pnl >= 0 else f'-${abs(pnl):.2f}'
        print(f'  REALIZED PnL: {pnl_str}')
    
    remaining = mkt_buy_shares - mkt_sell_shares
    if abs(remaining) > 0.01:
        status = "LONG" if remaining > 0 else "SHORT"
        print(f'  OPEN POSITION: {status} {abs(remaining):.2f} shares')
    
    print()

print('=' * 80)
print('  SUMMARY')
print('=' * 80)
print(f'  Total Trades: {total_trades}')
print(f'  Total Bought: ${total_buy_cost:.2f}')
print(f'  Total Sold:   ${total_sell_revenue:.2f}')
print(f'  Net Cash Flow: ${total_sell_revenue - total_buy_cost:.2f}')
print(f'  Realized PnL:  ${total_pnl:.2f}')
print('=' * 80)

# Problem trades analysis
print()
print('=' * 80)
print('  PROBLEM ANALYSIS')
print('=' * 80)

problems = []

# Find trades where we sold at lower price than bought
for title, trades_list in markets.items():
    buys = [t for t in trades_list if t.get('side', '').upper() == 'BUY']
    sells = [t for t in trades_list if t.get('side', '').upper() == 'SELL']
    
    if buys and sells:
        avg_buy = sum(float(t.get('price', 0)) * float(t.get('size', 0)) for t in buys) / sum(float(t.get('size', 0)) for t in buys)
        avg_sell = sum(float(t.get('price', 0)) * float(t.get('size', 0)) for t in sells) / sum(float(t.get('size', 0)) for t in sells)
        
        if avg_sell < avg_buy:
            loss = (avg_buy - avg_sell) * sum(float(t.get('size', 0)) for t in sells)
            problems.append({
                'market': title[:50],
                'avg_buy': avg_buy,
                'avg_sell': avg_sell,
                'loss': loss
            })

if problems:
    print('  Trades where SELL < BUY (losses):')
    for p in sorted(problems, key=lambda x: x['loss'], reverse=True):
        print(f'    {p["market"]}')
        print(f'      Bought @ {p["avg_buy"]:.4f}, Sold @ {p["avg_sell"]:.4f}, Loss: ${p["loss"]:.2f}')
else:
    print('  No losing trades identified.')
