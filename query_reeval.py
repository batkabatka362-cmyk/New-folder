import sqlite3

conn = sqlite3.connect("memebot.db")
cursor = conn.cursor()

print("=== SELL REASONS AND COUNTS ===")
cursor.execute("""
    SELECT reason, COUNT(*) as count
    FROM trades
    WHERE side = 'sell'
    GROUP BY reason
    ORDER BY count DESC
""")

for row in cursor.fetchall():
    print(f"{row[0]}: {row[1]}")

print("\n=== CONCENTRATION EXITS ===")
cursor.execute("SELECT COUNT(*) FROM trades WHERE side = 'sell' AND reason = 'conc_rise'")
print(f"conc_rise: {cursor.fetchone()[0]}")

cursor.execute("SELECT COUNT(*) FROM trades WHERE side = 'sell' AND reason = 'reeval_cut'")
print(f"reeval_cut: {cursor.fetchone()[0]}")

cursor.execute("SELECT COUNT(*) FROM trades WHERE side = 'sell' AND reason = 'reeval_extend'")
print(f"reeval_extend: {cursor.fetchone()[0]}")

conn.close()
