from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import (JSON, Boolean, CheckConstraint, Date, DateTime, Float, ForeignKey, Index, Integer,
                        Numeric, String, Text, UniqueConstraint, text)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base

CATEGORIES = ("electricity", "mobile", "internet", "water", "gas", "other")
_CATEGORY_VALUES = ", ".join(f"'{c}'" for c in CATEGORIES)


def utcnow() -> datetime:
    """Current UTC time as a naive datetime, matching the DATETIME columns."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def as_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """Normalise a stored timestamp to naive UTC so comparisons never mix types."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(254), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(80))
    password_hash: Mapped[str] = mapped_column(String(255))
    # Bumped on logout-all / password change so old JWTs stop working everywhere.
    token_version: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("1"), nullable=False)
    notify_email: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("1"))
    failed_logins: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    locked_until: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow,
                                                  server_default=text("CURRENT_TIMESTAMP"))

    bills = relationship("Bill", back_populates="user", cascade="all, delete-orphan")
    alerts = relationship("Alert", cascade="all, delete-orphan")
    profile = relationship("HouseholdProfile", uselist=False, cascade="all, delete-orphan")
    ai_insights = relationship("AIInsight", cascade="all, delete-orphan")


class RateLimitHit(Base):
    """Durable rate-limit event. Shared by every worker/process via the database."""
    __tablename__ = "rate_limit_hits"
    __table_args__ = (Index("ix_rate_limit_hits_bucket_time", "bucket", "occurred_at"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    bucket: Mapped[str] = mapped_column(String(64), nullable=False)  # keyed HMAC of the logical key
    occurred_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow,
                                                  server_default=text("CURRENT_TIMESTAMP"))


class RateLimitLock(Base):
    """One row per bucket, locked with SELECT ... FOR UPDATE to make counting atomic."""
    __tablename__ = "rate_limit_locks"
    bucket: Mapped[str] = mapped_column(String(64), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow,
                                                 server_default=text("CURRENT_TIMESTAMP"))


class Bill(Base):
    __tablename__ = "bills"
    __table_args__ = (
        Index("ix_bills_user_cat_due", "user_id", "category", "due_date"),
        UniqueConstraint("user_id", "file_hash", name="uq_bills_user_file"),
        CheckConstraint(f"category IN ({_CATEGORY_VALUES})", name="ck_bills_category"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    category: Mapped[str] = mapped_column(String(20), default="other", server_default=text("'other'"))
    provider: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    account_number: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    bill_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    due_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    amount: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), nullable=True)
    units: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # kWh / kL / GB etc.
    notes: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)

    paid: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("0"))
    paid_on: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    reviewed: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("0"))  # False = OCR draft
    confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    file_path: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    file_mime: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    file_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    raw_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    analysis: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)  # written by n8n / fallback
    feedback: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)  # user's explanation of an unusual bill
    reminded_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow,
                                                  server_default=text("CURRENT_TIMESTAMP"))

    user = relationship("User", back_populates="bills")
    insights = relationship("AIInsight", back_populates="bill", cascade="all, delete-orphan")

    @property
    def period(self) -> Optional[date]:
        return self.bill_date or self.due_date

    @property
    def state(self) -> str:
        if not self.reviewed:
            return "draft"
        if self.paid:
            return "paid"
        if self.due_date and self.due_date < date.today():
            return "overdue"
        return "unpaid"

    @property
    def has_file(self) -> bool:
        return bool(self.file_path)


class Alert(Base):
    __tablename__ = "alerts"
    __table_args__ = (
        Index("ix_alerts_bill_kind", "bill_id", "kind"),
        UniqueConstraint("user_id", "dedupe_key", name="uq_alerts_user_dedupe"),
        CheckConstraint("kind IN ('anomaly', 'reminder', 'info')", name="ck_alerts_kind"),
        CheckConstraint("severity IN ('info', 'warning', 'critical')", name="ck_alerts_severity"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    bill_id: Mapped[Optional[int]] = mapped_column(ForeignKey("bills.id", ondelete="SET NULL"), nullable=True)
    kind: Mapped[str] = mapped_column(String(20))  # anomaly | reminder | info
    severity: Mapped[str] = mapped_column(String(10), default="info", server_default=text("'info'"))
    message: Mapped[str] = mapped_column(String(500))
    # One alert per user, kind and bill/day. NULL = no de-duplication (generic info alerts).
    dedupe_key: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    is_read: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow,
                                                  server_default=text("CURRENT_TIMESTAMP"))


class AIInsight(Base):
    """Cached Ollama output. Kept apart from Bill.analysis, which n8n owns."""
    __tablename__ = "ai_insights"
    __table_args__ = (
        Index("ix_ai_user_kind_bill", "user_id", "kind", "bill_id"),
        CheckConstraint("kind IN ('extract', 'bill', 'portfolio')", name="ck_ai_kind"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    bill_id: Mapped[Optional[int]] = mapped_column(ForeignKey("bills.id", ondelete="CASCADE"), nullable=True)
    kind: Mapped[str] = mapped_column(String(20))  # extract | bill | portfolio
    model: Mapped[str] = mapped_column(String(80), default="", server_default=text("''"))
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow,
                                                  server_default=text("CURRENT_TIMESTAMP"))

    bill = relationship("Bill", back_populates="insights")


class HouseholdProfile(Base):
    """Answers to the 'tell us about your home' questions. Every field is optional; None = not answered yet."""
    __tablename__ = "household_profiles"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), unique=True, index=True)
    city: Mapped[Optional[str]] = mapped_column(String(60), nullable=True)
    climate_zone: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    area_sqft: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    occupants: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    ac_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    ac_hours_peak: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    fridges: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    electric_geyser: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    induction: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    washing_machine: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    water_pump: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    ev_km_per_day: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    solar_kw: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    wfh_days: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    tariff_per_unit: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    fixed_charge: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow,
                                                 server_default=text("CURRENT_TIMESTAMP"))
