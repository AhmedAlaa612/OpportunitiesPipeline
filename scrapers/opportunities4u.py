"""
Scraper for opportunit4u.com — "Recent Posts" section.

The "Recent Posts" section is the main blog feed: div.widget.Blog#Blog1,
containing div.blog-post cards. Each card has all the metadata we need
(title, link, datetime, category) without any extra requests.

Only contains site-specific logic:
  - fetch_opportunity_list(): parses div.blog-post cards from div.Blog.
  - scrape_opportunity_page(): fetches one post, pre-extracts structured
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


class Opportunit4UScraper(BaseScraper):

    source_name = "opportunit4u"
    base_url = "https://www.opportunit4u.com/"
    exclude_domains = ["https://www.opportunit4u.com"]
    request_delay = 1.5

    # ── Listing page ───────────────────────────────────────────────────

    def fetch_opportunity_list(self) -> List[Dict[str, Any]]:
        response = requests.get(self.base_url, timeout=30)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, "html.parser")

        # The "Recent posts" section is div.widget.Blog (Blogger's main feed widget)
        # Structure per card:
        #   div.blog-post
        #     a.post-tag                              ← category
        #     h2.post-title > a[href]                ← title + link
        #     span.post-date.published[datetime]      ← ISO datetime + human text
        blog_widget = soup.find("div", class_="Blog")
        if not blog_widget:
            logger.error("[%s] Could not find div.Blog — site structure may have changed", self.source_name)
            return []

        results: List[Dict[str, Any]] = []
        seen: set = set()

        for card in blog_widget.find_all("div", class_="blog-post"):
            h2 = card.find("h2", class_="post-title")
            a  = h2.find("a", href=True) if h2 else None
            if not a:
                continue

            title = a.get_text(strip=True)
            link  = a["href"]
            if not link or link in seen or not title:
                continue

            # span.post-date.published carries both the datetime attr and human text
            date_span = card.find("span", class_="post-date")
            date_text = date_span.get_text(strip=True) if date_span else ""
            iso_dt    = date_span.get("datetime", "")  if date_span else ""

            # category already on the card — no need to fetch from the post page
            tag_a    = card.find("a", class_="post-tag")
            category = tag_a.get_text(strip=True) if tag_a else ""

            seen.add(link)
            results.append({
                "title":    title,
                "link":     link,
                "date_text": date_text,
                "datetime": iso_dt,
                "category": category,
            })

        logger.info("[%s] Found %d posts", self.source_name, len(results))
        return results

    # ── Individual post page ───────────────────────────────────────────

    def scrape_opportunity_page(self, opp: Dict[str, Any]) -> Optional[str]:
        response = requests.get(opp["link"], timeout=15)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, "html.parser")

        # 1. Pre-extract structured fields
        structured = self._extract_structured_fields(soup, opp)
        logger.info(
            "  Pre-extracted: category=%s | overview_keys=%s | apply_link=%s",
            structured.get("category", "—"),
            list(structured.get("quick_overview", {}).keys()),
            (structured.get("apply_link") or "—")[:60],
        )

        # 2. Locate article body — Blogger uses .post-body or .entry-content
        article = (
            soup.find("div", class_="post-body")
            or soup.find("div", class_="entry-content")
            or soup.find("article")
        )
        if not article:
            logger.warning("[%s] No article body found for: %s", self.source_name, opp["link"])
            return None

        # 3. Convert to Markdown
        body_md = html_to_clean_md(article.decode_contents(), exclude_domains=self.exclude_domains)

        # 4. Build enriched document
        return build_enriched_markdown(
            title=opp["title"],
            date_text=opp.get("date_text", ""),
            link=opp["link"],
            structured=structured,
            body_md=body_md,
        )

    def _extract_structured_fields(
        self, soup: BeautifulSoup, opp: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Pull out reliably structured fields from an opportunit4u.com post:
          - category:       from the listing card (passed via opp), fallback to label links
          - description:    first substantive paragraph before any overview heading
          - quick_overview: bullet-point overview table (Host, Deadline, etc.)
          - apply_link:     external application URL
        """
        fields: Dict[str, Any] = {}

        # Category already scraped from the listing card
        if opp.get("category"):
            fields["category"] = opp["category"]
        else:
            label = soup.find("a", rel="tag") or soup.find("a", href=re.compile(r"/search/label/"))
            if label:
                fields["category"] = label.get_text(strip=True)

        # Article body
        article = (
            soup.find("div", class_="post-body")
            or soup.find("div", class_="entry-content")
            or soup.find("article")
        )
        if not article:
            return fields

        # Description: first long <p> before any overview heading
        for elem in article.find_all(["p", "h2", "h3"]):
            text = elem.get_text(strip=True)
            if not text:
                continue
            if "overview" in text.lower():
                break
            if elem.name == "p" and len(text) > 60:
                fields["description"] = text
                break

        # Quick Overview: bullet list right after an overview heading
        overview_heading = article.find(
            lambda tag: tag.name in ("h2", "h3", "h4", "strong", "p", "b")
            and "overview" in tag.get_text(strip=True).lower()
        )
        if overview_heading:
            ul = overview_heading.find_next("ul")
            if ul:
                overview: Dict[str, str] = {}
                for li in ul.find_all("li"):
                    text = re.sub(r"\s+", " ", li.get_text(separator=" ", strip=True))
                    if ":" in text:
                        key, _, val = text.partition(":")
                        key = key.strip().strip("*").strip()
                        val = val.strip().strip("*").strip()
                        if key and val and len(key) < 60:
                            overview[key] = val
                if overview:
                    fields["quick_overview"] = overview

        # Apply link: first external link with apply/official text
        apply_pattern = re.compile(r"apply|official|application", re.I)
        for a in article.find_all("a", href=True):
            href = a["href"]
            if "opportunit4u.com" in href:
                continue
            if apply_pattern.search(a.get_text(strip=True)) or apply_pattern.search(href):
                fields["apply_link"] = href
                break

        return fields