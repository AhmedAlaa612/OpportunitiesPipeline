"""
Shared database helpers used by all scrapers and pipeline steps.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

import psycopg2

from config import DB_CONFIG

logger = logging.getLogger(__name__)


def get_db_connection():
    """Return a new psycopg2 connection."""
    return psycopg2.connect(**DB_CONFIG)


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
