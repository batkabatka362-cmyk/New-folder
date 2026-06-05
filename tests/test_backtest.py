"""Offline backtest engine: cost model, feature round-trip, simulate aggregation."""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import tempfile

from memebot.backtest.simulate import (candidate_from_features, round_trip_cost_pct,
                                        simulate)
from memebot.config import Fees, Settings
from memebot.filter.features import build_features
from memebot.models import Candidate
from memebot.portfolio.portfolio import ClosedTrade
from memebot.signals.scoring import Scorer
from memebot.storage.db import Storage


def test_fixed_horizon_labels():
    from memebot.backtest.label import fixed_horizon_labels
    f = {"liquidity_usd": 1.0}
    rows = [
        ("A", 0.0, 1.0, f), ("A", 1800.0, 2.0, f),        # +100% at +30m -> positive
        ("B", 0.0, 1.0, f), ("B", 1800.0, 1.1, f),        # +10% -> negative
        ("C", 0.0, 1.0, f), ("C", 100.0, 1.5, f),         # nothing near +30m -> dropped
    ]
    out = fixed_horizon_labels(rows, horizon_s=1800.0, up=0.5, tol_s=300.0)
    d = {m: (lbl, round(fwd, 2)) for m, _, lbl, fwd in out}
    assert d["A"] == (1, 1.0) and d["B"] == (0, 0.1)
    assert "C" not in d                                   # no horizon price -> dropped, never fabricated


def test_fixed_horizon_labels_death_aware():
    # P2: a token with NO price near the horizon is normally dropped — but if we WITNESSED it
    # collapse by >= death_drop within the window, that is a real logged loser -> label 0.
    from memebot.backtest.label import fixed_horizon_labels
    f = {"liquidity_usd": 1.0}
    rows = [
        ("DIE", 0.0, 1.0, f), ("DIE", 120.0, 0.05, f),   # -95% at +2m, then vanishes (no +5m price)
        ("OK",  0.0, 1.0, f), ("OK", 300.0, 1.2, f),     # has a +5m price -> labeled normally (+20% < up)
    ]
    # without death_drop: DIE is dropped (no horizon price)
    base = fixed_horizon_labels(rows, horizon_s=300.0, up=0.5, tol_s=60.0)
    assert {m for m, _, _, _ in base} == {"OK"}
    # with death_drop=0.9: the witnessed -95% collapse rescues DIE as a label-0 negative
    da = {m: lbl for m, _, lbl, _ in fixed_horizon_labels(rows, 300.0, up=0.5, tol_s=60.0, death_drop=0.9)}
    assert da["DIE"] == 0 and da["OK"] == 0


def test_fixed_horizon_labels_indexed_only():
    # P2: indexed_only keeps only in-distribution snapshots (liquidity_usd>0) — the population
    # the live GBM actually scores. A pre-index curve-only mint (liquidity 0) is excluded.
    from memebot.backtest.label import fixed_horizon_labels
    indexed, curve = {"liquidity_usd": 5000.0}, {"liquidity_usd": 0.0}
    rows = [
        ("IDX", 0.0, 1.0, indexed), ("IDX", 300.0, 2.0, indexed),   # indexed, +100% -> positive
        ("CRV", 0.0, 1.0, curve),   ("CRV", 300.0, 2.0, curve),     # curve-only -> excluded
    ]
    out = {m for m, _, _, _ in fixed_horizon_labels(rows, 300.0, up=0.5, tol_s=60.0, indexed_only=True)}
    assert out == {"IDX"}


def test_observations_table_decoupled_from_gate():
    # P2: observations record EVERY indexed snapshot regardless of the rule gate, and
    # _load_all_obs prefers that unbiased table over the survivor-only `candidates` table.
    d = tempfile.mkdtemp()
    db = os.path.join(d, "t.db")
    st = Storage(db); st.connect()

    def obs(mint, price, liq, passed):
        c = Candidate(mint=mint, symbol=mint)
        c.price_usd, c.liquidity_usd, c.market_cap_usd = price, liq, liq * 6
        c.features["vol_h1"] = 1234.0
        c.rule_passed, c.score = passed, 0.4
        return c

    # a gate FAILER (rule_passed=0) is logged just like a passer — the whole point of P2
    asyncio.run(st.log_observation(obs("FAIL", 1.0, 5000.0, False), json.dumps({"liquidity_usd": 5000.0})))
    asyncio.run(st.log_observation(obs("PASS", 1.0, 9000.0, True), json.dumps({"liquidity_usd": 9000.0})))
    st.close()

    with sqlite3.connect(db) as conn:
        n_obs = conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
        n_fail = conn.execute("SELECT COUNT(*) FROM observations WHERE rule_passed=0").fetchone()[0]
    assert n_obs == 2 and n_fail == 1                      # the gate-failer IS recorded

    from memebot.backtest.label import _load_all_obs
    mints = {m for m, _ts, _p, _f in _load_all_obs(db)}
    assert mints == {"FAIL", "PASS"}                       # loader reads the unbiased table


def test_trade_outcome_row_persists():
    # P2: a closed trade's entry features -> realized PnL is the cleanest in-distribution row.
    d = tempfile.mkdtemp()
    db = os.path.join(d, "t.db")
    st = Storage(db); st.connect()
    ct = ClosedTrade(mint="m", symbol="X", mode="scalp", cost_sol=0.5, proceeds_sol=0.7,
                     pnl_sol=0.2, pnl_pct=0.4, opened_ts=100.0, closed_ts=160.0, reason="tp",
                     entry_features={"vol_h1": 2500.0}, entry_score=0.62)
    asyncio.run(st.log_trade_outcome(ct))
    st.close()
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT mint, entry_score, pnl_sol, hold_s, entry_features FROM trade_outcomes"
        ).fetchone()
    assert row[0] == "m" and abs(row[1] - 0.62) < 1e-9 and abs(row[2] - 0.2) < 1e-9
    assert abs(row[3] - 60.0) < 1e-9                       # hold_s = closed - opened
    assert json.loads(row[4])["vol_h1"] == 2500.0


