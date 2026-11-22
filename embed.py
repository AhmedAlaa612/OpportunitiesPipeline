"""
Step 3: Embed extracted opportunities and upsert to Qdrant.

Reads opportunities_en.json, generates Jina embeddings,
and upserts points with structured payloads to Qdrant.
"""

import json
import time
import logging
from datetime import date
from typing import List

import numpy as np
import requests as http_requests
from qdrant_client import QdrantClient
from qdrant_client.models import PointStruct

try:
    from .config import (
        JINA_API_KEY,
        JINA_ENDPOINT,
        JINA_MODEL,
        EMBED_BATCH_SIZE,
        QDRANT_ENDPOINT,
        QDRANT_API_KEY,
        QDRANT_COLLECTION,
        UPSERT_BATCH_SIZE,
        OPPORTUNITIES_JSON,
    )
    from .countries import normalize_countries
except ImportError:
    from config import (
        JINA_API_KEY,
        JINA_ENDPOINT,
        JINA_MODEL,
        EMBED_BATCH_SIZE,
        QDRANT_ENDPOINT,
        QDRANT_API_KEY,
        QDRANT_COLLECTION,
        UPSERT_BATCH_SIZE,
        OPPORTUNITIES_JSON,
    )
    from countries import normalize_countries

logger = logging.getLogger(__name__)


# ── Embedding helper ───────────────────────────────────────────────────


def get_jina_embedding(texts: List[str]) -> np.ndarray:
    headers = {
        "Authorization": f"Bearer {JINA_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {"model": JINA_MODEL, "input": texts}
    response = http_requests.post(JINA_ENDPOINT, json=payload, headers=headers, timeout=60)
    response.raise_for_status()
    return np.array(
        [d["embedding"] for d in response.json()["data"]],
        dtype=np.float32,
    )


# ── Data helpers ───────────────────────────────────────────────────────


def ensure_list(val):
    if val is None:
        return []
    return [val] if isinstance(val, str) else list(val)


def deadline_to_ts(deadline_str: str | None) -> float | None:
    """Convert 'YYYY-MM-DD' string to Unix timestamp (UTC midnight). Returns None if absent/invalid."""
    if not deadline_str:
        return None
    try:
        d = date.fromisoformat(deadline_str)
        from datetime import datetime, timezone
        return datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None



def build_exam_scores(lang_reqs):
    """Convert language_requirements dict → nested exam_scores list."""
    if not lang_reqs or not isinstance(lang_reqs, dict):
        return []
    scores = []
    for exam_name, score_val in lang_reqs.items():
        try:
            scores.append({"name": exam_name.strip().lower(), "score": float(score_val)})
        except (ValueError, TypeError):
            continue
    return scores


def build_qdrant_point(opp_en: dict, embedding: np.ndarray) -> PointStruct:
    countries = ensure_list(opp_en.get("country"))
    fund_types = ensure_list(opp_en.get("fund_type"))
    type_info = opp_en.get("type") or {}
    subtypes = ensure_list(type_info.get("subtype"))
    target_segments = ensure_list(opp_en.get("target_segment"))
    documents_required = ensure_list(opp_en.get("documents_required"))
    application_fee = opp_en.get("application_fee")
    exam_scores = build_exam_scores(opp_en.get("language_requirements"))

    deadline_str = opp_en.get("deadline")
    deadline_ts = deadline_to_ts(deadline_str)
    today_ts = deadline_to_ts(date.today().isoformat())
    is_active = bool(opp_en.get("is_active", True)) and (
        deadline_ts is None or deadline_ts >= today_ts
    )

    payload = {
        "program_id": opp_en["id"],
        "title": opp_en.get("title"),
        "country": countries,
        "fund_type": fund_types,
        "category": type_info.get("category"),
        "subtype": subtypes,
        "documents_required": documents_required,
        "exam_scores": exam_scores,
        "is_remote": opp_en.get("is_remote", False),
        "target_segment": target_segments,
        "deadline": deadline_str,
        "deadline_ts": deadline_ts,
        "is_active": is_active,
        "gpa": float(opp_en["gpa"]) if opp_en.get("gpa") is not None else None,
        "has_language_requirements": len(exam_scores) > 0,
        "has_fee": bool(application_fee),
        "has_document_requirements": bool(documents_required),
    }
    return PointStruct(id=opp_en["id"], vector=embedding, payload=payload)


def embed_and_upsert(opportunities: list[dict]) -> int:
    """Embed in-memory opportunities and upsert their points to Qdrant."""
    if not opportunities:
        return 0

    rich_texts = []
    for opportunity in opportunities:
        if opportunity.get("country"):
            raw_country = opportunity["country"]
            opportunity["country"] = normalize_countries(
                raw_country if isinstance(raw_country, list) else [raw_country]
            )
        rich_texts.append(
            f"{opportunity.get('title', '')}\n"
            f"{opportunity.get('description', '')}\n"
            f"Eligibility: {opportunity.get('eligibility', '')}\n"
        )

    all_embeddings = []
    for start in range(0, len(rich_texts), EMBED_BATCH_SIZE):
        batch = rich_texts[start : start + EMBED_BATCH_SIZE]
        logger.info("Embedding batch %d–%d / %d", start, start + len(batch), len(rich_texts))
        all_embeddings.extend(get_jina_embedding(batch))
        time.sleep(1)

    points = [
        build_qdrant_point(opportunity, embedding)
        for opportunity, embedding in zip(opportunities, all_embeddings)
    ]
    client = QdrantClient(url=QDRANT_ENDPOINT, api_key=QDRANT_API_KEY)
    for start in range(0, len(points), UPSERT_BATCH_SIZE):
        client.upsert(
            collection_name=QDRANT_COLLECTION,
            points=points[start : start + UPSERT_BATCH_SIZE],
        )

    logger.info("Upserted %d points to Qdrant collection '%s'", len(points), QDRANT_COLLECTION)
    return len(points)


# ── Main ───────────────────────────────────────────────────────────────


def run() -> bool:
    """
    Execute the embedding step.
    Returns True on success.
    """
    if not OPPORTUNITIES_JSON.exists():
        logger.error("opportunities_en.json not found at %s — run extract step first", OPPORTUNITIES_JSON)
        return False

    opportunities = json.loads(OPPORTUNITIES_JSON.read_text(encoding="utf-8"))
    logger.info("Loaded %d opportunities from %s", len(opportunities), OPPORTUNITIES_JSON)

    if not opportunities:
        logger.warning("No opportunities to embed")
        return False

    count = embed_and_upsert(opportunities)
    logger.info("Generated and upserted %d opportunity embeddings", count)
    return count > 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    run()