"""
Template for adding a new scraper source.

Copy this file to scrapers/mysource.py, fill in the TODOs,
then register it in run_pipeline.py.
"""

import logging
from typing import Any, Dict, List, Optional

import requests
from bs4 import BeautifulSoup

from helpers.scraper_base import BaseScraper
from helpers.html import html_to_clean_md
from helpers.markdown import build_enriched_markdown

logger = logging.getLogger(__name__)


class ExampleNewScraper(BaseScraper):

    # TODO: set these
    source_name = "mysource"                   # unique key stored in DB
    base_url = "https://mysite.com/opportunities/"
    exclude_domains = ["https://mysite.com"]   # links to strip from Markdown
    request_delay = 1.5                        # seconds between page requests

    def fetch_opportunity_list(self) -> List[Dict[str, Any]]:
        """
        Fetch the listing page and return opportunity stubs.

        Each dict must have at minimum:
            title    (str)
            link     (str)
            datetime (str)  ISO-8601, used for dedup against DB
            date_text(str)  human-readable, stored in metadata
        """
        # TODO: implement for your site
        response = requests.get(self.base_url, timeout=30)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, "html.parser")

        results = []
        # for card in soup.find_all(...):
        #     results.append({
        #         "title": ...,
        #         "link": ...,
        #         "datetime": ...,
        #         "date_text": ...,
        #     })
        return results

    def scrape_opportunity_page(self, opp: Dict[str, Any]) -> Optional[str]:
        """
        Fetch one opportunity page and return enriched Markdown, or None.
        """
        # TODO: implement for your site
        response = requests.get(opp["link"], timeout=15)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, "html.parser")

        # Extract whatever structured fields you can reliably parse
        structured = {}
        # structured["description"] = ...
        # structured["quick_overview"] = {...}
        # structured["apply_link"] = ...

        article = soup.find("div", class_="your-article-class")
        if not article:
            return None

        body_md = html_to_clean_md(article.decode_contents(), exclude_domains=self.exclude_domains)

        return build_enriched_markdown(
            title=opp["title"],
            date_text=opp.get("date_text", ""),
            link=opp["link"],
            structured=structured,
            body_md=body_md,
        )