def test_concentration_split_and_verdict():
    from memebot.backtest.concentration_study import concentration_split, verdict
    # losers clearly MORE concentrated than winners -> SEPARATES
    rows = ([(20.0, 0.1), (30.0, 0.2), (25.0, 0.05), (40.0, 0.3), (35.0, 0.1)]      # 5 winners ~20-40
            + [(90.0, -0.2), (95.0, -0.3), (92.0, -0.1), (88.0, -0.15), (96.0, -0.4)])  # 5 losers ~88-96
    r = concentration_split(rows)
    assert r["winners"] == 5 and r["losers"] == 5 and r["separation"] > 5.0
    assert "SEPARATES" in verdict(r)
    # too few of a class -> INCONCLUSIVE (don't re-tune the veto)
    assert "INCONCLUSIVE" in verdict(concentration_split([(50.0, 0.1), (60.0, -0.1)]))
    # overlapping concentration -> NO clean separation
    overlap = [(85.0, 0.1)] * 5 + [(86.0, -0.1)] * 5
    assert "NO clean separation" in verdict(concentration_split(overlap))


def test_winner_characteristics():
    from memebot.backtest.winners import winner_characteristics
    labeled = [
        ("a", {"buy_sell_ratio": 3.0, "liquidity_usd": 10000}, 1, 1.0),
        ("b", {"buy_sell_ratio": 2.5, "liquidity_usd": 12000}, 1, 0.8),
        ("c", {"buy_sell_ratio": 1.0, "liquidity_usd": 9000}, 0, -0.2),
        ("d", {"buy_sell_ratio": 1.2, "liquidity_usd": 11000}, 0, 0.1),
    ]
    chars, nw, nl = winner_characteristics(labeled)
    assert nw == 2 and nl == 2
    assert chars["buy_sell_ratio"]["winner_median"] > chars["buy_sell_ratio"]["loser_median"]
    assert chars["buy_sell_ratio"]["lift"] > 1.0       # winners run with stronger buy pressure


def test_gbm_deploy_gate():
    from memebot.backtest.retrain import should_deploy
    assert should_deploy(0.70, 0.60, 0.60) is True        # beats deployed, above floor
    assert should_deploy(0.55, 0.60, 0.60) is False        # worse than deployed -> keep
    assert should_deploy(0.65, 0.0, 0.60) is True          # nothing deployed, clears floor
    assert should_deploy(0.58, 0.0, 0.60) is False         # below floor
    assert should_deploy(0.79, 0.79, 0.60) is True         # equal -> redeploy (>=)


def test_precision_over_base():
    # P4: precision among predicted-positives vs the positive base rate (rare-event gate input).
    from memebot.backtest.retrain import precision_over_base
    y = [1, 1, 0, 0, 1, 0]                                   # base rate 3/6 = 0.5
    s = [0.9, 0.8, 0.7, 0.2, 0.1, 0.05]
    p, b, n = precision_over_base(y, s, 0.75)               # >=0.75 -> first two, both label 1
    assert n == 2 and abs(p - 1.0) < 1e-9 and abs(b - 0.5) < 1e-9
    p2, b2, n2 = precision_over_base(y, s, 0.0)             # all predicted-positive -> precision == base
    assert n2 == 6 and abs(p2 - 0.5) < 1e-9
    assert precision_over_base([], [], 0.5) == (0.0, 0.0, 0)   # empty -> no crash


def test_rug_timing():
    from memebot.backtest.rug_timing import mint_timing, timestop_sweep
    # a rug: falls >=80% below entry at age 20 (first crossing), even after a pump
    assert mint_timing([(0.0, 1.0), (10.0, 2.0), (20.0, 0.1)], rug_drop=0.8, up=0.5) == ("rug", 20.0)
    assert mint_timing([(0.0, 1.0), (10.0, 0.5), (20.0, 0.1)], 0.8, 0.5) == ("rug", 20.0)   # 0.5 isn't -80%, 0.1 is
    # a winner: peaks at +60% at age 10, never rugs
    assert mint_timing([(0.0, 1.0), (10.0, 1.6), (20.0, 1.3)], 0.8, 0.5) == ("winner", 10.0)
    assert mint_timing([(0.0, 1.0), (10.0, 1.1)], 0.8, 0.5) == ("other", None)               # neither
    assert mint_timing([(0.0, 1.0)], 0.8, 0.5) == ("other", None)                            # too few obs
    assert mint_timing([(0.0, 0.0), (10.0, 1.0)], 0.8, 0.5) == ("other", None)               # bad entry price
    # time-stop sweep: exiting at T dodges rugs that collapse AFTER T, cuts winners peaking AFTER T
    sweep = timestop_sweep([100.0, 500.0], [50.0, 300.0], [200.0])
    assert sweep[0]["rugs_dodged"] == 0.5 and sweep[0]["winners_cut"] == 0.5 and sweep[0]["net"] == 0.0
    assert timestop_sweep([], [10.0], [5.0])[0]["rugs_dodged"] == 0.0   # no rugs -> 0, no crash


def test_dodge_tradeoff():
    from memebot.backtest.rug_model import dodge_tradeoff
    rugs = [0.9, 0.8, 0.6, 0.4, 0.2]        # 5 rugs' P(rug)
    winners = [0.7, 0.3, 0.1, 0.1]          # 4 winners' P(rug)
    t = {r["threshold"]: r for r in dodge_tradeoff(rugs, winners, [0.5, 0.75])}
    assert abs(t[0.5]["rugs_dodged"] - 3 / 5) < 1e-9        # 0.9,0.8,0.6 >= 0.5 -> 60% of rugs dodged
    assert abs(t[0.5]["winners_lost"] - 1 / 4) < 1e-9       # only 0.7 >= 0.5 -> 25% of winners lost
    assert abs(t[0.75]["rugs_dodged"] - 2 / 5) < 1e-9 and t[0.75]["winners_lost"] == 0.0
    assert dodge_tradeoff([], [], [0.5])[0] == {"threshold": 0.5, "rugs_dodged": 0.0, "winners_lost": 0.0}


