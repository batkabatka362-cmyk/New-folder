"""Brain audit (deferred-5 #1) — does the advisory LLM brain actually EARN ITS KEEP, or is the bot
secretly a mechanical rule-bot?

The north star is a self-REASONING AI trader, not a rule machine. This measures whether reality matches:
  1. BRAIN-USAGE RATE  — of every DECIDED verdict, what fraction did the LLM brain drive (ollama/haiku/
     sonnet tiers) vs the deterministic RULE fallback? (Available now from the `verdicts` table.) A tiny
     fraction means the expensive intelligence layer is barely consulted and the bot trades as a rule-bot.
  2. BRAIN-vs-RULE A/B — split the realized CLEAN book by verdict source: do brain-sourced BUYS outperform
     rule-fallback buys on the same honest, glitch-excluded book? (Needs trade_outcomes.verdict_source,
     logged going forward — historical rows are NULL.)

Read-only, offline. Honest by construction: it reports what the data shows, including "not enough
brain-sourced trades yet" when the answer is forward-gated.

  python -m memebot.backtest.brain_audit
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict

from ..config import get_settings

_GLITCH_RET_MULT = 20.0          # same pricing-glitch ceiling as pnl_reconstruct
_BRAIN_HINTS = ("ollama", "haiku", "sonnet", "claude", "gpt")   # a source that is NOT the rule fallback


def _is_brain(source: str) -> bool:
    s = (source or "").lower()
    return s != "rule" and any(h in s for h in _BRAIN_HINTS)


def usage_rate(rows: list[tuple[str, int]]) -> dict:
    """rows = [(source, count)] from the verdicts table -> brain vs rule decision share."""
    total = sum(n for _, n in rows)
    brain = sum(n for src, n in rows if _is_brain(src))
    rule = sum(n for src, n in rows if (src or "").lower() == "rule")
    other = total - brain - rule
    return {"total": total, "brain": brain, "rule": rule, "other": other,
            "brain_frac": (brain / total) if total else 0.0}


def ab_book(rows: list[tuple]) -> dict:
    """rows = [(verdict_source, pnl_sol, pnl_pct)] from trade_outcomes -> per-source CLEAN book
    (glitch round-trips excluded). Groups brain-sourced vs rule-sourced realized outcomes."""
    groups: dict[str, list] = defaultdict(list)
    for src, pnl, pct in rows:
        if src is None or src == "":
            groups["(unlabeled)"].append((pnl, pct))
            continue
        if pct is not None and (1.0 + float(pct)) > _GLITCH_RET_MULT:
            continue                                  # drop the same pricing glitch the honest book excludes
        groups["brain" if _is_brain(src) else ("rule" if src.lower() == "rule" else src)].append((pnl, pct))
    out = {}
    for g, vals in groups.items():
        n = len(vals)
        net = sum(p for p, _ in vals)
        wins = sum(1 for p, _ in vals if p > 0)
        gross_w = sum(p for p, _ in vals if p > 0)
        gross_l = -sum(p for p, _ in vals if p < 0)
        out[g] = {"n": n, "net": net, "win": (wins / n) if n else 0.0,
                  "pf": (gross_w / gross_l) if gross_l > 0 else float("inf") if gross_w > 0 else 0.0}
    return out


def main() -> None:
    s = get_settings()
    conn = sqlite3.connect(s.db_path)
    try:
        vrows = conn.execute("SELECT source, COUNT(*) FROM verdicts GROUP BY source").fetchall()
        try:
            trows = conn.execute("SELECT verdict_source, pnl_sol, pnl_pct FROM trade_outcomes").fetchall()
        except sqlite3.OperationalError:
            trows = []
    finally:
        conn.close()

    u = usage_rate(vrows)
    print("=== BRAIN-USAGE RATE (of every decided verdict) ===")
    print(f"  total decided: {u['total']}   brain: {u['brain']}   rule: {u['rule']}   other: {u['other']}")
    print(f"  -> the LLM brain drove {u['brain_frac']*100:.2f}% of decisions"
          + ("  *** the bot is trading as a RULE-BOT, not the self-reasoning AI of the vision ***"
             if u['total'] and u['brain_frac'] < 0.05 else ""))
    print("     (mostly historical: the brain only runs when the local LLM is UP, the score is in the")
    print("     uncertain band [entry,high_conf), and within the per-minute budget. Re-run as forward")
    print("     decisions with the LLM up accrue.)\n")

    ab = ab_book(trows)
    print("=== BRAIN-vs-RULE A/B (realized CLEAN book by verdict source) ===")
    labeled = {g: v for g, v in ab.items() if g != "(unlabeled)"}
    if not labeled or all(v["n"] == 0 for v in labeled.values()):
        un = ab.get("(unlabeled)", {}).get("n", 0)
        print(f"  not enough source-labeled trades yet ({un} pre-logging rows are NULL). The verdict-source")
        print("  stamp is live now; this A/B becomes answerable as new closed trades accrue.")
    else:
        for g, v in sorted(labeled.items()):
            pf = "inf" if v["pf"] == float("inf") else f"{v['pf']:.2f}"
            print(f"  {g:>12}: n={v['n']:>3}  net={v['net']:+.4f} SOL  win={v['win']*100:.0f}%  pf={pf}")
        print("  -> the honest question: do brain-sourced buys beat the rule fallback on the SAME book?")


if __name__ == "__main__":
    main()
