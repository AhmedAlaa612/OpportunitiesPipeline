"""HTTP entrypoint for manually submitted opportunities."""

import secrets
import uuid
from datetime import date
from typing import Annotated, Literal, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator

try:
    from config import FORM_WEBHOOK_SECRET
    from countries import normalize_countries
    from embed import embed_and_upsert
    from helpers.db import upsert_opportunity
except ImportError:
    from .config import FORM_WEBHOOK_SECRET
    from .countries import normalize_countries
    from .embed import embed_and_upsert
    from .helpers.db import upsert_opportunity


FundType = Literal[
    "fully_funded",
    "partially_funded",
    "paid",
    "stipend",
    "self_funded",
]
OpportunityCategory = Literal["academic", "non_academic"]
OpportunitySubtype = Literal[
    "masters",
    "bachelor",
    "phd",
    "internship",
    "fellowship",
    "conference",
    "exchange",
    "summer_school",
    "workshop",
    "prize",
    "volunteering",
    "camp",
]
TargetSegment = Literal["high_school", "undergraduate", "graduate"]


class ManualOpportunityPayload(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    title_en: str = Field(min_length=3)
    title_ar: str = Field(min_length=3)
    description_en: str = Field(min_length=20)
    description_ar: str = Field(min_length=20)
    application_link: HttpUrl
    fund_type: list[FundType] = Field(min_length=1)
    category: OpportunityCategory
    subtype: list[OpportunitySubtype] = Field(min_length=1)
    target_segment: list[TargetSegment] = Field(min_length=1)
    eligibility_en: Optional[str] = None
    eligibility_ar: Optional[str] = None
    country: list[str] = Field(default_factory=list)
    deadline: Optional[str] = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    is_remote: bool = False
    open_to_egyptians: bool = True

    @field_validator("fund_type", "category", "subtype", "target_segment", mode="before")
    @classmethod
    def strip_enum_values(cls, value):
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, list):
            return [item.strip() if isinstance(item, str) else item for item in value]
        return value

    @field_validator("deadline")
    @classmethod
    def validate_calendar_date(cls, value):
        if value is not None:
            date.fromisoformat(value)
        return value


app = FastAPI()


def require_webhook_secret(
    x_webhook_secret: Annotated[Optional[str], Header(alias="X-Webhook-Secret")] = None,
) -> None:
    if (
        not FORM_WEBHOOK_SECRET
        or x_webhook_secret is None
        or not secrets.compare_digest(
            x_webhook_secret.encode("utf-8"), FORM_WEBHOOK_SECRET.encode("utf-8")
        )
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid webhook secret",
        )


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/api/v1/opportunities/manual", status_code=status.HTTP_201_CREATED)
def ingest_manual_opportunity(
    payload: ManualOpportunityPayload,
    _authenticated: Annotated[None, Depends(require_webhook_secret)],
):
    opportunity_id = str(uuid.uuid4())
    countries = normalize_countries(payload.country) if payload.country else None
    type_info = {"category": payload.category, "subtype": payload.subtype}

    data_en = {
        "id": opportunity_id,
        "title": payload.title_en,
        "description": payload.description_en,
        "eligibility": payload.eligibility_en or "",
        "country": countries,
        "deadline": payload.deadline,
        "fund_type": payload.fund_type,
        "type": type_info,
        "application_link": str(payload.application_link),
        "target_segment": payload.target_segment,
        "is_remote": payload.is_remote,
        "open_to_egyptians": payload.open_to_egyptians,
        "is_active": True,
    }
    data_ar = {
        "id": opportunity_id,
        "title": payload.title_ar,
        "description": payload.description_ar,
        "eligibility": payload.eligibility_ar or "",
        "country": countries,
        "deadline": payload.deadline,
        "fund_type": payload.fund_type,
        "type": type_info,
        "application_link": str(payload.application_link),
        "target_segment": payload.target_segment,
        "is_remote": payload.is_remote,
        "open_to_egyptians": payload.open_to_egyptians,
        "is_active": True,
    }

    upsert_opportunity(
        opportunity_id=opportunity_id,
        data_en=data_en,
        data_ar=data_ar,
        source="manual_form",
        source_url=str(payload.application_link),
        is_active=True,
    )
    embed_and_upsert([data_en])

    return {"id": opportunity_id, "status": "indexed", "is_active": True}