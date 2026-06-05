import sqlite3
import json

conn = sqlite3.connect("memebot.db")
cursor = conn.cursor()

print("=== THE SINGLE conc_rise EXIT ===")
cursor.execute("""
    SELECT mint, symbol, sol, tokens, price_sol, fee_sol, created_at
    FROM trades
    WHERE side = 'sell' AND reason = 'conc_rise'
""")
row = cursor.fetchone()
if row:
    print(f"Mint: {row[0]}")
    print(f"Symbol: {row[1]}")
    print(f"SOL sold: {row[2]:.4f}")
    print(f"Tokens sold: {row[3]:.2f}")
    print(f"Price SOL: {row[4]:.8f}")
    print(f"Fee SOL: {row[5]:.6f}")
    print(f"Time: {row[6]}")
    
    # Get the position entry
    mint = row[0]
    cursor.execute("""
        SELECT entry_features, entry_price_sol, avg_price_sol, qty
        FROM positions
        WHERE mint = ?
    """, (mint,))
    pos = cursor.fetchone()
    if pos:
        features = json.loads(pos[0]) if pos[0] else {}
        entry_conc = features.get('top5_concentration_pct', None)
        print(f"\nEntry concentration: {entry_conc}")
        print(f"Entry price SOL: {pos[1]:.8f}")
        print(f"Avg price SOL: {pos[2]:.8f}")
        print(f"Quantity: {pos[3]:.2f}")
    
    # Also check closed trades for this mint
    cursor.execute("""
        SELECT pnl_sol, pnl_pct, entry_features
        FROM trade_outcomes
        WHERE mint = ?
    """, (mint,))
    outcomes = cursor.fetchall()
    if outcomes:
        print(f"\nClosed trade outcomes for this mint: {len(outcomes)}")
        for outcome in outcomes:
            print(f"  PnL: {outcome[0]:.4f} SOL ({outcome[1]:+.1f}%)")

print("\n=== REEVAL LOOP METRICS ===")
# Count how many positions had concentration data at entry
cursor.execute("""
    SELECT COUNT(DISTINCT mint) FROM positions
    WHERE entry_features LIKE '%top5_concentration%'
""")
with_conc = cursor.fetchone()[0]
print(f"Positions with concentration at entry: {with_conc}")

cursor.execute("SELECT COUNT(DISTINCT mint) FROM positions")
total_positions = cursor.fetchone()[0]
print(f"Total positions ever: {total_positions}")

# Check if Helius RPC is even enabled
print("\n=== CHECKING CONFIG ===")
import os
helius_url = os.getenv('HELIUS_RPC_URL', '')
print(f"HELIUS_RPC_URL env set: {bool(helius_url)}")
if helius_url:
    print(f"  Value: {helius_url[:50]}...")

conn.close()
