"""
Scraper for opportunitiescorners.com.

Only contains site-specific logic:
  - fetch_opportunity_list(): finds the "Latest Opportunities" section
    and parses the article cards.
  - scrape_opportunity_page(): fetches one page, pre-extracts structured
    fields, converts the body to Markdown, and builds the enriched document.

All shared orchestration (date filtering, file saving, logging) lives in
BaseScraper.run().
"""

import re
import logging
from typing import Any, Dict, List, Optional

import requests
from bs4 import BeautifulSoup

from helpers.scraper_base import BaseScraper
from helpers.html import html_to_clean_md
from helpers.markdown import build_enriched_markdown

logger = logging.getLogger(__name__)


class OpportunitiesCornersScraper(BaseScraper):

    source_name = "opportunitiescorners"
    base_url = "https://opportunitiescorners.com/"
    exclude_domains = ["https://opportunitiescorners"]
    request_delay = 1.0

    # ── Listing page ───────────────────────────────────────────────────

    def fetch_opportunity_list(self) -> List[Dict[str, Any]]:
        response = requests.get(self.base_url, timeout=30)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, "html.parser")

        section = self._find_latest_section(soup)
        cards = self._parse_opportunity_cards(section)
        return cards

    def _find_latest_section(self, soup: BeautifulSoup):
        heading = soup.find(
            lambda tag: tag.name in ("h2", "h3", "h4", "h5", "span", "div", "p")
            and "LATEST OPPORTUNITIES" in tag.get_text(strip=True).upper()
        )

        if not heading:
            logger.warning("Could not find 'LATEST OPPORTUNITIES' heading — falling back to full page")
            return soup

        # The cards live in the sibling .td_block_inner div,
        # not in an ancestor — walk siblings, not parents
        title_wrap = heading.find_parent("div", class_="td-block-title-wrap")
        if title_wrap:
            block_inner = title_wrap.find_next_sibling("div", class_="td_block_inner")
            if block_inner:
                logger.info("Found td_block_inner via sibling of td-block-title-wrap")
                return block_inner

        # Fallback: look for the nearest td_block_inner after the heading
        block_inner = heading.find_next("div", class_="td_block_inner")
        if block_inner:
            logger.info("Found td_block_inner via find_next fallback")
            return block_inner

        logger.warning("Could not isolate section container — using heading's parent")
        return heading.parent

    def _parse_opportunity_cards(self, section) -> List[Dict]:
        """Extract title, url, and datetime from each card in the section."""
        cards = section.find_all(
            lambda tag: tag.name == "div"
            and any("td_module" in c for c in tag.get("class", []))
        )

        results = []
        seen = set()

        for card in cards:
            time_elem = card.find("time")
            if not time_elem or not time_elem.get("datetime"):
                continue

            title_elem = card.find("h3", class_="entry-title") or card.find("h3")
            a_tag = title_elem.find("a") if title_elem else None
            title = a_tag.get_text(strip=True) if a_tag else None
            link = a_tag["href"] if a_tag and a_tag.get("href") else None

            if not link or link in seen:
                continue
            seen.add(link)

            results.append({
                "title": title,
                "link": link,
                "date_text": time_elem.get_text(strip=True),
                "datetime": time_elem.get("datetime"),
            })

        return results

    # ── Individual opportunity page ────────────────────────────────────

    def scrape_opportunity_page(self, opp: Dict[str, Any]) -> Optional[str]:
        opp_response = requests.get(opp["link"], timeout=15)
        opp_response.raise_for_status()
        opp_soup = BeautifulSoup(opp_response.content, "html.parser")

        # 1. Pre-extract structured fields from the page
        structured = self._extract_structured_fields(opp_soup)
        logger.info(
            "  Pre-extracted: category=%s | overview_keys=%s | apply_link=%s",
            structured.get("category", "—"),
            list(structured.get("quick_overview", {}).keys()),
            (structured.get("apply_link") or "—")[:60],
        )

        # 2. Get the article HTML
        main_div = opp_soup.find("div", class_="td-main-content")
        article = main_div.find("article") if main_div else None
        if not article:
            article = opp_soup.find("div", class_="td-post-content")
        if not article:
            logger.warning("  No article content found")
            return None

        # 3. Convert to Markdown
        body_md = html_to_clean_md(article.decode_contents(), exclude_domains=self.exclude_domains)

        # 4. Build enriched Markdown (structured preamble + full body)
        return build_enriched_markdown(
            title=opp["title"],
            date_text=opp.get("date_text", ""),
            link=opp["link"],
            structured=structured,
            body_md=body_md,
        )

    def _extract_structured_fields(self, soup: BeautifulSoup) -> Dict[str, Any]:
        """
        Pull out fields that are reliably structured on every opportunity page:
          - quick_overview: the bullet-point table (Host, Deadline, etc.)
          - description:    the opening paragraph before Quick Overview
          - apply_link:     the external application URL
          - category:       breadcrumb category
        """
        fields: Dict[str, Any] = {}

        # ── Category from breadcrumb ─────────────────────────────────
        breadcrumb = soup.find("div", class_=re.compile(r"td-crumbs|breadcrumb"))
        if breadcrumb:
            crumb_links = breadcrumb.find_all("a")
            if crumb_links:
                fields["category"] = crumb_links[-1].get_text(strip=True)

        # ── Article body ─────────────────────────────────────────────
        article = (
            soup.find("div", class_="td-post-content")
            or soup.find("article")
            or soup.find("div", class_="td-main-content")
        )
        if not article:
            return fields

        # ── Description: first <p> before the Quick Overview heading ─
        for elem in article.find_all(["p", "h2", "h3"]):
            text = elem.get_text(strip=True)
            if not text:
                continue
            if "quick overview" in text.lower():
                break
            if elem.name == "p" and len(text) > 60:
                fields["description"] = text
                break

        # ── Quick Overview: bullet list right after the heading ──────
        overview_heading = article.find(
            lambda tag: tag.name in ("h2", "h3", "h4", "strong", "p")
            and "quick overview" in tag.get_text(strip=True).lower()
        )
        if overview_heading:
            ul = overview_heading.find_next("ul")
            if ul:
                overview = {}
                for li in ul.find_all("li"):
                    text = li.get_text(separator=" ", strip=True)
                    text = re.sub(r"\s+", " ", text).strip()
                    if ":" in text:
                        key, _, val = text.partition(":")
                        key = key.strip().strip("*").strip()
                        val = val.strip().strip("*").strip()
                        if key and val and len(key) < 60:
                            overview[key] = val
                if overview:
                    fields["quick_overview"] = overview

        # ── Apply link: external URL on a button/link with apply text ─
        apply_pattern = re.compile(r"apply|application|apply now|official", re.I)
        for a in article.find_all("a", href=True):
            href = a["href"]
            text = a.get_text(strip=True)
            if "opportunitiescorners.com" in href:
                continue
            if apply_pattern.search(text) or apply_pattern.search(href):
                fields["apply_link"] = href
                break

        return fields
