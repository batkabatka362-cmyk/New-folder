from memebot.config import get_settings

s = get_settings()

print("=== REEVAL CONFIGURATION ===")
print(f"reeval_interval_s: {s.reeval_interval_s}")
print(f"holder_cut_pct: {s.holder_cut_pct}")
print(f"holder_good_pct: {s.holder_good_pct}")
print(f"conc_rise_cut_pct: {s.safety.conc_rise_cut_pct}")
print(f"concentration_warn_pct: {s.safety.concentration_warn_pct}")
print(f"max_safety_checks_per_cycle: {s.max_safety_checks_per_cycle}")

print("\n=== HELIUS RPC ===")
print(f"helius_rpc_url: {s.helius_rpc_url}")
print(f"Helius RPC is: {'ENABLED' if s.helius_rpc_url else 'DISABLED (None/empty)'}")

print("\n=== REEVAL LOOP WOULD RUN? ===")
# The reeval loop checks: if self.helius is None: continue
# So if no helius_rpc_url, the loop just spins idle
if not s.helius_rpc_url:
    print("NO - Helius RPC is not configured, so _reeval_loop exists but does nothing")
else:
    print("YES - Helius RPC is configured")

print("\n=== REEVAL ACTION LOGIC ===")
print(f"Action at conc >= {s.holder_cut_pct}%: 'cut' (close position)")
print(f"Action at conc <= {s.holder_good_pct}%: 'extend' (if scalp mode)")
print(f"Concentration RISE >= {s.safety.conc_rise_cut_pct}pp above entry: close (conc_rise)")
print(f"Concentration in warn zone [{s.holder_good_pct}, {s.safety.concentration_warn_pct}]: derisk if profitable")

