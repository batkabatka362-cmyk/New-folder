"""Persistent lesson memory — the agent's cross-session learning, now SELF-CURATING.

Lessons are written by reflection after each closed trade and recalled before future decisions. The
raw lessons table is the durable log; on load (and incrementally on each store) the memory CURATES
itself — it de-duplicates near-identical lessons into ONE entry with a hit-count (REINFORCEMENT: a
lesson independently re-learned many times is more trustworthy), decays old un-reinforced lessons by
age, and recalls the highest-SCORE (most-validated) lessons for a candidate rather than merely the
newest. So the agent takes responsibility for its OWN knowledge base and keeps improving it: the
signal (repeated, recent truths) rises and the noise (one-off, stale notes) sinks. This makes the
memory HONEST + trustworthy; it does not, by itself, raise the trading ceiling.
"""
from __future__ import annotations

import re
import time

from ..utils.logging import get_logger

log = get_logger("agent.memory")

_WS = re.compile(r"\s+")


def _norm(lesson: str) -> str:
    """Normalized key for de-duplication — case/whitespace-insensitive, punctuation-trimmed."""
    return _WS.sub(" ", (lesson or "").strip().lower()).strip(" .")


def _score(confidence: float, hits: int, ts: float, now: float, half_life_s: float) -> float:
    """A lesson's recall priority: base confidence, BOOSTED by reinforcement (independent re-learnings)
    and DECAYED by age. reinforcement saturates so a spam-repeated note can't dominate; decay is a soft
    half-life so a stale one-off sinks below a fresh, repeated truth."""
    reinforce = min(0.4, 0.08 * max(0, hits - 1))           # +0.08/extra hit, capped at +0.4
    age = max(0.0, now - ts)
    decay = 0.5 ** (age / half_life_s) if half_life_s > 0 else 1.0
    return (max(0.0, confidence) + reinforce) * decay


def curate_lessons(rows: list, *, max_keep: int = 300, now: float | None = None,
                   half_life_s: float = 1_209_600.0) -> list[dict]:
    """De-duplicate raw lesson rows into scored, merged entries (highest score first, capped to
    max_keep). rows: [{lesson, tag, confidence, ts}]. Merge = same normalized text -> one entry with
    hits=count, the union of tags, the max confidence, and the latest ts. half_life ~14 days."""
    now = time.time() if now is None else now
    merged: dict[str, dict] = {}
    for r in rows:
        lesson = r.get("lesson") or ""
        key = _norm(lesson)
        if not key:
            continue
        m = merged.get(key)
        if m is None:
            merged[key] = {"lesson": lesson, "tags": set(t for t in (r.get("tag") or "").split("-") if t),
                           "confidence": float(r.get("confidence") or 0.0), "hits": 1, "ts": float(r.get("ts") or 0.0)}
        else:
            m["hits"] += 1
            m["confidence"] = max(m["confidence"], float(r.get("confidence") or 0.0))
            m["ts"] = max(m["ts"], float(r.get("ts") or 0.0))
            m["tags"].update(t for t in (r.get("tag") or "").split("-") if t)
            if len(lesson) > len(m["lesson"]):
                m["lesson"] = lesson                          # keep the most complete phrasing
    out = []
    for m in merged.values():
        out.append({"lesson": m["lesson"], "tag": "-".join(sorted(m["tags"])), "hits": m["hits"],
                    "ts": m["ts"], "confidence": m["confidence"],   # carried so store() can re-score on MAX confidence
                    "score": _score(m["confidence"], m["hits"], m["ts"], now, half_life_s)})
    out.sort(key=lambda e: e["score"], reverse=True)
    return out[:max_keep]


