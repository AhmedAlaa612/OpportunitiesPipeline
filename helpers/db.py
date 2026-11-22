"""
Shared database helpers used by all scrapers and pipeline steps.
"""

import logging
import re
from datetime import datetime, timezone
from typing import Optional

import psycopg2
from psycopg2.extras import Json

try:
    from ..config import DB_CONFIG
except ImportError:
    from config import DB_CONFIG

logger = logging.getLogger(__name__)


def _ensure_list(value):
    if value is None:
        return None
    return [value] if isinstance(value, str) else list(value)


def _parse_date(value):
    if not value or not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return None
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return None
    return value


def get_db_connection():
    """Return a new psycopg2 connection."""
    return psycopg2.connect(**DB_CONFIG)


def upsert_opportunity(
    opportunity_id: str,
    data_en: dict,
    data_ar: dict,
    source: str = "opportunitiescorners",
    source_url: Optional[str] = None,
    source_md: Optional[str] = None,
    is_active: bool = True,
) -> None:
    """Insert or update one opportunity in PostgreSQL."""
    now = datetime.now(timezone.utc)
    type_info = data_en.get("type") or {}
    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO opportunities (
                id, source, source_url, source_md, data_en, data_ar,
                title_en, title_ar, category, subtype, country, fund_type,
                target_segment, deadline, is_remote, open_to_egyptians,
                is_active, created_at, updated_at
            )
            VALUES (
                %s::uuid, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s
            )
            ON CONFLICT (id) DO UPDATE SET
                source = EXCLUDED.source,
                source_url = EXCLUDED.source_url,
                source_md = EXCLUDED.source_md,
                data_en = EXCLUDED.data_en,
                data_ar = EXCLUDED.data_ar,
                title_en = EXCLUDED.title_en,
                title_ar = EXCLUDED.title_ar,
                category = EXCLUDED.category,
                subtype = EXCLUDED.subtype,
                country = EXCLUDED.country,
                fund_type = EXCLUDED.fund_type,
                target_segment = EXCLUDED.target_segment,
                deadline = EXCLUDED.deadline,
                is_remote = EXCLUDED.is_remote,
                open_to_egyptians = EXCLUDED.open_to_egyptians,
                is_active = EXCLUDED.is_active,
                updated_at = EXCLUDED.updated_at;
            """,
            (
                opportunity_id,
                source,
                source_url,
                source_md,
                Json(data_en),
                Json(data_ar),
                data_en.get("title"),
                data_ar.get("title"),
                type_info.get("category"),
                _ensure_list(type_info.get("subtype")),
                _ensure_list(data_en.get("country")),
                _ensure_list(data_en.get("fund_type")),
                _ensure_list(data_en.get("target_segment")),
                _parse_date(data_en.get("deadline")),
                bool(data_en.get("is_remote", False)),
                bool(data_en.get("open_to_egyptians", True)),
                is_active,
                now,
                now,
            ),
        )
        conn.commit()
    finally:
        try:
            if cur is not None:
                cur.close()
        finally:
            if conn is not None:
                conn.close()


def get_last_scraped_date(source: Optional[str] = None) -> Optional[datetime]:
    """
    Return the most recent created_at timestamp in the opportunities table.

    Pass `source` (e.g. "opportunitiescorners") to track each scraper
    independently, so adding a new source doesn't make it think everything
    is already scraped.
    """
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        if source:
            cur.execute(
                "SELECT MAX(created_at) FROM opportunities WHERE source = %s;",
                (source,),
            )
        else:
            cur.execute("SELECT MAX(created_at) FROM opportunities;")
        result = cur.fetchone()[0]
        cur.close()
        conn.close()

        if result:
            if result.tzinfo is None:
                result = result.replace(tzinfo=timezone.utc)
            logger.info(
                "Last scraped date%s: %s",
                f" for '{source}'" if source else "",
                result.isoformat(),
            )
            return result

        logger.info(
            "No existing opportunities%s — will scrape all",
            f" for source '{source}'" if source else "",
        )
        return None

    except Exception as e:
        logger.warning("Could not query DB for last date: %s — will scrape all", e)
        return None