def test_rug_win_labels():
    from memebot.backtest.rug_model import rug_label, win_label
    assert rug_label("rug") == 1 and rug_label("dead") == 1            # both = capital you lose -> AVOID
    assert rug_label("winner") == 0 and rug_label("fade") == 0 and rug_label("flat") == 0
    assert rug_label("loser") == 0 and rug_label("glitch") == 0       # glitch isn't the avoid class
    assert win_label("winner") == 1 and win_label("fade") == 0 and win_label("rug") == 0


def test_gbm_stability_pure():
    from memebot.backtest.gbm_stability import fold_cuts, summarize_stability
    # expanding-window folds: train grows, val slides, never peeks ahead; train end <= val start
    cuts = fold_cuts(100, folds=3, min_train=40, min_val=10)
    assert cuts and all(cut < end <= 100 for cut, end in cuts)
    assert cuts[0][0] == 40                                  # first fold trains on the min prefix
    assert all(b[0] >= a[0] for a, b in zip(cuts, cuts[1:]))  # train end is non-decreasing (expanding)
    assert fold_cuts(30, folds=5) == []                     # too few rows -> no folds
    # verdicts
    assert summarize_stability([])["verdict"] == "no-data"
    assert summarize_stability([0.61, 0.58, 0.57])["verdict"] == "STABLE_EDGE"   # mean>=.55 & min>=.5
    assert summarize_stability([0.50, 0.48, 0.52])["verdict"] == "NOISE"         # mean<=.55 ~ chance
    assert summarize_stability([0.70, 0.45, 0.62])["verdict"] == "INCONCLUSIVE"  # good mean, a sub-chance fold
    s = summarize_stability([0.6, 0.4])
    assert abs(s["mean"] - 0.5) < 1e-9 and s["min"] == 0.4 and s["max"] == 0.6


def test_balanced_params():
    from memebot.backtest.retrain import balanced_params
    base = {"objective": "binary", "num_leaves": 31}
    y = [1, 0, 0, 0, 0]                                          # 1 pos / 4 neg -> scale_pos_weight = 4
    p = balanced_params(base, y, balance=True)
    assert p["scale_pos_weight"] == 4.0 and p["objective"] == "binary"
    assert "scale_pos_weight" not in base                       # never mutates the base dict
    assert "scale_pos_weight" not in balanced_params(base, y, balance=False)   # disabled -> no correction
    assert "scale_pos_weight" not in balanced_params(base, [1, 1, 1], balance=True)  # single-class -> no-op
    assert "scale_pos_weight" not in balanced_params(base, [0, 0, 0], balance=True)


def test_should_deploy_precision_gate():
    # P4: AUC-only path stays backward-compatible; the precision gate refuses a no-edge operating point.
    from memebot.backtest.retrain import should_deploy
    assert should_deploy(0.70, 0.60, 0.60) is True          # AUC-only (unchanged)
    assert should_deploy(0.55, 0.60, 0.60) is False
    # AUC clears but precision == base rate (no edge) -> refuse
    assert should_deploy(0.75, 0.0, 0.6, precision=0.30, base_rate=0.30,
                         min_precision_lift=0.05, n_pred_pos=50) is False
    # precision beats base rate by the margin -> deploy
    assert should_deploy(0.75, 0.0, 0.6, precision=0.40, base_rate=0.30,
                         min_precision_lift=0.05, n_pred_pos=50) is True
    # too few predicted-positives -> precision unreliable -> refuse even if it looks great
    assert should_deploy(0.75, 0.0, 0.6, precision=0.90, base_rate=0.30,
                         min_precision_lift=0.05, n_pred_pos=3) is False


def test_dataquality_report():
    from memebot.backtest.dataquality import report, trainable
    f = {"liquidity_usd": 1.0}
    rows = [
        ("A", 0.0, 1.0, f), ("A", 1800.0, 2.0, f),    # tracked 30m, +100% -> positive
        ("B", 0.0, 1.0, f), ("B", 1800.0, 0.5, f),    # tracked 30m, -50% -> negative
        ("C", 0.0, 1.0, f),                            # single obs -> not labelable
    ]
    r = report(rows)
    assert r["observations"] == 5 and r["distinct_mints"] == 3
    assert r["tracked_ge_30m"] == 2 and r["labelable_30m"] == 2 and r["pos_rate_30m"] == 0.5
    assert trainable(r) is False                                  # ~0 labelable at 5m -> not enough
    assert trainable({"labelable_5m": 300, "pos_rate_5m": 0.4}) is True

    # P2: in-distribution view — all rows are indexed (liquidity 1.0); none have a 5m price here
    assert r["indexed_observations"] == 5 and r["indexed_mints"] == 3
    assert r["labelable_5m_indexed"] == 0 and r["pos_5m_indexed"] == 0 and r["neg_5m_indexed"] == 0
    # P2: trainable() prefers the indexed both-class count when present
    assert trainable({"labelable_5m_indexed": 300, "pos_rate_5m_indexed": 0.3}) is True
    assert trainable({"labelable_5m_indexed": 300, "pos_rate_5m_indexed": 0.95}) is False  # one-class

    # a 5m-indexed scenario WITH both classes (death-aware): one +100% win, one witnessed rug
    idx = {"liquidity_usd": 1000.0}
    rows2 = [("W", 0.0, 1.0, idx), ("W", 300.0, 2.0, idx),        # +100% at +5m -> positive
             ("R", 0.0, 1.0, idx), ("R", 120.0, 0.02, idx)]       # -98% witnessed, no +5m -> death 0
    r2 = report(rows2)
    assert r2["labelable_5m_indexed"] == 2 and r2["pos_5m_indexed"] == 1 and r2["neg_5m_indexed"] == 1


