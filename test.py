"""
Standalone test for the Opportunit4U scraper — Recent Posts only.

Run from the pipeline/ directory:
    python test_opportunit4u.py

Saves .md files into ./test_output/  — no DB, no config, no pipeline deps.
"""

import logging
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from bs4 import BeautifulSoup
from markdownify import markdownify as mdify

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

OUTPUT_DIR = Path("test_output")
OUTPUT_DIR.mkdir(exist_ok=True)

BASE_URL = "https://www.opportunit4u.com/"
EXCLUDE  = "opportunit4u.com"

# ── Helpers ────────────────────────────────────────────────────────────

def html_to_md(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "header", "footer", "nav"]):
        tag.decompose()
    for a in soup.find_all("a", href=True):
        if EXCLUDE in a["href"]:
            (a.find_parent("p") or a).decompose()
    return mdify(soup.decode_contents(), heading_style="ATX", strip=["img"])

def sanitize(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', "", name).strip()[:100] or "opportunity"

# ── Fetch listing ──────────────────────────────────────────────────────

def fetch_list() -> List[Dict[str, Any]]:
    logger.info("Fetching listing page: %s", BASE_URL)
    soup = BeautifulSoup(requests.get(BASE_URL, timeout=30).content, "html.parser")

    # The "Recent posts" section is div.widget.Blog#Blog1
    # Each post is: div.blog-post > div.index-post-inside-wrap
    #   h2.post-title > a            ← title + link
    #   span.post-date.published[datetime]  ← ISO datetime
    #   a.post-tag                   ← category

    blog_widget = soup.find("div", class_="Blog")
    if not blog_widget:
        logger.error("Could not find div.Blog — site structure may have changed")
        return []

    results, seen = [], set()
    for card in blog_widget.find_all("div", class_="blog-post"):
        # Title + link
        h2 = card.find("h2", class_="post-title")
        a  = h2.find("a", href=True) if h2 else None
        if not a:
            continue
        title = a.get_text(strip=True)
        link  = a["href"]
        if not link or link in seen or not title:
            continue

        # Datetime from the <span class="post-date published" datetime="...">
        date_span = card.find("span", class_="post-date")
        date_text = date_span.get_text(strip=True) if date_span else ""
        iso_dt    = date_span.get("datetime", "") if date_span else ""

        # Category from a.post-tag
        tag_a     = card.find("a", class_="post-tag")
        category  = tag_a.get_text(strip=True) if tag_a else ""

        seen.add(link)
        results.append({
            "title":     title,
            "link":      link,
            "date_text": date_text,
            "datetime":  iso_dt,
            "category":  category,
        })
        logger.info("  Found: %s", title[:80])

    logger.info("Total posts found: %d", len(results))
    return results

# ── Scrape individual post page ────────────────────────────────────────

def scrape_page(opp: Dict[str, Any]) -> Optional[str]:
    soup = BeautifulSoup(requests.get(opp["link"], timeout=15).content, "html.parser")

    # Article body — Blogger uses .post-body or .entry-content
    article = (
        soup.find("div", class_="post-body")
        or soup.find("div", class_="entry-content")
        or soup.find("article")
    )
    if not article:
        logger.warning("  No article body found — skipping")
        return None

    # First long paragraph as description
    description = ""
    for elem in article.find_all(["p", "h2", "h3"]):
        text = elem.get_text(strip=True)
        if "overview" in text.lower():
            break
        if elem.name == "p" and len(text) > 60:
            description = text
            break

    # Quick overview bullet list
    overview: Dict[str, str] = {}
    ov_heading = article.find(
        lambda t: t.name in ("h2", "h3", "h4", "strong", "p", "b")
        and "overview" in t.get_text(strip=True).lower()
    )
    if ov_heading:
        ul = ov_heading.find_next("ul")
        if ul:
            for li in ul.find_all("li"):
                text = re.sub(r"\s+", " ", li.get_text(separator=" ", strip=True))
                if ":" in text:
                    k, _, v = text.partition(":")
                    k, v = k.strip().strip("*"), v.strip().strip("*")
                    if k and v and len(k) < 60:
                        overview[k] = v

    # Apply link
    apply_link = ""
    apply_pat = re.compile(r"apply|official|application", re.I)
    for a in article.find_all("a", href=True):
        if EXCLUDE in a["href"]:
            continue
        if apply_pat.search(a.get_text(strip=True)) or apply_pat.search(a["href"]):
            apply_link = a["href"]
            break

    body_md = html_to_md(article.decode_contents())

    # Build enriched markdown
    lines = [f"# {opp['title']}"]
    if opp.get("category"):
        lines.append(f"**Category:** {opp['category']}")
    if description:
        lines += ["", "## Summary", "", description]
    if overview:
        lines += ["", "## Quick Overview", ""]
        for k, v in overview.items():
            lines.append(f"- **{k}:** {v}")
    if apply_link:
        lines += ["", f"**Apply Link:** {apply_link}"]
    lines += ["", "---", "", body_md]
    return "\n".join(lines)

# ── Main ───────────────────────────────────────────────────────────────

def main():
    posts = fetch_list()
    if not posts:
        sys.exit(1)

    ok, fail = 0, 0
    for i, opp in enumerate(posts, 1):
        logger.info("[%d/%d] Scraping: %s", i, len(posts), opp["title"][:70])
        try:
            content = scrape_page(opp)
            if not content:
                fail += 1
                continue
            path = OUTPUT_DIR / (sanitize(opp["title"]) + ".md")
            path.write_text(content, encoding="utf-8")
            logger.info("  Saved → %s", path.name)
            ok += 1
        except Exception as e:
            logger.error("  Error: %s", e)
            fail += 1

    print(f"\nDone — {ok} saved to ./{OUTPUT_DIR}/, {fail} failed")
    print("\nFiles:")
    for f in sorted(OUTPUT_DIR.glob("*.md")):
        print(f"  {f.name}")

if __name__ == "__main__":
    main()