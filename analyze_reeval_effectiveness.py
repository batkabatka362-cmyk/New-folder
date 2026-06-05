import sqlite3
import json

conn = sqlite3.connect("memebot.db")
cursor = conn.cursor()

print("=== REEVAL EFFECTIVENESS ANALYSIS ===\n")

# Get the DBGI trade that was closed by conc_rise
cursor.execute("""
    SELECT entry_ts, ts, pnl_sol, pnl_pct, hold_s
    FROM trade_outcomes
    WHERE mint LIKE 'BVmDvf%' AND reason = 'conc_rise'
""")
dbgi = cursor.fetchone()
if dbgi:
    print(f"DBGI (conc_rise exit):")
    print(f"  Hold duration: {dbgi[4]:.1f} seconds ({dbgi[4]/60:.1f} minutes)")
    print(f"  PnL: {dbgi[2]:.4f} SOL ({dbgi[3]:+.2f}%)")
    print(f"  VERDICT: Loss avoidance? {dbgi[2] >= 0}")

# Now find all trades that WERE NOT exited via conc_rise
# to see if concentration_rise is a real signal
print(f"\n=== CONCENTRATION AT ENTRY FOR ALL TRADES ===")
cursor.execute("""
    SELECT 
        reason,
        COUNT(*) as count,
        AVG(CAST(json_extract(entry_features, '$.top5_concentration_pct') AS REAL)) as avg_entry_conc,
        AVG(pnl_pct) as avg_pnl_pct
    FROM trade_outcomes
    GROUP BY reason
    ORDER BY count DESC
""")

for row in cursor.fetchall():
    reason, cnt, avg_conc, avg_pnl = row
    if avg_conc is None:
        avg_conc_str = "N/A"
    else:
        avg_conc_str = f"{avg_conc:.1f}%"
    pnl_str = f"{avg_pnl:+.2f}%" if avg_pnl is not None else "N/A"
    print(f"{reason:20} ({cnt:2}): avg_entry_conc={avg_conc_str:7} avg_pnl={pnl_str}")

# Analyze: did the reeval mechanism help at all?
print(f"\n=== REEVAL IMPACT ===")
cursor.execute("""
    SELECT 
        COUNT(*) as total,
        SUM(CASE WHEN reason = 'conc_rise' THEN 1 ELSE 0 END) as conc_rise_exits,
        SUM(CASE WHEN reason LIKE 'derisk%' THEN 1 ELSE 0 END) as derisk_exits,
        SUM(CASE WHEN reason = 'conc_rise' THEN pnl_sol ELSE 0 END) as conc_rise_pnl
    FROM trade_outcomes
""")

total, conc_rise_exits, derisk_exits, conc_rise_pnl = cursor.fetchone()
print(f"Total closed trades: {total}")
print(f"Exits due to conc_rise: {conc_rise_exits} ({100*conc_rise_exits/total:.1f}%)")
print(f"Exits due to derisk_*: {derisk_exits} ({100*derisk_exits/total:.1f}%)")
if conc_rise_exits > 0:
    print(f"PnL from conc_rise exits: {conc_rise_pnl:.4f} SOL")

# Check: was that 1 conc_rise a loser?
cursor.execute("""
    SELECT pnl_sol, pnl_pct FROM trade_outcomes WHERE reason = 'conc_rise'
""")
conc_rise_trade = cursor.fetchone()
if conc_rise_trade:
    print(f"\nThe single conc_rise exit lost {conc_rise_trade[0]:.4f} SOL ({conc_rise_trade[1]:.2f}%)")
    print(f"  -> It DID catch a loser (early exit on rug signal)")

# Statistical: concentration and outcomes
print(f"\n=== CONCENTRATION CORRELATION WITH PNLS ===")
cursor.execute("""
    SELECT 
        ROUND(CAST(json_extract(entry_features, '$.top5_concentration_pct') AS REAL) / 10) * 10 as conc_bucket,
        COUNT(*) as count,
        AVG(pnl_pct) as avg_pnl_pct,
        SUM(CASE WHEN pnl_pct > 0 THEN 1 ELSE 0 END) as winners
    FROM trade_outcomes
    WHERE json_extract(entry_features, '$.top5_concentration_pct') IS NOT NULL
    GROUP BY conc_bucket
    ORDER BY conc_bucket
""")

print("Entry conc bucket  | Count | Avg PnL% | Winners")
for row in cursor.fetchall():
    bucket, cnt, avg_pnl, winners = row
    if bucket is not None:
        print(f"  {bucket:5.0f}%-{bucket+10:5.0f}%  | {cnt:4} | {avg_pnl:7.2f}% | {winners:3}")

conn.close()
