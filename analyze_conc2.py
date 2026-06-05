import sqlite3
import json

conn = sqlite3.connect("memebot.db")
cursor = conn.cursor()

# Get schema
cursor.execute("PRAGMA table_info(trades)")
print("=== TRADES TABLE SCHEMA ===")
for col in cursor.fetchall():
    print(f"  {col[1]}: {col[2]}")

print("\n=== THE SINGLE conc_rise EXIT ===")
cursor.execute("""
    SELECT mint, symbol, sol, price_sol, fee_sol
    FROM trades
    WHERE side = 'sell' AND reason = 'conc_rise'
""")
row = cursor.fetchone()
if row:
    mint = row[0]
    print(f"Mint: {mint}")
    print(f"Symbol: {row[1]}")
    print(f"SOL sold: {row[2]:.4f}")
    print(f"Price SOL: {row[3]:.8f}")
    print(f"Fee SOL: {row[4]:.6f}")

print("\n=== REEVAL LOOP STATUS ===")
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

print(f"Coverage: {with_conc}/{total_positions} ({100*with_conc/total_positions:.1f}%)")

conn.close()
