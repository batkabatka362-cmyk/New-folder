"""PnL / performance metrics over closed trades + equity samples."""
from __future__ import annotations

from dataclasses import dataclass

from .portfolio import ClosedTrade


@dataclass(slots=True)
class Stats:
    n_trades: int
    wins: int
    losses: int
    win_rate: float
    total_pnl_sol: float
    avg_pnl_sol: float
    profit_factor: float
    best_sol: float
    worst_sol: float
    avg_win_pct: float
    avg_loss_pct: float

    def as_line(self) -> str:
        return (
            f"trades={self.n_trades} win%={self.win_rate*100:.1f} "
            f"pnl={self.total_pnl_sol:+.4f} SOL pf={self.profit_factor:.2f} "
            f"best={self.best_sol:+.3f} worst={self.worst_sol:+.3f}"
        )


def compute_stats(closed: list[ClosedTrade]) -> Stats:
    if not closed:
        return Stats(0, 0, 0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    pnls = [t.pnl_sol for t in closed]
    wins = [t for t in closed if t.pnl_sol > 0]
    losses = [t for t in closed if t.pnl_sol < 0]   # break-even (==0) counts as neither
    gross_win = sum(t.pnl_sol for t in wins)
    gross_loss = -sum(t.pnl_sol for t in losses)
    pf = (gross_win / gross_loss) if gross_loss > 0 else float("inf")
    return Stats(
        n_trades=len(closed),
        wins=len(wins),
        losses=len(losses),
        win_rate=len(wins) / len(closed),
        total_pnl_sol=sum(pnls),
        avg_pnl_sol=sum(pnls) / len(pnls),
        profit_factor=pf,
        best_sol=max(pnls),
        worst_sol=min(pnls),
        avg_win_pct=(sum(t.pnl_pct for t in wins) / len(wins)) if wins else 0.0,
        avg_loss_pct=(sum(t.pnl_pct for t in losses) / len(losses)) if losses else 0.0,
    )


def max_drawdown(equity_series: list[float]) -> float:
    """Largest peak-to-trough drop as a fraction (0..1)."""
    peak = float("-inf")
    mdd = 0.0
    for e in equity_series:
        peak = max(peak, e)
        if peak > 0:
            mdd = max(mdd, (peak - e) / peak)
    return mdd
