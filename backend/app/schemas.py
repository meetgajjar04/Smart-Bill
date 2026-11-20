import re
from datetime import date, datetime
from decimal import Decimal
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, EmailStr, Field, condecimal, field_validator, model_validator

Category = Literal["electricity", "mobile", "internet", "water", "gas", "other"]
# Storage is DECIMAL(12,2); keep the API in the same exact-money domain.
Money = condecimal(max_digits=12, decimal_places=2, gt=Decimal("0"), le=Decimal("10000000"))


def _strong_password(v: str) -> str:
    if not (re.search(r"[A-Za-z]", v) and re.search(r"\d", v)):
        raise ValueError("Password must contain letters and numbers.")
    return v


class RegisterIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    email: EmailStr
    password: str = Field(min_length=10, max_length=128)

    @field_validator("password")
    @classmethod
    def strong(cls, v: str) -> str:
        return _strong_password(v)

    @field_validator("name")
    @classmethod
    def clean_name(cls, v: str) -> str:
        return v.strip()


class LoginIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class ProfileIn(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=80)
    notify_email: Optional[bool] = None


class PasswordChangeIn(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=10, max_length=128)

    @field_validator("new_password")
    @classmethod
    def strong(cls, v: str) -> str:
        return _strong_password(v)


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    email: str
    notify_email: bool


class BillIn(BaseModel):
    category: Category
    provider: Optional[str] = Field(default=None, max_length=80)
    account_number: Optional[str] = Field(default=None, max_length=40)
    bill_date: Optional[date] = None
    due_date: date
    amount: Money
    units: Optional[float] = Field(default=None, ge=0, le=1_000_000)
    notes: Optional[str] = Field(default=None, max_length=300)

    @field_validator("provider", "account_number", "notes")
    @classmethod
    def strip(cls, v):
        v = (v or "").strip()
        return v or None

    @field_validator("bill_date", "due_date")
    @classmethod
    def sane_year(cls, v):
        if v and not (2000 <= v.year <= 2100):
            raise ValueError("Date looks wrong — please check the year.")
        return v

    @model_validator(mode="after")
    def order(self):
        if self.bill_date and self.due_date < self.bill_date:
            raise ValueError("Due date cannot be before the bill date.")
        return self


class BillOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    category: str
    provider: Optional[str]
    account_number: Optional[str]
    bill_date: Optional[date]
    due_date: Optional[date]
    amount: Optional[float]
    units: Optional[float]
    notes: Optional[str]
    paid: bool
    paid_on: Optional[date]
    state: str
    reviewed: bool
    confidence: Optional[float]
    has_file: bool
    file_mime: Optional[str]
    analysis: Optional[dict]
    feedback: Optional[dict]
    created_at: datetime


class BillList(BaseModel):
    items: list[BillOut]
    total: int


class BulkRowError(BaseModel):
    row: int
    message: str


class BulkPreviewRow(BaseModel):
    row: int
    category: str = ""
    provider: str = ""
    account_number: str = ""
    bill_date: str = ""
    due_date: str = ""
    amount: str = ""
    units: str = ""
    notes: str = ""
    status: Literal["ready", "duplicate", "invalid"]
    message: str = ""


class BulkPreviewOut(BaseModel):
    total: int
    ready: int
    duplicates: int
    invalid: int
    token: str
    rows: list[BulkPreviewRow] = Field(default_factory=list)


class BulkImportOut(BaseModel):
    total: int
    created: int
    skipped: int
    errors: list[BulkRowError] = Field(default_factory=list)


class ChatTurn(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=1200)


class ChatIn(BaseModel):
    question: str = Field(min_length=2, max_length=600)
    history: list[ChatTurn] = Field(default_factory=list, max_length=24)  # the service keeps the newest 12

    @field_validator("question")
    @classmethod
    def clean_question(cls, v: str) -> str:
        v = " ".join(v.split()).strip()
        if len(v) < 2:
            raise ValueError("Ask a question first.")
        return v


class ExtractOut(BaseModel):
    bill: BillOut
    confidence: float
    missing: list[str]
    warnings: list[str]
    raw_text: str = ""  # what OCR actually read, so the user can verify it
    ai_used: bool = False             # Smart Bill AI (Ollama) was used to read this bill
    ai: Optional[dict] = None         # line items / notes / summary from the model


class PaymentIn(BaseModel):
    paid: bool
    paid_on: Optional[date] = None

    @field_validator("paid_on")
    @classmethod
    def sane_payment_date(cls, v):
        if v is None:
            return v
        if not (2000 <= v.year <= 2100) or v > date.today():
            raise ValueError("Paid date looks wrong — please check it.")
        return v


class AlertOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    bill_id: Optional[int]
    kind: str
    severity: str
    message: str
    is_read: bool
    created_at: datetime


# ---- internal (n8n) payloads ----
class AnalysisResultIn(BaseModel):
    is_anomaly: bool = False
    severity: Literal["none", "low", "medium", "high"] = "none"
    summary: str = Field(default="", max_length=500)
    forecast_next: Optional[float] = None
    top_tip: Optional[str] = Field(default=None, max_length=300)
    source: str = Field(default="n8n", max_length=20)


class InternalAlertIn(BaseModel):
    user_id: int
    bill_id: Optional[int] = None
    kind: Literal["anomaly", "reminder", "info"] = "info"
    severity: Literal["info", "warning", "critical"] = "info"
    message: str = Field(min_length=1, max_length=500)


# ---- household profile / personalisation ----
Zone = Literal["hot_humid", "hot_dry", "composite", "temperate", "cold"]


class HomeProfileIn(BaseModel):
    """All optional: users answer progressively. Send null to clear an answer."""
    city: Optional[str] = Field(default=None, max_length=60)
    climate_zone: Optional[Zone] = None
    area_sqft: Optional[int] = Field(default=None, ge=100, le=10000)
    occupants: Optional[int] = Field(default=None, ge=1, le=20)
    ac_count: Optional[int] = Field(default=None, ge=0, le=10)
    ac_hours_peak: Optional[float] = Field(default=None, ge=0, le=24)
    fridges: Optional[int] = Field(default=None, ge=0, le=4)
    electric_geyser: Optional[bool] = None
    induction: Optional[bool] = None
    washing_machine: Optional[bool] = None
    water_pump: Optional[bool] = None
    ev_km_per_day: Optional[float] = Field(default=None, ge=0, le=400)
    solar_kw: Optional[float] = Field(default=None, ge=0, le=50)
    wfh_days: Optional[int] = Field(default=None, ge=0, le=7)
    tariff_per_unit: Optional[float] = Field(default=None, gt=0, le=40)
    fixed_charge: Optional[float] = Field(default=None, ge=0, le=5000)

    @field_validator("city")
    @classmethod
    def clean_city(cls, v):
        v = (v or "").strip()
        return v or None


class WhatIfIn(BaseModel):
    ac_count: Optional[int] = Field(default=None, ge=0, le=10)
    ac_hours_peak: Optional[float] = Field(default=None, ge=0, le=24)
    electric_geyser: Optional[bool] = None
    solar_kw: Optional[float] = Field(default=None, ge=0, le=50)
    ev_km_per_day: Optional[float] = Field(default=None, ge=0, le=400)
    wfh_days: Optional[int] = Field(default=None, ge=0, le=7)


class FeedbackIn(BaseModel):
    reason: Literal["guests", "travel", "meter_issue", "new_appliance", "tariff_change", "seasonal", "other"]
    note: Optional[str] = Field(default=None, max_length=200)
