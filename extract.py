"""
Step 2: Extract, rewrite, and translate opportunities from Markdown files.

Pipeline per file:
  [LLM Call 1] Extract structured fields + rewrite description/eligibility
               in a consistent editorial voice (English)
  [LLM Call 2] Translate the written content fields to Egyptian Arabic
  [Quality Gate] Validate required fields before DB insert.
                 Blocks anything not explicitly open to Egyptians.
"""

import json
import re
import random
import time
import uuid
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import psycopg2
from psycopg2.extras import Json
from openai import OpenAI

from countries import normalize_country, normalize_countries
from config import (
    DB_CONFIG,
    GROQ_API_KEY,
    CEREBRAS_API_KEY,
    LLM_MODEL_GROQ,
    LLM_MODEL_CEREBRAS,
    OUTPUT_DIR,
    SOURCE_META_PATH,
    OPPORTUNITIES_JSON,
)

logger = logging.getLogger(__name__)


# ── LLM client (round-robin Groq / Cerebras) ──────────────────────────

_CLIENTS: List[Dict] = []
_client_index = 0


def _init_clients():
    global _CLIENTS
    if _CLIENTS:
        return
    if GROQ_API_KEY:
        _CLIENTS.append({
            "client": OpenAI(api_key=GROQ_API_KEY, base_url="https://api.groq.com/openai/v1"),
            "model": LLM_MODEL_GROQ,
            "name": "groq",
        })
    if CEREBRAS_API_KEY:
        _CLIENTS.append({
            "client": OpenAI(api_key=CEREBRAS_API_KEY, base_url="https://api.cerebras.ai/v1/"),
            "model": LLM_MODEL_CEREBRAS,
            "name": "cerebras",
        })
    if not _CLIENTS:
        raise RuntimeError("No LLM API keys configured (GROQ_API_KEY / CEREBRAS_API_KEY)")
    logger.info("LLM clients ready: %s", ", ".join(c["name"] for c in _CLIENTS))


def _get_next_client() -> Dict:
    global _client_index
    entry = _CLIENTS[_client_index % len(_CLIENTS)]
    _client_index += 1
    return entry


