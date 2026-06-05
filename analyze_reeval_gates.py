import sqlite3
import json

conn = sqlite3.connect("memebot.db")
cursor = conn.cursor()

print("=== REEVAL GATE EFFECTIVENESS ===\n")

# Current gate thresholds
holder_cut_pct = 96.0
holder_good_pct = 85.0
concentration_warn_pct = 90.0

print(f"Reeval gates:")
print(f"  holder_cut_pct (close): {holder_cut_pct}%")
print(f"  concentration_warn_pct (derisk zone): {concentration_warn_pct}%")
print(f"  holder_good_pct (extend): {holder_good_pct}%")
print()

# Check: how many positions had concentration >= holder_cut_pct (96%)
cursor.execute("""
    SELECT 
        COUNT(*) as total,
        SUM(CASE WHEN CAST(json_extract(entry_features, '$.top5_concentration_pct') AS REAL) >= ? THEN 1 ELSE 0 END) as at_cut_threshold,
        SUM(CASE WHEN CAST(json_extract(entry_features, '$.top5_concentration_pct') AS REAL) >= ? THEN 1 ELSE 0 END) as at_warn_threshold,
        SUM(CASE WHEN CAST(json_extract(entry_features, '$.top5_concentration_pct') AS REAL) <= ? THEN 1 ELSE 0 END) as at_good_threshold
    FROM trade_outcomes
    WHERE json_extract(entry_features, '$.top5_concentration_pct') IS NOT NULL
""", (holder_cut_pct, concentration_warn_pct, holder_good_pct))

total, at_cut, at_warn, at_good = cursor.fetchone()
print(f"At ENTRY:")
print(f"  >= {holder_cut_pct}% (cut threshold): {at_cut}/{total} ({100*at_cut/total:.1f}%)")
print(f"  >= {concentration_warn_pct}% (warn threshold): {at_warn}/{total} ({100*at_warn/total:.1f}%)")
print(f"  <= {holder_good_pct}% (good threshold): {at_good}/{total} ({100*at_good/total:.1f}%)")

# These high-concentration entries: what were their outcomes?
print(f"\n=== TRADES ENTERED AT HIGH CONCENTRATION (>= {holder_cut_pct}%) ===")
cursor.execute("""
    SELECT 
        symbol,
        CAST(json_extract(entry_features, '$.top5_concentration_pct') AS REAL) as entry_conc,
        pnl_pct,
        reason,
        hold_s
    FROM trade_outcomes
    WHERE json_extract(entry_features, '$.top5_concentration_pct') IS NOT NULL
        AND CAST(json_extract(entry_features, '$.top5_concentration_pct') AS REAL) >= ?
    ORDER BY entry_conc DESC
""", (holder_cut_pct,))

print(f"Entry conc | Symbol | PnL %  | Exit reason | Hold (min)")
for row in cursor.fetchall():
    symbol, conc, pnl, reason, hold_s = row
    print(f"{conc:8.1f}% | {symbol:6} | {pnl:+6.2f}% | {reason:15} | {hold_s/60:6.1f}")

# The key insight: concentration_rise as the lever
print(f"\n=== CONCENTRATION RISE ANALYSIS ===")
print(f"Current threshold: conc_rise_cut_pct = {8.0}pp\n")

# We only have 1 conc_rise. What was its rise?
cursor.execute("""
    SELECT symbol, 
        CAST(json_extract(entry_features, '$.top5_concentration_pct') AS REAL) as entry_conc,
        pnl_sol
    FROM trade_outcomes
    WHERE reason = 'conc_rise'
""")
cr = cursor.fetchone()
if cr:
    symbol, entry_conc, pnl = cr
    print(f"DBGI (the only conc_rise):")
    print(f"  Entry concentration: {entry_conc:.1f}%")
    print(f"  Rise detected: was high enough to trigger at {entry_conc + 8.0:.1f}%")
    print(f"  PnL: {pnl:.4f} SOL (loss, but small)")

# Now analyze: which losers had a concentration RISE that went undetected?
# We can't measure rise without a live read, but we can see if high-entry-conc losses
# would have been caught by the rise threshold
print(f"\n=== MISSED OPPORTUNITIES (high-conc entries that lost) ===")
cursor.execute("""
    SELECT 
        symbol,
        CAST(json_extract(entry_features, '$.top5_concentration_pct') AS REAL) as entry_conc,
        pnl_sol,
        pnl_pct,
        reason
    FROM trade_outcomes
    WHERE json_extract(entry_features, '$.top5_concentration_pct') IS NOT NULL
        AND pnl_pct < -0.2  -- significant losses
        AND CAST(json_extract(entry_features, '$.top5_concentration_pct') AS REAL) >= 30.0  -- medium-high entry conc
    ORDER BY pnl_pct
    LIMIT 10
""")

print("Entry conc | Symbol | PnL $  | PnL %  | Exit reason")
for row in cursor.fetchall():
    symbol, conc, pnl_sol, pnl_pct, reason = row
    print(f"{conc:8.1f}% | {symbol:6} | {pnl_sol:+.4f} | {pnl_pct:+6.2f}% | {reason}")

conn.close()
