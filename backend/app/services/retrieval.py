"""Similar-incident retrieval over verified knowledge (pgvector on PostgreSQL, numpy elsewhere)."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import KnowledgeEntry
from .embeddings import get_embedder


@dataclass
class Retrieved:
    entry: KnowledgeEntry
    similarity: float

    def as_dict(self) -> dict:
        e = self.entry
        return {
            "knowledge_id": e.id,
            "incident_id": e.incident_id,
            "title": e.title,
            "root_cause_id": e.root_cause_id,
            "root_cause": e.root_cause,
            "action_type": e.action_type,
            "outcome": e.outcome,
            "affected_module": e.affected_module,
            "similarity": round(self.similarity, 4),
        }


def search_similar(
    db: Session,
    query_text: str | None = None,
    embedding: list[float] | None = None,
    k: int | None = None,
    min_similarity: float | None = None,
    exclude_incident_id: int | None = None,
) -> list[Retrieved]:
    settings = get_settings()
    k = k or settings.rag_top_k
    min_similarity = settings.rag_min_similarity if min_similarity is None else min_similarity
    if embedding is None:
        embedding = get_embedder().embed(query_text or "")

    if db.bind.dialect.name == "postgresql":
        vec = "[" + ",".join(f"{x:.6f}" for x in embedding) + "]"
        rows = db.execute(
            text(
                "SELECT id, 1 - (embedding <=> CAST(:q AS vector)) AS sim FROM knowledge_entries "
                "WHERE verified AND embedding IS NOT NULL "
                "AND (CAST(:ex AS integer) IS NULL OR incident_id IS DISTINCT FROM CAST(:ex AS integer)) "
                "ORDER BY embedding <=> CAST(:q AS vector) LIMIT :k"
            ),
            {"q": vec, "k": k, "ex": exclude_incident_id},
        ).all()
        entries = {e.id: e for e in db.scalars(
            select(KnowledgeEntry).where(KnowledgeEntry.id.in_([r.id for r in rows]))
        )}
        results = [Retrieved(entries[r.id], float(r.sim)) for r in rows if r.id in entries]
    else:
        stmt = select(KnowledgeEntry).where(KnowledgeEntry.verified.is_(True))
        candidates = [e for e in db.scalars(stmt) if e.embedding is not None
                      and (exclude_incident_id is None or e.incident_id != exclude_incident_id)]
        if not candidates:
            return []
        q = np.asarray(embedding)
        mat = np.asarray([c.embedding for c in candidates])
        sims = mat @ q / (np.linalg.norm(mat, axis=1) * (np.linalg.norm(q) or 1.0) + 1e-12)
        order = np.argsort(-sims)[:k]
        results = [Retrieved(candidates[i], float(sims[i])) for i in order]

    return [r for r in results if r.similarity >= min_similarity]
