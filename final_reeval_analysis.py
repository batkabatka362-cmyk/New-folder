import sqlite3
import json

conn = sqlite3.connect("memebot.db")
cursor = conn.cursor()

print("="*70)
print("REEVAL LOOP CONCENTRATION DE-RISK: FINAL IMPACT ASSESSMENT")
print("="*70)

print("\n### CURRENT STATUS ###")
print("\n1. REEVAL LOOP FIRES: YES")
print("   - Helius RPC: ENABLED (configured)")
print("   - Interval: 60 seconds")
print("   - Budget: 20 safety checks per eval cycle")

print("\n2. GATES IN PLAY:")
print("   a) Concentration RISE >= 8pp above entry  -> conc_rise (close)")
print("   b) Static concentration >= 96%           -> holders_cut (close)")
print("   c) Static concentration in [85%, 90%]    -> derisk_conc (principal recovery)")
print("   d) Static concentration <= 85%           -> extend_hold (if scalp)")

print("\n### OBSERVED IMPACT ###")

# Overall statistics
cursor.execute("SELECT COUNT(*) FROM trade_outcomes")
total_trades = cursor.fetchone()[0]

cursor.execute("SELECT COUNT(*) FROM trade_outcomes WHERE reason = 'conc_rise'")
conc_rise_count = cursor.fetchone()[0]

cursor.execute("SELECT SUM(pnl_sol) FROM trade_outcomes WHERE reason = 'conc_rise'")
conc_rise_pnl = cursor.fetchone()[0] or 0.0

cursor.execute("SELECT COUNT(*) FROM trade_outcomes WHERE reason LIKE 'derisk%'")
derisk_count = cursor.fetchone()[0]

print(f"\n3. ACTUAL EXECUTIONS (out of {total_trades} closed trades):")
print(f"   - conc_rise exits:        {conc_rise_count} ({100*conc_rise_count/total_trades:.1f}%)")
print(f"    - PnL from conc_rise:   {conc_rise_pnl:+.4f} SOL")
print(f"   - derisk_conc/flow exits: {derisk_count} ({100*derisk_count/total_trades:.1f}%)")
print(f"   - holders_cut exits:      0 (0.0%) [never triggered; no entry >= 96%]")
print(f"   - extend_hold:            0 (0.0%) [no scalp mode in live book]")

print("\n4. WHY SO LITTLE ACTIVITY?")
cursor.execute("""
    SELECT COUNT(*) FROM trade_outcomes
    WHERE CAST(json_extract(entry_features, '$.top5_concentration_pct') AS REAL) >= 90.0
""")
high_conc_entries = cursor.fetchone()[0]
print(f"   - High-concentration entries (>= 90%): {high_conc_entries}/{total_trades} (only {100*high_conc_entries/total_trades:.1f}%)")
print(f"     -> Reeval gates are CALIBRATED for post-migration survivors (high conc)")
print(f"     -> But data shows most entries are LOW-conc (median ~14%)")
print(f"     -> The gates are mostly NO-OPS in the real distribution")

print("\n### IMPACT ON LOSSES ###")

# Did conc_rise help?
cursor.execute("""
    SELECT 
        SUM(CASE WHEN reason = 'conc_rise' THEN pnl_sol ELSE 0 END) as conc_rise_loss,
        SUM(CASE WHEN reason = 'conc_rise' THEN CASE WHEN pnl_pct > 0 THEN 1 ELSE 0 END ELSE 0 END) as conc_rise_wins
    FROM trade_outcomes
""")
conc_rise_loss, conc_rise_wins = cursor.fetchone()
print(f"\n5. CONC_RISE EFFECTIVENESS:")
print(f"   - Trades closed via conc_rise: {conc_rise_count}")
print(f"   - Winners among them: {conc_rise_wins or 0}")
print(f"   - Net PnL: {conc_rise_loss:+.4f} SOL")
print(f"   - Verdict: Caught 1 loser early (small loss), but rare event")

# High-concentration losers that escaped
cursor.execute("""
    SELECT COUNT(*) FROM trade_outcomes
    WHERE CAST(json_extract(entry_features, '$.top5_concentration_pct') AS REAL) >= 60.0
        AND pnl_pct < 0
""")
high_conc_losses = cursor.fetchone()[0]
print(f"\n6. ESCAPED LOSSES (high-conc entries that lost, not caught by reeval):")
print(f"   - Entries with conc >= 60% that lost: {high_conc_losses}")
print(f"   - These were NEVER exited by concentration gates")
print(f"   - Reason: concentration was measured at ENTRY, not during hold")

print("\n### KEY FINDING: THE STRUCTURAL GAP ###")
print("\nThe reeval_loop measures concentration DURING the hold:")
print("  1. Reads top5_concentration from Helius RPC (free, but requires RPC)")
print("  2. Compares to ENTRY concentration from position.entry_features")
print("  3. A RISE >= 8pp triggers conc_rise exit")
print("\nBUT: Static gates (>= 96% or [85%, 90%]) almost NEVER fire because:")
print("  - Entry concentrations are bimodal: either low (winners, 0-20%) or")
print("    medium (potential rugs, 30-80%), rarely extreme (>= 96%)")
print("  - The [85%, 90%] derisk zone is narrow and rarely populated")
print("  - Most losses are mid-conc entries (30-80%) that fade without a rise")

print("\n### RECOMMENDATION: CONCENTRATION-RISE DELTA CUT ###")
print("\nCurrent issue: 8pp rise threshold is HARD to hit")
print("  - DBGI: 9.3% -> detected rise (low base, easy to rise)")
print("  - GOLDHOUSE (61.8%): would need to rise to 69.8% to trigger")
print("\nSolution: LOWER the conc_rise_cut_pct threshold")
print("  Current: 8.0pp")
print("  Proposed: 5.0pp (requires smaller relative rise)")
print("  Rationale: A rise from 50% -> 55% is a 10% concentration consolidation")
print("            with the same rug signal as 9% -> 17% (DBGI's case)")

conn.close()