def test_threshold_grid():
    from memebot.backtest.sweep import threshold_grid
    assert threshold_grid(0.4, 0.8, 0.1) == [0.4, 0.5, 0.6, 0.7, 0.8]
    assert threshold_grid(0.5, 0.5, 0.1) == [0.5]
    assert threshold_grid(0.55, 0.75, 0.05) == [0.55, 0.6, 0.65, 0.7, 0.75]


def test_classify_outcome_taxonomy():
    from memebot.backtest.dataset import classify_outcome
    # a witnessed collapse is a RUG even if it pumped first / lost its price
    assert classify_outcome(peak_ret=2.0, trough_ret=-0.85, final_ret=0.0, priced_at_horizon=False) == "rug"
    # no horizon price + no collapse -> DEAD (untracked), never fabricated
    assert classify_outcome(0.1, -0.3, 0.0, False) == "dead"
    # pumped >= +up and HELD >= win_hold -> winner; gave it back -> fade
    assert classify_outcome(0.6, -0.1, 0.30, True) == "winner"
    assert classify_outcome(0.6, -0.1, 0.05, True) == "fade"
    # no pump, faded to <= -loss_band -> loser; small move -> flat
    assert classify_outcome(0.1, -0.3, -0.40, True) == "loser"
    assert classify_outcome(0.1, -0.1, -0.05, True) == "flat"


def test_exitlab_replay_and_evaluate():
    from memebot.backtest.exitlab import default_variants, evaluate, replay_exit
    from memebot.portfolio.portfolio import ExitParams
    ep = ExitParams()
    # N2: a partial (partial_tp_pct=0.15, WL2 frac=0.7) banks 0.7 at +20% then the rest TPs at +50% ->
    # qty-weighted multiple = 0.7*1.2 + 0.3*1.5 = 1.29 (NOT the single-leg 1.5).
    reason, mult, _ = replay_exit([(0.0, 1.0), (2.0, 1.2), (4.0, 1.5)], "scalp", ep)
    assert reason == "tp" and abs(mult - 1.29) < 1e-9
    assert replay_exit([(0.0, 1.0), (2.0, 0.9), (4.0, 0.75)], "scalp", ep)[0] == "sl"          # -25% -> scalp SL
    assert replay_exit([(0.0, 1.0), (2.0, 1.01), (4.0, 1.0)], "scalp", ep)[0] == "end"         # flat -> never exits
    assert replay_exit([(0.0, 0.0), (2.0, 1.0)], "scalp", ep)[0] == "skip"                     # bad entry price
    paths = [("A", "scalp", [(0.0, 1.0), (2.0, 1.5)]), ("B", "scalp", [(0.0, 1.0), (2.0, 0.75)])]
    res = evaluate(paths, ep, rtc=0.1)
    assert res["n"] == 2 and res["win_rate"] == 0.5            # one TP win (+0.4 net), one SL loss
    assert "tp" in res["reasons"] and "sl" in res["reasons"]
    assert "baseline" in default_variants(ep) and len(default_variants(ep)) >= 5  # baseline + perturbations


def test_exitlab_scale_out_legs():
    from memebot.backtest.exitlab import evaluate, replay_exit
    from dataclasses import replace
    from memebot.portfolio.portfolio import ExitParams
    ep = ExitParams()
    # A partial banked at +20% CUSHIONS a subsequent crash: WL2 default partial_tp_frac=0.7 sold at 1.2,
    # the armed be-trail exits the rest off the 1.2 peak at 0.7 -> 0.7*1.2 + 0.3*0.7 = 1.05 (vs 0.70 no partial).
    crash = [(0.0, 1.0), (2.0, 1.2), (4.0, 0.7)]
    reason, mult, _ = replay_exit(crash, "scalp", ep)
    assert reason == "be_trail" and abs(mult - 1.05) < 1e-9
    # no_partial isolates it: a single leg, the armed be-trail exits at 0.7 -> 0.70 (the partial helps).
    no_partial = replace(ep, partial_tp_frac=0.0)
    assert abs(replay_exit(crash, "scalp", no_partial)[1] - 0.70) < 1e-9
    # N5 proactive derisk: a +35% spike (below the 40% scalp TP, so no full exit) then a crash. Baseline
    # takes only the P3 half-partial; the proactive derisk recovers the WHOLE principal -> nets more.
    # Isolate it from the WL7 take-initial (now 1.3x, which would itself fire at +35% and erase the gap).
    base = replace(ep, take_initial_pct=0.0)
    spike = [(0.0, 1.0), (2.0, 1.35), (4.0, 0.5)]
    derisk = replace(base, derisk_proactive_pct=0.30)
    base_mult = replay_exit(spike, "scalp", base, sell_cost=0.05)[1]
    drk_mult = replay_exit(spike, "scalp", derisk, sell_cost=0.05)[1]
    assert drk_mult > base_mult                               # principal-recovery banked more before the dump


def test_fill_divergence():
    from memebot.backtest.fill_divergence import _idx_at_or_before, fill_divergence, summarize
    series = {
        "A": [(0.0, 1.0), (1.0, 1.1), (2.0, 1.2)],     # rising -> a buy here fills WORSE live
        "B": [(0.0, 1.0), (1.0, 0.9), (2.0, 0.8)],     # falling -> a sell here fills WORSE live
    }
    assert _idx_at_or_before(series["A"], 0.5) == 0     # last obs at/before 0.5 is index 0
    assert _idx_at_or_before(series["A"], 5.0) == 2     # after the end -> last index
    assert _idx_at_or_before(series["A"], -1.0) is None # before the start -> None
    trades = [(0.0, "A", "buy", 0.001), (0.0, "B", "sell", 0.001)]
    div = fill_divergence(trades, series, polls=2)
    assert div["matched"] == 2
    assert abs(div["buys"][0] - 0.2) < 1e-9 and abs(div["sells"][0] - (-0.2)) < 1e-9
    r = summarize(div, rtc=0.095)
    assert abs(r["buy_adverse"] - 0.2) < 1e-9           # price rose after buy -> +adverse
    assert abs(r["sell_adverse"] - 0.2) < 1e-9          # price fell after sell -> +adverse
    assert abs(r["extra_round_trip_cost"] - 0.4) < 1e-9 # both legs adverse
    assert abs(r["eats_cushion_frac"] - 0.4 / 0.095) < 1e-9
    # a trade with no price far enough ahead is unmatched (never fabricated)
    assert fill_divergence([(2.0, "A", "buy", 0.001)], series, polls=2)["matched"] == 0
    assert fill_divergence([(0.0, "Z", "buy", 0.001)], series, polls=2)["matched"] == 0  # mint not observed
    assert summarize({"buys": [], "sells": []}, rtc=0.095)["extra_round_trip_cost"] == 0.0


