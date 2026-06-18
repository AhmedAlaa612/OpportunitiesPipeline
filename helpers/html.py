"""
Shared HTML processing utilities used by all scrapers.
"""

import re
import logging
from typing import List, Optional

from bs4 import BeautifulSoup
from markdownify import markdownify as md

logger = logging.getLogger(__name__)


def html_to_clean_md(html: str, exclude_domains: Optional[List[str]] = None) -> str:
    """
    Convert raw HTML to clean Markdown.

    Strips scripts/styles/nav, removes links to excluded domains,
    and converts the rest to ATX-style Markdown (no images).
    """
    try:
        soup = BeautifulSoup(html, "html.parser")

        for tag in soup(["script", "style", "noscript", "header", "footer", "nav"]):
            tag.decompose()

        for button in soup.find_all("button"):
            a = button.find("a", href=True)
            if a:
                button.replace_with(a)

        if exclude_domains:
            for a_tag in soup.find_all("a", href=True):
                href = a_tag.get("href", "")
                if href and any(domain in href for domain in exclude_domains):
                    p_tag = a_tag.find_parent("p")
                    if p_tag and "Also Check" in p_tag.get_text():
                        p_tag.decompose()
                    else:
                        parent = a_tag.find_parent()
                        if parent and parent.name != "body":
                            try:
                                parent.decompose()
                            except Exception:
                                pass

        return md(soup.decode_contents(), heading_style="ATX", strip=["img"])

    except Exception as e:
        logger.error("html_to_clean_md error: %s", e)
        return ""


def sanitize_filename(filename: str) -> str:
    """Strip characters that are illegal in filenames and truncate to 100 chars."""
    sanitized = re.sub(r'[<>:"/\\|?*]', "", filename).strip()[:100]
    return sanitized if sanitized else "opportunity"
