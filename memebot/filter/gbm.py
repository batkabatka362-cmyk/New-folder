"""GBM (LightGBM) probability scorer — Tier 0 ranking.

Loads a trained model artifact if present AND lightgbm is installed; otherwise
returns None so the caller (signals.Scorer) falls back to the rule scorer. This
keeps the bot running on rules until you have collected data and trained a model
(backtest/label.py -> backtest/train_gbm.py).
"""
from __future__ import annotations

import os

from ..models import Candidate
from ..utils.logging import get_logger
from .features import feature_vector

log = get_logger("filter.gbm")


class GBMScorer:
    def __init__(self, booster) -> None:
        self.booster = booster

    @classmethod
    def load(cls, path: str) -> "GBMScorer | None":
        if not path or not os.path.exists(path):
            log.info("no GBM model at %s -> using rule scorer (Phase 1 behavior)", path)
            return None
        try:
            import lightgbm as lgb
        except ImportError:
            log.warning("lightgbm not installed -> rule scorer (pip install lightgbm numpy)")
            return None
        try:
            booster = lgb.Booster(model_file=path)
        except Exception as e:  # noqa: BLE001
            log.warning("failed to load GBM model %s: %s -> rule scorer", path, e)
            return None
        log.info("GBM scorer loaded from %s", path)
        return cls(booster)

    def score(self, c: Candidate) -> float:
        # local import so numpy is only required when a model is actually loaded
        import numpy as np

        x = np.asarray([feature_vector(c)], dtype=float)
        p = float(self.booster.predict(x)[0])
        return max(0.0, min(1.0, p))