def test_pnl_reconstruct_flags_glitch_and_book():
    from memebot.backtest.pnl_reconstruct import reconstruct, summarize
    trades = [
        ("WIN", "buy", 0.5, 1000.0), ("WIN", "sell", 1.5, 1000.0),        # +1.0 clean (3x)
        ("LOSS", "buy", 0.5, 1000.0), ("LOSS", "sell", 0.3, 1000.0),      # -0.2
        ("GLITCH", "buy", 0.5, 1000.0), ("GLITCH", "sell", 100.0, 1000.0),  # +99.5 (200x) = glitch
        ("OPEN", "buy", 0.5, 1000.0), ("OPEN", "sell", 0.3, 400.0),       # still open -> excluded
    ]
    rows = reconstruct(trades, glitch_mult=20.0)
    by = {r["mint"]: r for r in rows}
    assert set(by) == {"WIN", "LOSS", "GLITCH"}                           # OPEN excluded (not fully closed)
    assert by["GLITCH"]["glitch"] is True and by["WIN"]["glitch"] is False
    s = summarize(rows)
    assert s["all"]["n"] == 3 and abs(s["all"]["net"] - 100.3) < 1e-9     # incl. glitch (the fake number)
    assert s["clean"]["n"] == 2 and abs(s["clean"]["net"] - 0.8) < 1e-9   # the number to trust
    assert s["clean"]["wins"] == 1 and s["clean"]["losses"] == 1
    assert len(s["glitches"]) == 1 and s["glitches"][0]["mint"] == "GLITCH"
    assert abs(s["winsorized"]["net"] - 1.8) < 1e-9                       # glitch pnl capped at median winner (1.0)
    assert summarize([]) == summarize([])  # no crash on empty (smoke)


def test_classify_outcome_glitch_quarantine():
    from memebot.backtest.dataset import classify_outcome
    # an absurd forward return (PEPTI-style +511x) is a PRICING GLITCH, NOT a winner -> quarantined
    assert classify_outcome(peak_ret=511.0, trough_ret=-0.1, final_ret=511.0, priced_at_horizon=True) == "glitch"
    assert classify_outcome(25.0, -0.1, 0.0, True) == "glitch"          # peak alone >= cap
    assert classify_outcome(2.0, -0.1, 1.5, True, glitch_cap=20.0) == "winner"   # real big winner kept
    assert classify_outcome(511.0, -0.1, 511.0, True, glitch_cap=0.0) == "winner"  # 0 disables the guard


def test_forward_outcomes_peak_trough_final():
    from memebot.backtest.dataset import forward_outcomes
    f = {"liquidity_usd": 1.0}
    rows = [
        ("WIN", 0.0, 1.0, f), ("WIN", 150.0, 2.0, f), ("WIN", 300.0, 1.8, f),   # peak +100%, final +80% @300
        ("RUG", 0.0, 1.0, f), ("RUG", 120.0, 0.05, f),                          # -95% @120, no price near 300
    ]
    recs = {r["mint"]: r for r in forward_outcomes(rows, horizon_s=300.0, tol_s=60.0)}
    assert abs(recs["WIN"]["peak_ret"] - 1.0) < 1e-9 and abs(recs["WIN"]["final_ret"] - 0.8) < 1e-9
    assert recs["WIN"]["priced_at_horizon"] is True
    assert abs(recs["RUG"]["trough_ret"] + 0.95) < 1e-9 and recs["RUG"]["priced_at_horizon"] is False


def test_single_feature_auc():
    from memebot.backtest.separation import single_feature_auc
    assert single_feature_auc([3, 4, 5], [0, 1, 2]) == 1.0     # winners all higher -> perfect
    assert single_feature_auc([0, 1, 2], [3, 4, 5]) == 0.0     # winners all lower -> perfect (other way)
    assert single_feature_auc([1, 1], [1, 1]) == 0.5           # all ties -> no separation
    assert single_feature_auc([1, 3], [2, 4]) == 0.25          # interleaved: only (3>2) of 4 pairs
    assert single_feature_auc([], [1, 2]) == 0.5               # empty -> 0.5, no crash


def test_separation_report_and_verdict():
    from memebot.backtest.separation import separation_report, verdict
    classified = (
        [{"outcome": "winner", "features": {"vol_h1": 1000.0 + i}} for i in range(10)]
        + [{"outcome": "rug", "features": {"vol_h1": 100.0 + i}} for i in range(10)]
    )
    rep = separation_report(classified, min_per_class=8)
    assert rep["ready"] and rep["winners"] == 10 and rep["rugs"] == 10
    assert rep["features"][0]["feature"] == "vol_h1" and rep["features"][0]["auc"] == 1.0
    thin = [{"outcome": "winner", "features": {"vol_h1": 1.0}}] * 3 + [{"outcome": "rug", "features": {"vol_h1": 2.0}}] * 3
    assert separation_report(thin, min_per_class=8)["ready"] is False
    assert verdict(0.5, 20, 20) == "SEPARATES"                 # strong effect + enough n
    assert verdict(0.1, 20, 20) == "overlaps"                  # weak effect
    assert verdict(0.9, 5, 20) == "inconclusive (thin)"        # one class too thin


