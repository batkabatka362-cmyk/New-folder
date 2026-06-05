"""Offline tools (Phase 2): replay logged candidates, label outcomes, train GBM.

Pipeline:  run the bot to collect candidates  ->  label.py (forward outcomes)
           ->  train_gbm.py (LightGBM model)  ->  drop gbm_model.txt in place.
Until a model exists, the live bot scores with the transparent rule scorer.
"""
