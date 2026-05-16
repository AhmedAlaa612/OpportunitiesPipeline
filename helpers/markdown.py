"""
Markdown builder shared by all scrapers.

Produces the enriched Markdown document that is later fed to the LLM
in the extract step.
"""

from typing import Any, Dict


def build_enriched_markdown(
    title: str,
    date_text: str,
    link: str,
    structured: Dict[str, Any],
    body_md: str,
) -> str:
    """
    Prepend pre-extracted structured fields as a clean block before the
    raw Markdown body. This gives the LLM reliable anchors for the most
    important fields (deadline, country, apply link, etc.) even when the
    article body is noisy or inconsistently formatted.

    Args:
        title:      Opportunity title from the listing card.
        date_text:  Human-readable publish date (e.g. "February 20, 2026").
        link:       Source URL of the individual opportunity page.
        structured: Dict of pre-extracted fields (category, description,
                    quick_overview, apply_link) from extract_structured_fields().
        body_md:    Full article body converted to Markdown.

    Returns:
        A single Markdown string ready to be saved as a .md file.
    """
    lines = [f"# {title}"]

    if structured.get("category"):
        lines.append(f"**Category:** {structured['category']}")

    if structured.get("description"):
        lines += ["", "## Summary", "", structured["description"]]

    if structured.get("quick_overview"):
        lines += ["", "## Quick Overview", ""]
        for k, v in structured["quick_overview"].items():
            lines.append(f"- **{k}:** {v}")

    if structured.get("apply_link"):
        lines += ["", f"**Apply Link:** {structured['apply_link']}"]

    lines += ["", "---", "", body_md]
    return "\n".join(lines)