def test_separation_skips_unknown_sentinels():
    from memebot.backtest.separation import separation_report
    classified = (
        [{"outcome": "winner", "features": {"top5_concentration_pct": -1.0}} for _ in range(10)]
        + [{"outcome": "rug", "features": {"top5_concentration_pct": 95.0 + i}} for i in range(10)]
    )
    rep = separation_report(classified, min_per_class=8)
    # all winners are -1 (unknown) -> too few KNOWN winner values -> feature skipped, NOT a fake AUC
    assert all(f["feature"] != "top5_concentration_pct" for f in rep["features"])


def test_suggest_thresholds_separates_not_noise():
    from memebot.backtest.calibrate import suggest_thresholds
    summary = {"n": 20, "classes": {
        "winner": {"n": 10, "sell_pressure": 0.0, "top5_concentration_pct": 80.0,
                   "creator_launches": 5.0, "buy_sell_ratio": 2.0, "vol_h1": 5000.0, "liquidity_usd": 14000.0},
        "rug": {"n": 10, "sell_pressure": 0.6, "top5_concentration_pct": 95.0,
                "creator_launches": 60.0, "buy_sell_ratio": 0.8, "vol_h1": 2000.0, "liquidity_usd": 13000.0},
    }}
    sug = suggest_thresholds(summary, min_class_n=5, min_sep=0.2)
    feats = [x["feature"] for x in sug]
    assert sug[0]["feature"] == "sell_pressure"                  # strongest separation first
    assert "creator_launches" in feats and "buy_sell_ratio" in feats
    assert "top5_concentration_pct" not in feats and "liquidity_usd" not in feats   # overlapping -> noise, skipped
    assert "HIGHER" in next(x for x in sug if x["feature"] == "sell_pressure")["note"]
    assert "LOWER" in next(x for x in sug if x["feature"] == "buy_sell_ratio")["note"]
    # too few of either class -> no suggestions (don't calibrate on noise)
    thin = {"n": 4, "classes": {"winner": {"n": 2, "sell_pressure": 0.0}, "rug": {"n": 2, "sell_pressure": 0.9}}}
    assert suggest_thresholds(thin, min_class_n=5) == []


def test_suggest_thresholds_skips_inverted_direction():
    # sell_pressure danger is HIGHER in rugs; if our data shows rugs LOWER (inverted), don't act on it
    from memebot.backtest.calibrate import suggest_thresholds
    summary = {"n": 20, "classes": {
        "winner": {"n": 10, "sell_pressure": 0.6}, "rug": {"n": 10, "sell_pressure": 0.0}}}
    assert all(x["feature"] != "sell_pressure" for x in suggest_thresholds(summary, min_class_n=5))


def test_suggest_thresholds_skips_unknown_sentinel():
    # an unknown-data sentinel (-1, e.g. concentration-dark winners vs a few known-high rugs) must
    # NEVER fabricate the top-ranked "separation" suggestion. top5_concentration_pct is excluded from
    # _WATCH entirely, and the w<0/r<0 guard defends any other feature that carries the -1 encoding.
    from memebot.backtest.calibrate import suggest_thresholds
    assert all("concentration" not in x["feature"] for x in suggest_thresholds(
        {"n": 20, "classes": {"winner": {"n": 10, "top5_concentration_pct": -1.0},
                              "rug": {"n": 10, "top5_concentration_pct": 95.0}}}, min_class_n=5))
    # the guard: a watched feature carrying -1 for a class is skipped, not turned into a fake separator
    assert all(x["feature"] != "vol_h1" for x in suggest_thresholds(
        {"n": 20, "classes": {"winner": {"n": 10, "vol_h1": -1.0}, "rug": {"n": 10, "vol_h1": 5000.0}}},
        min_class_n=5))


def test_creator_reputation_aggregates():
    from memebot.backtest.calibrate import creator_reputation
    classified = [
        {"mint": "a", "outcome": "rug"}, {"mint": "b", "outcome": "rug"},
        {"mint": "c", "outcome": "winner"},                      # creator X: 3 tokens, 2 rug / 1 win
        {"mint": "d", "outcome": "flat"},                        # creator Y: 1 token -> below min_tokens
    ]
    mint_creator = {"a": "X", "b": "X", "c": "X", "d": "Y"}
    rep = creator_reputation(classified, mint_creator, min_tokens=3)
    assert "X" in rep and "Y" not in rep
    assert rep["X"]["n"] == 3 and abs(rep["X"]["rug_rate"] - 2 / 3) < 1e-9 and abs(rep["X"]["win_rate"] - 1 / 3) < 1e-9


def test_readiness():
    from memebot.backtest.calibrate import readiness
    r = readiness({"n": 100, "classes": {"winner": {"n": 35}, "rug": {"n": 31}}}, min_per_class=30)
    assert r["winners"] == 35 and r["rugs"] == 31 and r["ready"] is True
    assert readiness({"n": 50, "classes": {"winner": {"n": 10}, "rug": {"n": 40}}}, min_per_class=30)["ready"] is False
    assert readiness({})["ready"] is False                       # empty -> not ready, no crash


def test_build_dataset_classes_and_summary():
    from memebot.backtest.dataset import build_dataset, summarize
    f = {"liquidity_usd": 5000.0, "vol_h1": 3000.0}
    rows = [
        ("WIN", 0.0, 1.0, f), ("WIN", 300.0, 2.0, f),       # +100% held -> winner
        ("RUG", 0.0, 1.0, f), ("RUG", 120.0, 0.05, f),      # -95% witnessed -> rug
    ]
    recs = build_dataset(rows, horizon_s=300.0, tol_s=60.0)
    by = {r["mint"]: r["outcome"] for r in recs}
    assert by["WIN"] == "winner" and by["RUG"] == "rug"
    assert next(r for r in recs if r["mint"] == "WIN")["win"] == 1   # convenience binary
    s = summarize(recs)
    assert s["n"] == 2 and s["classes"]["winner"]["n"] == 1 and s["classes"]["rug"]["n"] == 1
    assert s["classes"]["winner"]["liquidity_usd"] == 5000.0         # per-class median feature
    assert summarize([]) == {}                                       # empty -> {}


