"""
Scraper for opportunitiesforafricans.com

Site structure:
  - Listing: div.mag-post-box cards inside div.home-featured-cat-content
  - Title:   h3.magcat-titlte > a
  - Date:    time.entry-date[datetime]
  - Page:    Penci/Elementor theme — article body in penci-post-content or entry-content
  - Apply:   External link labelled "Apply Now" / "Apply Here" / "Click here to apply"
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


class OpportunitiesForAfricansScraper(BaseScraper):

    source_name = "opportunitiesforafricans"
    base_url = "https://www.opportunitiesforafricans.com/"
    exclude_domains = ["opportunitiesforafricans.com"]
    request_delay = 1.5

    # ── Listing page ───────────────────────────────────────────────────

    def fetch_opportunity_list(self) -> List[Dict[str, Any]]:
        response = requests.get(self.base_url, timeout=30)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, "html.parser")
        return self._parse_cards(soup)

    def _parse_cards(self, soup: BeautifulSoup) -> List[Dict[str, Any]]:
        """
        Scope to the "Latest Opportunities" section only.

        The section wrapper has class: home-featured-cat-content pwf-id-default style-6
        There are multiple home-featured-cat-content sections on the page
        (one per category), so we find the right one by looking for the
        heading that precedes it containing "latest" or "opportunities".
        Fallback: just take the first one.
        """
        section = None
        for wrapper in soup.find_all("div", class_="home-featured-cat-wrapper"):
            heading = wrapper.find_previous(
                lambda t: t.name in ("div", "h2", "h3", "span")
                and re.search(r"latest|recent|opportunit", t.get_text(strip=True), re.I)
                and len(t.get_text(strip=True)) < 60
            )
            if heading:
                section = wrapper.find("div", class_="home-featured-cat-content")
                break

        if not section:
            section = soup.find("div", class_="home-featured-cat-content")

        if not section:
            logger.error("[%s] Could not find the opportunities section", self.source_name)
            return []

        cards = section.find_all(
            lambda tag: tag.name == "div" and "mag-post-box" in tag.get("class", [])
        )

        results, seen = [], set()
        for card in cards:
            time_elem = card.find("time", class_="entry-date")
            if not time_elem or not time_elem.get("datetime"):
                continue

            title_elem = card.find("h3", class_="magcat-titlte")
            a = title_elem.find("a") if title_elem else None
            if not a:
                continue

            title = a.get_text(strip=True)
            link  = a.get("href", "")
            if not title or not link or link in seen:
                continue

            seen.add(link)
            results.append({
                "title":     title,
                "link":      link,
                "date_text": time_elem.get_text(strip=True),
                "datetime":  time_elem["datetime"],
            })

        return results

    # ── Individual opportunity page ────────────────────────────────────

    def scrape_opportunity_page(self, opp: Dict[str, Any]) -> Optional[str]:
        response = requests.get(opp["link"], timeout=15)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, "html.parser")

        article = soup.find("div", class_=re.compile(r"penci-post-content|entry-content|post-content"))
        if not article:
            logger.warning("  No article body found for %s", opp["link"])
            return None

        structured = self._extract_structured_fields(soup, article)
        logger.info(
            "  Pre-extracted: category=%s | deadline=%s | overview_keys=%s | apply=%s",
            structured.get("category", "—"),
            structured.get("deadline_text", "—")[:40],
            list(structured.get("quick_overview", {}).keys()),
            (structured.get("apply_link") or "—")[:60],
        )

        body_md = html_to_clean_md(
            article.decode_contents(),
            exclude_domains=self.exclude_domains,
        )

        return build_enriched_markdown(
            title=opp["title"],
            date_text=opp.get("date_text", ""),
            link=opp["link"],
            structured=structured,
            body_md=body_md,
        )

    def _extract_structured_fields(self, soup: BeautifulSoup, article) -> Dict[str, Any]:
        fields: Dict[str, Any] = {}

        # ── Category from breadcrumb (Home > Scholarships > ...) ─────
        breadcrumb = soup.find(
            lambda tag: tag.name in ("div", "nav", "span")
            and re.search(r"breadcrumb|crumbs", " ".join(tag.get("class", [])))
        )
        if breadcrumb:
            crumb_links = [
                a for a in breadcrumb.find_all("a")
                if a.get_text(strip=True).lower() != "home"
            ]
            if crumb_links:
                fields["category"] = crumb_links[-1].get_text(strip=True)

        # ── Deadline: OFA opens every post with "Application Deadline: ..." ──
        for elem in article.find_all(["p", "li", "strong", "b"]):
            text = elem.get_text(strip=True)
            if re.search(r"deadline", text, re.I) and len(text) < 120:
                fields["deadline_text"] = text
                break

        # ── Description: first real paragraph (skip the deadline line) ─
        for elem in article.find_all("p"):
            text = elem.get_text(strip=True)
            if not text or len(text) < 60:
                continue
            if re.search(r"^application deadline", text, re.I):
                continue
            if re.search(r"facebook|twitter|linkedin|whatsapp", text, re.I):
                continue
            fields["description"] = text
            break

        # ── Quick Overview bullet list (not all posts have this) ─────
        ov_heading = article.find(
            lambda t: t.name in ("h2", "h3", "h4", "strong", "b")
            and re.search(r"quick overview|at a glance|program details", t.get_text(strip=True), re.I)
        )
        if ov_heading:
            ul = ov_heading.find_next("ul")
            if ul:
                overview = {}
                for li in ul.find_all("li"):
                    text = re.sub(r"\s+", " ", li.get_text(separator=" ", strip=True))
                    if ":" in text:
                        k, _, v = text.partition(":")
                        k, v = k.strip().strip("*"), v.strip().strip("*")
                        if k and v and len(k) < 60:
                            overview[k] = v
                if overview:
                    fields["quick_overview"] = overview

        # ── Apply link ───────────────────────────────────────────────
        apply_pat = re.compile(r"apply\s*(now|here|online)?|click\s*here\s*to\s*apply", re.I)
        for a in article.find_all("a", href=True):
            href = a["href"]
            if any(d in href for d in self.exclude_domains) or not href.startswith("http"):
                continue
            if apply_pat.search(a.get_text(strip=True)) or apply_pat.search(href):
                fields["apply_link"] = href
                break

        return fields