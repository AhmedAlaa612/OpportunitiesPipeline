"""
Pipeline orchestrator — runs scrape → extract → embed in sequence.

Usage:
    python run_pipeline.py              # run all 3 steps
    python run_pipeline.py scrape       # scrape only
    python run_pipeline.py extract      # extract only
    python run_pipeline.py embed        # embed only
    python run_pipeline.py scrape embed # any combination

Adding a new scraper:
    1. Create scrapers/mysource.py (subclass BaseScraper)
    2. Import it here and add to SCRAPERS list below
"""

import logging
import sys
import time

import extract
import embed
from scrapers.opportunitiescorners import OpportunitiesCornersScraper
from scrapers.opportunities4u import Opportunities4uScraper
from scrapers.opportunitiesforafricans import OpportunitiesForAfricansScraper
# from scrapers.mysource import MySourceScraper  # ← add new scrapers here

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger("pipeline")


# ── Register scrapers ──────────────────────────────────────────────────
# Each scraper runs independently and tracks its own last-scraped date.
SCRAPERS = [
    OpportunitiesCornersScraper(),
    Opportunities4uScraper(),
    OpportunitiesForAfricansScraper(),
    # MySourceScraper(),
]


def run_scrape() -> bool:
    """Run all registered scrapers. Returns True if any produced new data."""
    any_new = False
    for scraper in SCRAPERS:
        logger.info("=== Scraper: %s ===", scraper.source_name)
        try:
            had_work = scraper.run()
            if had_work:
                any_new = True
        except Exception:
            logger.exception("Scraper '%s' failed with an unhandled exception", scraper.source_name)
    return any_new


STEPS = {
    "scrape": run_scrape,
    "extract": extract.run,
    "embed": embed.run,
}


def main():
    requested = sys.argv[1:] if len(sys.argv) > 1 else list(STEPS.keys())

    for step_name in requested:
        if step_name not in STEPS:
            logger.error(
                "Unknown step: %s (choose from: %s)",
                step_name, ", ".join(STEPS),
            )
            sys.exit(1)

    logger.info("=== Starting pipeline: %s ===", " → ".join(requested))
    start = time.time()

    for step_name in requested:
        logger.info("── Step: %s ──", step_name)
        step_start = time.time()

        try:
            has_work = STEPS[step_name]()
        except Exception:
            logger.exception("Step '%s' failed with an unhandled exception", step_name)
            sys.exit(1)

        elapsed = time.time() - step_start
        logger.info("── %s finished in %.1fs (has_work=%s) ──", step_name, elapsed, has_work)

        # If scrape/extract produced nothing new, skip downstream steps
        if not has_work and step_name in ("scrape", "extract") and len(requested) > 1:
            logger.info("No new data from '%s' — skipping remaining steps", step_name)
            break

    total = time.time() - start
    logger.info("=== Pipeline completed in %.1fs ===", total)


if __name__ == "__main__":
    main()