def test_round_trip_cost():
    f = Fees()
    assert round_trip_cost_pct(f) == 2.0 * (f.pumpportal_pct + f.pumpfun_curve_pct + f.base_slippage_pct)


def test_candidate_from_features_roundtrip():
    c0 = Candidate(mint="m")
    c0.liquidity_usd, c0.mode = 5000.0, "hold"
    c0.mint_revoked, c0.freeze_revoked = True, None
    c0.top5_concentration_pct = 42.0
    c1 = candidate_from_features(build_features(c0), 0.001, mint="m")
    assert c1.liquidity_usd == 5000.0 and c1.mode == "hold"
    assert c1.mint_revoked is True and c1.freeze_revoked is None
    assert abs(c1.top5_concentration_pct - 42.0) < 1e-9


def test_row_from_feats_defaults_serve_sentinels():
    # an OLD logged row (pre-trajectory) missing the new keys must default to the SERVE sentinels
    # (-2 unknown for trend_dir/ema_signal, -1 for safety), NOT a blanket 0.0 (=real "choppy"/"flat").
    from memebot.filter.features import FEATURE_NAMES, row_from_feats
    row = dict(zip(FEATURE_NAMES, row_from_feats({"liquidity_usd": 5000.0, "vol_h1": 1000.0})))
    assert row["trend_dir"] == -2.0 and row["ema_signal"] == -2.0          # unknown, not choppy/flat
    assert row["mint_revoked"] == -1.0 and row["top5_concentration_pct"] == -1.0
    assert row["breakout"] == 0.0 and row["price_change_m5"] == 0.0        # serve-consistent
    assert row["liquidity_usd"] == 5000.0 and len(FEATURE_NAMES) == len(row)


def test_features_trajectory_and_velocity_roundtrip():
    # P8: the trajectory/velocity block is in FEATURE_NAMES, correctly encoded, and round-trips
    # through the backtest inverse so a future GBM sees identical vectors offline and live.
    from memebot.filter.features import FEATURE_NAMES, build_features, feature_vector
    c = Candidate(mint="m"); c.mode = "hold"
    c.price_change_m5, c.price_change_h1 = 12.5, -3.0
    c.features["trend"] = {"dir": "rising", "chg_pct": 8.0, "off_high_pct": 2.0}
    c.features["tech"] = {"ema_signal": "bull", "breakout": "up"}
    c.features["age_s"] = 600.0      # 10 minutes old
    c.features["vol_h1"] = 6000.0
    c.features["liq_trend"] = {"dir": "falling", "chg_pct": -30.0, "n": 6}   # P8: pool draining
    c.features["creator_holding_pct"] = 25.0                                  # P8: creator still holds
    c.features["sniper_share"] = 0.7                                          # G3 tape: sniper-dominated
    c.features["creator_dump_ratio"] = 0.4                                    # G3 tape: creator dumping
    c.features["bundle_share"] = 0.55                                         # G3 tape: coordinated bundles
    f = build_features(c)
    assert f["price_change_m5"] == 12.5 and f["price_change_h1"] == -3.0
    assert f["trend_dir"] == 1.0 and f["trend_chg_pct"] == 8.0 and f["off_high_pct"] == 2.0
    assert f["ema_signal"] == 1.0 and f["breakout"] == 1.0
    assert f["age_min"] == 10.0 and f["vol_per_min"] == 600.0          # 6000 / min(60, 10) = 600/min
    assert f["liq_trend_dir"] == -1.0 and f["liq_trend_chg_pct"] == -30.0   # draining
    assert f["creator_holding_pct"] == 25.0                            # creator skin-in-the-game
    assert f["sniper_share"] == 0.7 and f["creator_dump_ratio"] == 0.4  # G3 tape signals
    assert f["bundle_share"] == 0.55                                    # bundle detection (research #1)
    assert len(f) == len(FEATURE_NAMES) and all(k in f for k in FEATURE_NAMES)
    c2 = candidate_from_features(f, 1e-4, mint="m")
    assert feature_vector(c2) == feature_vector(c)                     # faithful inverse
    # an unknown/warming-up token encodes to the sentinels, not a misleading 0
    cu = Candidate(mint="u")
    fu = build_features(cu)
    assert fu["trend_dir"] == -2.0 and fu["ema_signal"] == -2.0 and fu["vol_per_min"] == 0.0
    assert fu["liq_trend_dir"] == -2.0 and fu["liq_trend_chg_pct"] == 0.0
    assert fu["creator_holding_pct"] == -1.0                           # unknown (not fetched) -> -1, not 0
    assert fu["sniper_share"] == -1.0 and fu["creator_dump_ratio"] == -1.0   # no tape -> -1, not 0
    assert fu["bundle_share"] == -1.0                                        # no tape -> -1, not 0


def _strong_features():
    c = Candidate(mint="a")
    c.liquidity_usd, c.vol_to_mcap_pct, c.buy_sell_ratio, c.unique_buyers = 20000, 40, 2.5, 60
    c.features["vol_spike"], c.mode = 3.0, "scalp"
    return build_features(c)


def test_simulate_winner():
    s = Settings()
    rows = [("a", 0.001, _strong_features())]
    prices = {"a": 0.002}                      # +100%
    res = simulate(rows, prices, Scorer(s, None), s, entry_threshold=0.0)
    assert res["entries"] == 1 and res["win_rate"] == 1.0
    assert res["total_net_return"] > 0         # +100% beats the round-trip cost
    assert res["scalp"]["entries"] == 1 and res["hold"]["entries"] == 0