def llm_call(messages, temperature=0.3, max_tokens=5000) -> str:
    _init_clients()
    primary = _get_next_client()
    try:
        logger.info("LLM call → %s (%s)", primary["name"], primary["model"])
        resp = primary["client"].chat.completions.create(
            model=primary["model"],
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        logger.error("%s FAILED: %s — trying fallback", primary["name"], str(e)[:120])
        fallback = _get_next_client()
        try:
            resp = fallback["client"].chat.completions.create(
                model=fallback["model"],
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            return resp.choices[0].message.content.strip()
        except Exception as e2:
            logger.error("%s FAILED (fallback): %s", fallback["name"], str(e2)[:120])
            raise


def parse_json_response(text: str) -> Any:
    """Strip markdown fences and parse JSON robustly."""
    if "```" in text:
        matches = re.findall(r"```json(.*?)```|```(.*?)```", text, re.S)
        if matches:
            text = next(x for pair in matches for x in pair if x).strip()
    if not text.startswith(("{", "[")):
        start = text.find("{")
        if start != -1:
            text = text[start:]
    return json.loads(text)


# ═══════════════════════════════════════════════════════════════════════
# CALL 1 — Extract + Rewrite (English)
# ═══════════════════════════════════════════════════════════════════════

EXTRACT_SYSTEM_PROMPT = """\
You are an editorial assistant for a student opportunities platform serving Egyptian and Arab students.

You receive a Markdown document about one opportunity (scholarship, internship, fellowship, conference, etc.)
scraped from an external website.

Your job is TWO things in a single response:

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PART A — EXTRACT structured fields
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Extract the fields listed in the schema below.
The "Quick Overview" block in the document already has most structured
fields pre-parsed — use those as your primary source. Prose fills in the rest.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PART B — REWRITE written content
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Rewrite "description" and "eligibility" from scratch in our editorial voice.
Do NOT copy-paste from the source. Do NOT start with "Applications are now open for...".

Our editorial voice (English):
  • Direct and encouraging — like a knowledgeable friend who found a great deal.
  • Short sentences. Active voice. Zero filler.
  • Description: 3–4 sentences max. Lead with what makes this opportunity worth applying to.
    Mention the host, the duration, and the key benefit (funded/paid/stipend).
  • Eligibility: 1–2 clean sentences summarizing who can apply.
    Do not bullet-dump — synthesize into prose.
  • Title: Remove funding tags in parentheses like "(Fully Funded)", "(Paid)", "(Funded)".
    Keep the year and the core name only.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
RULES (apply to both parts):
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
• Return ONLY a single valid JSON object — no explanation, no markdown fences.
• Omit any field that is genuinely absent from the source. Never invent data.
• Dates must be YYYY-MM-DD. If only month+year known, use the 1st of the month.
• country: always use short normalized names: USA, UK, UAE, Saudi Arabia, South Korea, etc.
  Never use full official names like "United States of America".
• fund_type: array, values from [fully_funded, partially_funded, paid, stipend, self_funded].
• type.subtype: array from [masters, bachelor, phd, internship, fellowship,
    conference, exchange, summer_school, workshop, prize, volunteering, camp].
  A program is "academic" only if it awards a degree or research fellowship. Everything else is non_academic.
• target_segment: array from [high_school, undergraduate, graduate].
  When unclear, include all three.
• documents_required: use canonical names → cv, transcript, motivation_letter,
    cover_letter, recommendation_letter, portfolio, research_proposal, passport_copy.
• language_requirements: only if explicitly stated. Format: {"IELTS": "6.5"} or {"TOEFL": ""}.
• application_fee: only if a specific non-zero amount is stated.
• is_remote: true only for fully online programs.
• benefits: short list of strings, each a concrete benefit (e.g. "Round-trip flights",
    "Monthly stipend of USD 600", "Free accommodation"). No vague entries like "Full support".

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
open_to_egyptians FIELD — CRITICAL
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
This is the most important field. Our platform serves Egyptian students exclusively.

Set open_to_egyptians to true if ANY of the following apply:
  - The opportunity is open to all nationalities / international students
  - Egypt or Egyptian nationals are explicitly mentioned as eligible
  - Arab countries are eligible (Egypt is an Arab country)
  - African countries are eligible (Egypt is in Africa)
  - No nationality restriction is mentioned at all

Set open_to_egyptians to false ONLY if there is an explicit restriction that excludes Egypt:
  - Restricted to citizens of specific countries and Egypt is NOT among them
  - Explicitly excludes Egyptian or Arab nationals
  - Restricted to a regional bloc Egypt doesn't belong to (e.g. "EU citizens only",
    "ASEAN countries only", "Latin American students only")

If you are not sure → set to true. Only set false when exclusion is explicit and clear.

JSON SCHEMA (include only fields present in the source):
{
  "title": "clean title, no funding tags",
  "description": "rewritten 3-4 sentence editorial description in English",
  "eligibility": "rewritten 1-2 sentence eligibility summary in English",
  "country": "string or array of normalized country names",
  "start_date": "YYYY-MM-DD",
  "deadline": "YYYY-MM-DD",
  "duration": "e.g. 12 months",
  "fund_type": ["fully_funded"],
  "benefits": ["Round-trip flights", "Accommodation", "..."],
  "gpa": "3.0",
  "type": {
    "category": "academic | non_academic",
    "subtype": ["fellowship"]
  },
  "application_fee": "USD 50",
  "application_link": "https://...",
  "official_website": "https://...",
  "target_segment": ["undergraduate", "graduate"],
  "language_requirements": {"IELTS": "6.5"},
  "documents_required": ["cv", "motivation_letter"],
  "is_remote": false,
  "open_to_egyptians": true
}
"""


def extract_and_rewrite(markdown_content: str, filename: str) -> Optional[Dict[str, Any]]:
    """LLM Call 1: extract structured fields + rewrite description/eligibility in English."""
    user_prompt = (
        "Here is the opportunity document:\n\n"
        "---\n"
        f"{markdown_content}\n"
        "---\n\n"
        "Return a single JSON object following the schema. "
        "Remember: rewrite description and eligibility in our editorial voice — "
        "do not copy from the source. "
        "And always include open_to_egyptians as true or false."
    )
    try:
        raw = llm_call(
            messages=[
                {"role": "system", "content": EXTRACT_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.4,
            max_tokens=5000,
        )
        data = parse_json_response(raw)
        if not isinstance(data, dict):
            raise ValueError(f"Expected dict, got {type(data)}")
        data["id"] = str(uuid.uuid4())
        data["_source_file"] = filename
        return data
    except Exception as e:
        logger.error("Extract+rewrite failed for %s: %s", filename, str(e)[:120])
        return None


# ═══════════════════════════════════════════════════════════════════════
# CALL 2 — Translate written content to Egyptian Arabic
# ═══════════════════════════════════════════════════════════════════════

TRANSLATABLE_FIELDS = {"title", "description", "eligibility", "benefits", "duration"}

TRANSLATE_SYSTEM_PROMPT = """\
You are an Arabic content writer for a student opportunities platform targeting Egyptian and Arab youth.

You will receive a JSON object describing one opportunity. Your job is to translate ONLY the
text-content fields into Egyptian Arabic, rewritten in our platform's voice:

Our Arabic editorial voice:
  • Egyptian Arabic dialect (عامية مصرية محترمة) — conversational and warm, not formal MSA (فصحى).
  • Direct and encouraging — like a friend telling you about a great opportunity.
  • Short sentences. Active voice. No stiff translations of English marketing phrases.
  • "description": rewrite in Arabic naturally — do NOT translate word-for-word.
    Lead with the benefit/hook. Sound like a real person, not a press release.
  • "eligibility": synthesize into 1-2 natural Arabic sentences.
  • "benefits": each item should read naturally in Arabic, not like a literal translation.
  • "title": keep the official name (proper nouns, numbers, year) but translate surrounding words.

Fields to translate (values only, keep keys in English):
  title, description, eligibility, benefits, duration

Fields to keep exactly as-is (do NOT translate):
  id, country, deadline, start_date, fund_type, type, application_link,
  official_website, documents_required, language_requirements,
  application_fee, gpa, is_remote, target_segment, open_to_egyptians,
  and any field not listed above.

Return ONLY a valid JSON object — no explanation, no markdown fences.
"""


def translate_to_arabic(data_en: Dict[str, Any]) -> Dict[str, Any]:
    """LLM Call 2: translate written content fields to Egyptian Arabic."""
    preserved = {k: v for k, v in data_en.items() if k not in TRANSLATABLE_FIELDS}
    to_translate = {k: v for k, v in data_en.items() if k in TRANSLATABLE_FIELDS and v}

    if not to_translate:
        logger.warning("Nothing to translate — returning English data as Arabic")
        return data_en.copy()

    user_prompt = (
        "Translate the following opportunity fields to Egyptian Arabic in our editorial voice.\n\n"
        f"{json.dumps(to_translate, ensure_ascii=False, indent=2)}\n\n"
        "Return a JSON object with only these translated fields."
    )
    try:
        raw = llm_call(
            messages=[
                {"role": "system", "content": TRANSLATE_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.4,
            max_tokens=3000,
        )
        translated_fields = parse_json_response(raw)
        if not isinstance(translated_fields, dict):
            raise ValueError(f"Expected dict, got {type(translated_fields)}")
        return {**preserved, **translated_fields}
    except Exception as e:
        logger.warning("Arabic translation failed: %s — using English fallback", str(e)[:120])
        return data_en.copy()


# ═══════════════════════════════════════════════════════════════════════
# Quality Gate
# ═══════════════════════════════════════════════════════════════════════

REQUIRED_FIELDS = ["title", "description", "application_link", "type", "target_segment"]


def quality_check(data: Dict[str, Any], filename: str) -> List[str]:
    """
    Return a list of quality issues. Empty list = passes gate.
    Blocking issues prevent DB save entirely.
    """
    issues = []

    # Required fields
    for field in REQUIRED_FIELDS:
        if not data.get(field):
            issues.append(f"missing required field: {field}")

    # Egyptian eligibility — blocking
    if data.get("open_to_egyptians") is not True:
        issues.append("not open to egyptians")

    # Description sanity
    desc = data.get("description", "")
    banned_openers = [
        "applications are now open for",
        "we are pleased to announce",
        "it is a fully funded",
        "the applications are now open",
    ]
    if desc and any(desc.lower().startswith(b) for b in banned_openers):
        issues.append("description looks copy-pasted (starts with banned opener)")

    if desc and len(desc) < 50:
        issues.append(f"description too short ({len(desc)} chars)")
    if desc and len(desc) > 1000:
        issues.append(f"description suspiciously long ({len(desc)} chars) — check rewrite")

    # Funding tag leaked into title
    title = data.get("title", "")
    if re.search(r"\((fully funded|paid|funded|stipend)\)", title, re.I):
        issues.append("funding tag still in title — LLM didn't clean it")

    # Deadline in the past
    deadline = data.get("deadline")
    if deadline:
        try:
            dl = datetime.strptime(deadline, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            days_past = (datetime.now(timezone.utc) - dl).days
            if days_past > 30:
                issues.append(f"deadline {deadline} is {days_past} days in the past")
        except ValueError:
            issues.append(f"deadline '{deadline}' is not valid YYYY-MM-DD")

    # Apply link must be external
    apply_link = data.get("application_link", "")
    if apply_link and "opportunitiescorners.com" in apply_link:
        issues.append("application_link points to opportunitiescorners.com, not the official site")

    for issue in issues:
        logger.warning("  [QA] %s — %s", filename, issue)

    return issues


# ═══════════════════════════════════════════════════════════════════════
# Country normalization
# ═══════════════════════════════════════════════════════════════════════

def normalize_opp_countries(data: dict):
    if "country" in data:
        raw = data["country"]
        data["country"] = normalize_countries(raw if isinstance(raw, list) else [raw])


# ═══════════════════════════════════════════════════════════════════════
# DB helpers
# ═══════════════════════════════════════════════════════════════════════

def ensure_list(val):
    if val is None:
        return None
    return [val] if isinstance(val, str) else list(val)


def parse_date(val):
    if not val or not isinstance(val, str):
        return None
    return val if re.match(r"^\d{4}-\d{2}-\d{2}$", val) else None


def save_to_db(
    opportunity_id: str,
    data_en: dict,
    data_ar: dict,
    source: str = "opportunitiescorners",
    source_url: str = None,
    source_md: str = None,
):
    normalize_opp_countries(data_en)

    conn = psycopg2.connect(**DB_CONFIG)
    cur = conn.cursor()
    now = datetime.now(timezone.utc)

    type_info = data_en.get("type") or {}
    cur.execute(
        """
        INSERT INTO opportunities (
            id, source, source_url, source_md,
            data_en, data_ar,
            category, subtype, country, fund_type, target_segment,
            deadline, is_remote, open_to_egyptians,
            created_at, updated_at
        )
        VALUES (
            %s::uuid, %s, %s, %s,
            %s, %s,
            %s, %s, %s, %s, %s,
            %s, %s, %s,
            %s, %s
        )
        ON CONFLICT (id) DO UPDATE SET
            source = EXCLUDED.source,
            source_url = EXCLUDED.source_url,
            source_md = EXCLUDED.source_md,
            data_en = EXCLUDED.data_en,
            data_ar = EXCLUDED.data_ar,
            category = EXCLUDED.category,
            subtype = EXCLUDED.subtype,
            country = EXCLUDED.country,
            fund_type = EXCLUDED.fund_type,
            target_segment = EXCLUDED.target_segment,
            deadline = EXCLUDED.deadline,
            is_remote = EXCLUDED.is_remote,
            open_to_egyptians = EXCLUDED.open_to_egyptians,
            updated_at = EXCLUDED.updated_at;
        """,
        (
            opportunity_id, source, source_url, source_md,
            Json(data_en), Json(data_ar),
            type_info.get("category"),
            ensure_list(type_info.get("subtype")),
            ensure_list(data_en.get("country")),
            ensure_list(data_en.get("fund_type")),
            ensure_list(data_en.get("target_segment")),
            parse_date(data_en.get("deadline")),
            bool(data_en.get("is_remote", False)),
            True,  # only reaches DB if quality gate passed open_to_egyptians check
            now, now,
        ),
    )
    conn.commit()
    cur.close()
    conn.close()


# ═══════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════

def run() -> bool:
    source_metadata = {}
    if SOURCE_META_PATH.exists():
        with open(SOURCE_META_PATH, encoding="utf-8") as f:
            source_metadata = json.load(f)
        logger.info("Loaded source metadata for %d files", len(source_metadata))

    markdown_files = sorted(OUTPUT_DIR.glob("*.md"))
    logger.info("Found %d markdown files to process", len(markdown_files))

    if not markdown_files:
        logger.warning("No markdown files in %s — run scrape step first", OUTPUT_DIR)
        return False

    all_en_data = []
    saved = failed = skipped_qa = 0

    for idx, file_path in enumerate(markdown_files, 1):
        logger.info("[%d/%d] %s", idx, len(markdown_files), file_path.name[:60])

        try:
            markdown_content = file_path.read_text(encoding="utf-8")
            meta = source_metadata.get(file_path.name, {})
            source = meta.get("source", "opportunitiescorners")
            source_url = meta.get("source_url", "")

            # ── Call 1: extract + rewrite ─────────────────────────────
            data_en = extract_and_rewrite(markdown_content, file_path.name)
            if not data_en:
                logger.warning("  No data extracted — skipping")
                failed += 1
                continue

            opp_id = data_en["id"]

            # ── Quality gate ──────────────────────────────────────────
            issues = quality_check(data_en, file_path.name)
            blocking = [
                i for i in issues
                if "missing required field" in i
                or "application_link" in i
                or "not open to egyptians" in i
            ]
            if blocking:
                logger.warning("  BLOCKED by %d quality issue(s) — skipping", len(blocking))
                skipped_qa += 1
                continue
            if issues:
                logger.warning("  %d non-blocking quality issue(s) — saving with warnings", len(issues))

            # ── Call 2: translate to Egyptian Arabic ──────────────────
            logger.info("  Translating to Egyptian Arabic...")
            en_clean = {k: v for k, v in data_en.items() if not k.startswith("_")}
            data_ar = translate_to_arabic(en_clean)

            # ── Save to DB ────────────────────────────────────────────
            save_to_db(
                opportunity_id=opp_id,
                data_en={k: v for k, v in data_en.items() if not k.startswith("_")},
                data_ar=data_ar,
                source=source,
                source_url=source_url,
                source_md=markdown_content,
            )
            logger.info("  ✓ Saved to DB")
            all_en_data.append(en_clean)
            saved += 1

            time.sleep(random.uniform(8, 15))

        except Exception as e:
            logger.error("  Unhandled error: %s", str(e)[:120])
            failed += 1

    with open(OPPORTUNITIES_JSON, "w", encoding="utf-8") as f:
        json.dump(all_en_data, f, ensure_ascii=False, indent=2)

    logger.info(
        "Extract done — saved: %d | failed: %d | blocked by QA: %d",
        saved, failed, skipped_qa,
    )
    return saved > 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    run()