import sqlite3

conn = sqlite3.connect("memebot.db")
cursor = conn.cursor()

# Get all tables
cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
print("=== TABLES IN DB ===")
tables = [row[0] for row in cursor.fetchall()]
for t in tables:
    print(f"  {t}")

# Now focus on trade_outcomes which likely has entry features
if 'trade_outcomes' in tables:
    cursor.execute("PRAGMA table_info(trade_outcomes)")
    print("\n=== trade_outcomes SCHEMA ===")
    for col in cursor.fetchall():
        print(f"  {col[1]}: {col[2]}")
    
    # Check how many outcomes had concentration at entry
    cursor.execute("""
        SELECT COUNT(*) FROM trade_outcomes
        WHERE entry_features LIKE '%concentration%'
    """)
    with_conc = cursor.fetchone()[0]
    
    cursor.execute("SELECT COUNT(*) FROM trade_outcomes")
    total = cursor.fetchone()[0]
    
    print(f"\n=== CONCENTRATION DATA COVERAGE ===")
    print(f"Outcomes with concentration in entry_features: {with_conc}/{total}")

# The DBGI mint that triggered conc_rise
print("\n=== DBGI conc_rise DETAILS ===")
cursor.execute("""
    SELECT * FROM trade_outcomes
    WHERE mint LIKE 'BVmDvf%'
""")
dbgi = cursor.fetchone()
if dbgi:
    print(f"Found DBGI trade outcome")
    cursor.execute("PRAGMA table_info(trade_outcomes)")
    cols = [col[1] for col in cursor.fetchall()]
    for i, col in enumerate(cols[:10]):  # First 10 columns
        print(f"  {col}: {dbgi[i]}")
    if 'entry_features' in cols:
        idx = cols.index('entry_features')
        import json
        features = json.loads(dbgi[idx]) if dbgi[idx] else {}
        print(f"  entry_features keys: {list(features.keys())[:10]}")
        if 'top5_concentration_pct' in features:
            print(f"    top5_concentration_pct: {features['top5_concentration_pct']}")

conn.close()
