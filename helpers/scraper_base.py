"""
BaseScraper — abstract base class for all source scrapers.

To add a new scraper:
  1. Create scrapers/mysource.py
  2. Subclass BaseScraper
  3. Set source_name, base_url, and optionally exclude_domains
  4. Implement fetch_opportunity_list() and scrape_opportunity_page()
  5. Register it in run_pipeline.py

The shared run() method handles:
  - Fetching the last scraped date per source (so sources are independent)
  - Filtering out already-seen opportunities by date
  - Looping, saving .md files, writing source_metadata.json
  - Logging and error handling
"""

import json
import logging
import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

from config import OUTPUT_DIR, CSV_OUTPUT, SOURCE_META_PATH
from helpers.db import get_last_scraped_date
from helpers.html import sanitize_filename

logger = logging.getLogger(__name__)


class BaseScraper(ABC):
    """
    Abstract base for all opportunity scrapers.

    Subclasses must define:
        source_name (str):  Unique identifier, e.g. "opportunitiescorners".
                            Stored in the DB and used for per-source date tracking.
        base_url    (str):  Homepage / listing URL to start from.

    Subclasses may override:
        exclude_domains (List[str]):  Domains to strip from converted Markdown.
        request_delay   (float):      Seconds to sleep between page requests.
    """

    source_name: str = ""
    base_url: str = ""
    exclude_domains: List[str] = []
    request_delay: float = 1.0

    # ── Abstract interface ─────────────────────────────────────────────

    @abstractmethod
    def fetch_opportunity_list(self) -> List[Dict[str, Any]]:
        """
        Fetch the listing page(s) and return a list of opportunity stubs.

        Each dict must contain at minimum:
            title    (str):  Opportunity title.
            link     (str):  Full URL to the individual opportunity page.
            datetime (str):  ISO-8601 publish datetime string (used for dedup).
            date_text(str):  Human-readable date string (stored in metadata).

        Extra keys are fine and will be passed through to save_opportunity().
        """
        raise NotImplementedError

    @abstractmethod
    def scrape_opportunity_page(self, opp: Dict[str, Any]) -> Optional[str]:
        """
        Fetch one opportunity page and return the enriched Markdown string,
        or None if the page cannot be processed.

        The returned string is what gets saved as a .md file and later
        passed to the extract step.
        """
        raise NotImplementedError

    # ── Shared orchestration ───────────────────────────────────────────

    def run(self) -> bool:
        """
        Full scrape run for this source.

        1. Fetch the opportunity list.
        2. Filter out anything already in the DB (by created_at per source).
        3. Scrape each new opportunity page.
        4. Save .md files + update source_metadata.json.

        Returns True if at least one opportunity was successfully scraped.
        """
        if not self.source_name or not self.base_url:
            raise RuntimeError(
                f"{self.__class__.__name__} must define source_name and base_url"
            )

        last_scraped_date = get_last_scraped_date(source=self.source_name)

        logger.info("[%s] Fetching opportunity list from %s", self.source_name, self.base_url)
        all_opportunities = self.fetch_opportunity_list()
        logger.info("[%s] Found %d opportunities", self.source_name, len(all_opportunities))

        if not all_opportunities:
            logger.error("[%s] No opportunities found — check fetch_opportunity_list()", self.source_name)
            return False

        # ── Filter by last scraped date ───────────────────────────────
        if last_scraped_date:
            new_opps, skipped = [], 0
            for opp in all_opportunities:
                dt_str = opp.get("datetime")
                if dt_str:
                    opp_date = datetime.fromisoformat(dt_str)
                    if opp_date.tzinfo is None:
                        opp_date = opp_date.replace(tzinfo=timezone.utc)
                    if opp_date > last_scraped_date:
                        new_opps.append(opp)
                    else:
                        skipped += 1
                else:
                    new_opps.append(opp)  # no date → include to be safe
            logger.info(
                "[%s] %d new opportunities (skipped %d already scraped)",
                self.source_name, len(new_opps), skipped,
            )
            opportunities_to_scrape = new_opps
        else:
            logger.info("[%s] First run — processing all %d", self.source_name, len(all_opportunities))
            opportunities_to_scrape = all_opportunities

        if not opportunities_to_scrape:
            logger.info("[%s] Nothing new. Done.", self.source_name)
            return False

        # ── Save CSV metadata ─────────────────────────────────────────
        pd.DataFrame(opportunities_to_scrape).to_csv(CSV_OUTPUT, index=False)
        logger.info("[%s] Saved metadata CSV → %s", self.source_name, CSV_OUTPUT)

        # ── Load existing source_metadata.json (multi-scraper safe) ──
        source_meta: Dict[str, Any] = {}
        if SOURCE_META_PATH.exists():
            with open(SOURCE_META_PATH, encoding="utf-8") as f:
                source_meta = json.load(f)

        # ── Clear old .md files for this source ───────────────────────
        # Only remove files belonging to this source (multi-scraper safe)
        files_for_source = {
            fname for fname, meta in source_meta.items()
            if meta.get("source") == self.source_name
        }
        for fname in files_for_source:
            fpath = OUTPUT_DIR / fname
            if fpath.exists():
                fpath.unlink()
        logger.info("[%s] Cleared %d old .md files", self.source_name, len(files_for_source))

        # ── Scrape each page ──────────────────────────────────────────
        successful, failed = 0, 0

        for idx, opp in enumerate(opportunities_to_scrape, 1):
            if not opp.get("link"):
                logger.warning("[%s][%d] No link for '%s' — skipping", self.source_name, idx, opp.get("title", ""))
                failed += 1
                continue

            logger.info(
                "[%s][%d/%d] %s",
                self.source_name, idx, len(opportunities_to_scrape),
                (opp.get("title") or "")[:70],
            )

            try:
                enriched_md = self.scrape_opportunity_page(opp)
                if not enriched_md:
                    logger.warning("[%s][%d] scrape_opportunity_page() returned None — skipping", self.source_name, idx)
                    failed += 1
                    continue

                filename = sanitize_filename(opp["title"]) + ".md"
                (OUTPUT_DIR / filename).write_text(enriched_md, encoding="utf-8")

                # Update source metadata
                source_meta[filename] = {
                    "source": self.source_name,
                    "source_url": opp.get("link"),
                }

                logger.info("[%s][%d] Saved %s", self.source_name, idx, filename)
                successful += 1
                time.sleep(self.request_delay)

            except Exception as e:
                logger.error("[%s][%d] Error: %s", self.source_name, idx, str(e)[:100])
                failed += 1

        # ── Persist updated source_metadata.json ─────────────────────
        with open(SOURCE_META_PATH, "w", encoding="utf-8") as f:
            json.dump(source_meta, f, ensure_ascii=False, indent=2)

        logger.info(
            "[%s] Done — %d succeeded, %d failed",
            self.source_name, successful, failed,
        )
        return successful > 0
