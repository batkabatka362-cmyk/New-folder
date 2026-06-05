"""Parquet archiver: pure column transform + round-trip-or-graceful-skip."""
from __future__ import annotations

import asyncio
import os
import tempfile

from memebot.storage.archive import ParquetArchiver, _rows_to_columns
from memebot.storage.db import Storage


def test_rows_to_columns():
    assert _rows_to_columns(["a", "b"], [(1, 2), (3, 4)]) == {"a": [1, 3], "b": [2, 4]}
    assert _rows_to_columns(["a"], []) == {"a": []}


def test_available_is_bool():
    assert isinstance(ParquetArchiver.available(), bool)


def test_export_roundtrip_or_skip():
    d = tempfile.mkdtemp()
    db = os.path.join(d, "t.db")
    st = Storage(db)
    st.connect()
    asyncio.run(st.log_trade(mint="m", symbol="X", side="buy", mode="scalp", sol=0.5,
                             tokens=1000, price_sol=5e-4, fee_sol=0.01, slippage_pct=0.03, reason="t"))
    st.close()

    res = ParquetArchiver(db, os.path.join(d, "arch")).export()
    if ParquetArchiver.available():
        assert res.get("trades", 0) == 1
        assert os.path.exists(os.path.join(d, "arch", "trades.parquet"))
    else:
        assert res == {}        # graceful skip when pyarrow absent