def test_simulate_rugged_and_threshold():
    s = Settings()
    rows = [("a", 0.001, _strong_features())]
    # missing forward price = rugged -> -100% before costs -> loss
    res = simulate(rows, {}, Scorer(s, None), s, entry_threshold=0.0)
    assert res["entries"] == 1 and res["win_rate"] == 0.0 and res["total_net_return"] < 0
    # an impossibly high threshold lets nothing enter
    res2 = simulate(rows, {"a": 0.002}, Scorer(s, None), s, entry_threshold=2.0)
    assert res2["considered"] == 1 and res2["entries"] == 0


def test_simulate_min_ev_gate_reduces_entries():
    # D4: the optional min_expected_return buffer drops marginal-EV entries (score as conviction proxy).
    s = Settings()
    rows = [("a", 0.001, _strong_features())]
    prices = {"a": 0.002}
    base = simulate(rows, prices, Scorer(s, None), s, entry_threshold=0.0)
    assert base["entries"] == 1 and base["ev_skipped"] == 0            # no EV gate by default
    strict = simulate(rows, prices, Scorer(s, None), s, entry_threshold=0.0, min_expected_return=999.0)
    assert strict["entries"] == 0 and strict["ev_skipped"] == 1         # impossibly high buffer admits nothing


def test_ev_grid_reused_from_sweep():
    from memebot.backtest.evsweep import ev_grid
    assert ev_grid(0.0, 0.10, 0.02) == [0.0, 0.02, 0.04, 0.06, 0.08, 0.1]


def test_simulate_min_ev_gate_mixed_pass_and_skip():
    # D4: a buffer BETWEEN two candidates' EVs admits the stronger and skips the weaker — proves the
    # gate uses the real expected_return(score,...) math, not just the all-or-nothing extremes.
    from memebot.backtest.simulate import _ev_tp_sl, candidate_from_features
    from memebot.filter import rules
    from memebot.risk.limits import expected_return
    s = Settings()
    scorer = Scorer(s, None)
    rtc = round_trip_cost_pct(s.fees)
    strong = _strong_features()                          # vol_spike 3.0 -> higher score/EV
    weak = _strong_features(); weak["vol_spike"] = 0.0   # same gates, weaker momentum -> lower score/EV

    def ev_of(feats):
        c = candidate_from_features(feats, 0.001); rules.evaluate(c, s); scorer.score(c)
        tp, sl = _ev_tp_sl(c.mode, s.exit)
        return expected_return(c.score, tp, sl, rtc)
    eva, evb = ev_of(strong), ev_of(weak)
    assert eva > evb                                     # stronger momentum -> higher EV
    buf = (eva + evb) / 2.0                               # a buffer strictly between the two
    rows = [("a", 0.001, strong), ("b", 0.001, weak)]
    res = simulate(rows, {"a": 0.002, "b": 0.002}, scorer, s, entry_threshold=0.0, min_expected_return=buf)
    assert res["entries"] == 1 and res["ev_skipped"] == 1   # strong admitted, weak gated out


def test_ev_tp_sl_mirrors_try_open():
    # D4: the EV gate's tp/sl must mirror Bot._try_open — a HOLD uses the TRAIL-capped tp, never the
    # unrealizable +300% hold_tp ceiling.
    from memebot.backtest.simulate import _ev_tp_sl
    ep = Settings().exit
    assert _ev_tp_sl("scalp", ep) == (ep.scalp_tp_pct, ep.scalp_sl_pct)
    assert _ev_tp_sl("hold", ep) == (min(ep.hold_tp_pct, ep.hold_trail_pct), ep.hold_sl_pct)
    assert _ev_tp_sl("hold", ep)[0] < ep.hold_tp_pct        # the trail cap actually bit


def test_pick_best_ev_respects_min_entries_and_avg_net():
    from memebot.backtest.evsweep import pick_best_ev
    results = [
        (0.0,  {"entries": 20, "avg_net_return": 0.05, "win_rate": 0.50}),
        (0.02, {"entries": 15, "avg_net_return": 0.12, "win_rate": 0.60}),   # best avg net w/ enough sample
        (0.04, {"entries": 3,  "avg_net_return": 0.40, "win_rate": 1.00}),   # higher avg but too few entries
    ]
    best = pick_best_ev(results, min_entries=10)
    assert best[0] == 0.02 and abs(best[1] - 0.12) < 1e-9     # 0.04 ignored (3 < 10 entries)
    assert pick_best_ev([(0.0, {"entries": 2, "avg_net_return": 0.5, "win_rate": 1.0})], 10) is None


def test_scenario_moon_scalp_takes_profit():
    from memebot.backtest.scenarios import SCENARIOS, run_scenario
    from memebot.constants import MODE_SCALP
    from memebot.portfolio.portfolio import ExitParams
    moon = dict(SCENARIOS)["moon"]
    r = run_scenario(moon, MODE_SCALP, ExitParams(), rt_cost=0.0)
    assert r["reason"] == "tp" and abs(r["exit_mult"] - 1.6) < 1e-9


def test_overtightening_whipsaws_a_fakeout():
    # the lesson behind the defensive profile: an over-tight SL gets whipsawed out
    # of a winner, so capital preservation comes from SIZE, not tighter stops.
    from memebot.backtest.scenarios import SCENARIOS, overtightened_params, run_scenario
    from memebot.constants import MODE_SCALP
    from memebot.portfolio.portfolio import ExitParams
    fakeout = dict(SCENARIOS)["fakeout_recover"]
    base = ExitParams()                       # default scalp_sl 0.18 survives the -12% dip
    tight = overtightened_params(base)        # scalp_sl 0.12 is stopped out on that dip
    rb = run_scenario(fakeout, MODE_SCALP, base, rt_cost=0.0)
    rt = run_scenario(fakeout, MODE_SCALP, tight, rt_cost=0.0)
    assert rb["reason"] == "tp" and rb["net_pct"] > 0      # rides the dip to the +50% recovery
    assert rt["reason"] == "sl" and rt["net_pct"] < 0      # whipsawed out for a loss
