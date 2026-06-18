"""
Standalone test for the OpportunitiesForAfricans scraper.

Run from the pipeline/ directory:
    python test_opportunitiesforafricans.py

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

BASE_URL = "https://www.opportunitiesforafricans.com/"
EXCLUDE  = "opportunitiesforafricans.com"

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

    # Scope to the specific "Latest Opportunities" section only.
    # The container is: div.home-featured-cat-content (inside div.home-featured-cat-wrapper)
    # There may be multiple such sections on the page — find the one whose
    # heading text contains "latest" or "recent".
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

    # Fallback: just take the first home-featured-cat-content on the page
    if not section:
        section = soup.find("div", class_="home-featured-cat-content")

    if not section:
        logger.error("Could not find the opportunities section — site structure may have changed")
        return []

    # Now search for cards only within this section
    cards = section.find_all(
        lambda tag: tag.name == "div" and "mag-post-box" in tag.get("class", [])
    )
    if not cards:
        logger.error("Could not find div.mag-post-box cards — site structure may have changed")
        return []

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
        logger.info("  Found: %s", title[:80])

    logger.info("Total posts found: %d", len(results))
    return results

# ── Scrape individual post page ────────────────────────────────────────

def scrape_page(opp: Dict[str, Any]) -> Optional[str]:
    soup = BeautifulSoup(requests.get(opp["link"], timeout=15).content, "html.parser")

    # OFA uses a Penci/Elementor theme — article body is in one of these
    article = (
        soup.find("div", class_=re.compile(r"penci-post-content|entry-content|post-content"))
    )
    if not article:
        logger.warning("  No article body found — skipping")
        return None

    # Category from breadcrumb (Home > Scholarships > ...)
    category = ""
    breadcrumb = soup.find(
        lambda tag: tag.name in ("div", "nav", "span")
        and re.search(r"breadcrumb|crumbs", " ".join(tag.get("class", [])))
    )
    if breadcrumb:
        crumb_links = [a for a in breadcrumb.find_all("a") if a.get_text(strip=True).lower() != "home"]
        if crumb_links:
            category = crumb_links[-1].get_text(strip=True)

    # Deadline: OFA consistently opens with "Application Deadline: DD Month YYYY"
    deadline_text = ""
    for elem in article.find_all(["p", "li", "strong", "b"]):
        text = elem.get_text(strip=True)
        if re.search(r"deadline", text, re.I) and len(text) < 120:
            deadline_text = text
            break

    # Description: first real paragraph after the deadline line
    description = ""
    for elem in article.find_all("p"):
        text = elem.get_text(strip=True)
        if not text or len(text) < 60:
            continue
        if re.search(r"^application deadline", text, re.I):
            continue
        if re.search(r"facebook|twitter|linkedin|whatsapp", text, re.I):
            continue
        description = text
        break

    # Quick overview bullet list (not all OFA posts have this)
    overview: Dict[str, str] = {}
    ov_heading = article.find(
        lambda t: t.name in ("h2", "h3", "h4", "strong", "b")
        and re.search(r"quick overview|at a glance|program details", t.get_text(strip=True), re.I)
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

    # Apply link — OFA labels these "Apply Now", "Apply Here", "Click here to apply"
    apply_link = ""
    apply_pat = re.compile(r"apply\s*(now|here|online)?|click\s*here\s*to\s*apply", re.I)
    for a in article.find_all("a", href=True):
        href = a["href"]
        if EXCLUDE in href or not href.startswith("http"):
            continue
        if apply_pat.search(a.get_text(strip=True)) or apply_pat.search(href):
            apply_link = href
            break

    body_md = html_to_md(article.decode_contents())

    # Build enriched markdown
    lines = [f"# {opp['title']}"]
    if category:
        lines.append(f"**Category:** {category}")
    if deadline_text:
        lines.append(f"**{deadline_text}**")
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