class AgentMemory:
    def __init__(self, storage, max_cache: int = 300, miss_recall_frac: float = 0.34) -> None:
        self.storage = storage
        self.max_cache = max_cache
        # P10b #2: cap the FRACTION of recall slots that 'miss'-origin (lean-in regret) lessons may fill,
        # so a burst of missed-winner reflections can't crowd out the survival/rug lessons.
        self.miss_recall_frac = min(1.0, max(0.0, miss_recall_frac))
        self._cache: list[dict] = []   # curated, score-sorted: {lesson, tag, hits, ts, score}

    async def load(self) -> None:
        rows = await self.storage.lessons_full(max(self.max_cache * 4, 1000))   # fold duplicates
        self._cache = curate_lessons(rows, max_keep=self.max_cache)
        log.info("loaded %d lessons (curated from %d raw, deduped + reinforcement-scored)",
                 len(self._cache), len(rows))

    def recall(self, candidate, k: int = 6) -> list[str]:
        """The highest-SCORE (most-validated: repeated + recent + confident) lessons relevant to this
        candidate's mode (+ global/rug). Tags matched by token so 'scalp-rug' matches but 'threshold'
        does not. Surfaces the agent's best-validated knowledge, not merely its newest note."""
        from .schema import setup_tag                     # local import avoids a module cycle
        wanted = {candidate.mode, "global", "rug", setup_tag(candidate.features.get("setup_type", ""))} - {""}
        # P10b LM5: a candidate already rolling over (falling trend) should also see FADE lessons.
        if (candidate.features.get("trend") or {}).get("dir") == "falling":
            wanted.add("fade")
        sit = self._situation_tokens(candidate)
        # P10b LM3: among the tag-eligible lessons, blend the validated score with a small RELEVANCE
        # nudge (situation tokens appearing in the lesson text). Relevance re-orders WITHIN the eligible
        # set; it never invents vocabulary nor overrides a much-higher-score lesson.
        eligible = []
        for entry in self._cache:
            tokens = {t for t in entry.get("tag", "").split("-") if t}
            if not (tokens & wanted):
                continue
            text = entry["lesson"].lower()
            relevance = sum(1 for s in sit if s and s in text)
            eligible.append((entry.get("score", 0.0) + 0.15 * relevance, entry, tokens))
        eligible.sort(key=lambda x: x[0], reverse=True)
        # P10b #2: bound the 'miss'-origin (regret) lessons so they can't crowd out survival lessons.
        miss_cap = int(k * self.miss_recall_frac)
        out: list[str] = []
        n_miss = 0
        # P10b LM4: reserve the first slot for the best eligible RUG lesson, so a rare catastrophe note
        # is never fully crowded out by reinforced platitudes.
        rug_entry = next((e for _, e, tk in eligible if "rug" in tk), None)
        if rug_entry is not None:
            out.append(rug_entry["lesson"])
        for _, entry, tokens in eligible:
            if len(out) >= k:
                break
            if entry is rug_entry:
                continue
            if "miss" in tokens:
                if n_miss >= miss_cap:
                    continue                                 # cap reached -> let a non-miss lesson take the slot
                n_miss += 1
            out.append(entry["lesson"])
        return out

    @staticmethod
    def _situation_tokens(candidate) -> set:
        """A small bag of canonical situation words for RELEVANCE matching against lesson TEXT — derived
        from the same features the prompt builder shows, so recall ranks by what is actually true now.
        Keep in sync with brain._candidate_prompt's signal lines."""
        f = candidate.features or {}
        toks = {candidate.mode}
        st = f.get("setup_type")
        if st:
            toks.add(st)
        trend = (f.get("trend") or {}).get("dir")
        if trend in ("falling", "rising"):
            toks.add(trend)
        if getattr(candidate, "liquidity_usd", 0) and candidate.liquidity_usd < 8000:
            toks.add("liquidity")
        conc = f.get("top5_concentration_pct")
        if conc is not None and conc >= 90:
            toks.add("concentration")
        chp = f.get("creator_holding_pct")
        if chp is not None and chp < 1.0:
            toks.add("creator")
        if (f.get("creator_launches") or 0) > 40:
            toks.add("creator")
        return toks - {""}

    async def store(self, lesson: str, tag: str, confidence: float) -> None:
        if not lesson:
            return
        await self.storage.log_lesson(lesson=lesson, tag=tag, confidence=confidence)
        # incremental curation: a re-learned lesson REINFORCES the existing entry (hit++ + re-score)
        # instead of appending a duplicate, so the cache stays deduped between full reloads.
        key = _norm(lesson)
        now = time.time()
        for e in self._cache:
            if _norm(e["lesson"]) == key:
                e["hits"] = e.get("hits", 1) + 1
                e["ts"] = now
                e["confidence"] = max(e.get("confidence", 0.0), confidence)   # MAX, consistent with curate_lessons
                e["tag"] = "-".join(sorted({t for t in (e.get("tag", "") + "-" + tag).split("-") if t}))
                e["score"] = _score(e["confidence"], e["hits"], now, now, 1_209_600.0)
                self._cache.sort(key=lambda x: x["score"], reverse=True)
                return
        self._cache.append({"lesson": lesson, "tag": tag, "hits": 1, "ts": now, "confidence": confidence,
                            "score": _score(confidence, 1, now, now, 1_209_600.0)})
        self._cache.sort(key=lambda x: x["score"], reverse=True)
        if len(self._cache) > self.max_cache:
            # P10b LM4: don't evict a high-confidence RUG lesson while non-rug entries remain — a rare
            # catastrophe note must survive crowding by reinforced platitudes. Drop the lowest-score
            # NON-protected entry; only if every entry is protected do we drop the lowest (never grow
            # unbounded). Cache is score-sorted desc, so scan from the lowest-score end.
            for i in range(len(self._cache) - 1, -1, -1):
                e = self._cache[i]
                protected = "rug" in e.get("tag", "").split("-") and e.get("confidence", 0.0) >= 0.6
                if not protected:
                    self._cache.pop(i)
                    break
            else:
                self._cache.pop()
