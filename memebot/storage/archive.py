"""Parquet export of the SQLite tables for offline training & analysis.

Snapshot export (overwrites {archive_dir}/{table}.parquet each run). Degrades
gracefully when pyarrow isn't installed — logs once and skips, never crashes.

CLI:  python -m memebot.storage.archive
"""
from __future__ import annotations

import os
import sqlite3

from ..utils.logging import get_logger

log = get_logger("storage.archive")


def _rows_to_columns(cols: list[str], rows: list[tuple]) -> dict[str, list]:
    """Column-oriented dict for pyarrow (pure, testable without pyarrow)."""
    return {c: [r[i] for r in rows] for i, c in enumerate(cols)}


class ParquetArchiver:
    TABLES = ("candidates", "trades", "equity", "lessons", "verdicts",
              "observations", "trade_outcomes")   # P2: export the in-distribution training data too

    def __init__(self, db_path: str, out_dir: str = "archive") -> None:
        self.db_path = db_path
        self.out_dir = out_dir

    @staticmethod
    def available() -> bool:
        try:
            import pyarrow  # noqa: F401
            return True
        except ImportError:
            return False

    def _export_table(self, conn: sqlite3.Connection, table: str, path: str) -> int:
        import pyarrow as pa
        import pyarrow.parquet as pq

        cur = conn.execute(f"SELECT * FROM {table}")          # table names are our constants, not user input
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
        if not rows:
            return 0
        # atomic write: a mid-write failure must not destroy the prior good snapshot
        tmp = f"{path}.{os.getpid()}.tmp"
        try:
            pq.write_table(pa.table(_rows_to_columns(cols, rows)), tmp)
            os.replace(tmp, path)                              # atomic on same filesystem
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        return len(rows)

    def export(self) -> dict[str, int]:
        if not self.available():
            log.warning("pyarrow not installed -> Parquet archival skipped (pip install pyarrow)")
            return {}
        os.makedirs(self.out_dir, exist_ok=True)
        out: dict[str, int] = {}
        conn = sqlite3.connect(self.db_path)
        try:
            for table in self.TABLES:
                try:
                    out[table] = self._export_table(conn, table, os.path.join(self.out_dir, f"{table}.parquet"))
                except Exception as e:  # noqa: BLE001 — one bad table shouldn't abort the rest
                    log.warning("archive %s failed: %s", table, e)
        finally:
            conn.close()
        return out


def main() -> None:
    from ..config import get_settings

    s = get_settings()
    res = ParquetArchiver(s.db_path, s.archive_dir).export()
    print(f"exported rows: {res}" if res else "nothing exported (pyarrow missing or no data)")


if __name__ == "__main__":
    main()
