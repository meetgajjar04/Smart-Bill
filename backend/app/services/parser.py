"""Rule-based extraction of bill fields from OCR text (Indian utility-bill vocabulary)."""
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from dateutil import parser as dparse

MONTHS = r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
DATE_RX = (
    r"(\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4}"
    rf"|\d{{1,2}}[\s\-]*{MONTHS}[\s\-,.]*\d{{2,4}}"
    rf"|{MONTHS}\.?\s+\d{{1,2}},?\s+\d{{4}})"
)
MONEY = r"(?:₹|rs\.?|inr)?\s*([\d,]+(?:\.\d{1,2})?)"

AMOUNT_LABELS = [
    "total amount payable", "net amount payable", "amount payable", "total amount due", "amount due",
    "net payable", "total payable", "amount to be paid", "bill amount", "total bill", "grand total",
    "net amount", "total",
]
DUE_LABELS = ["due date", "last date of payment", "last date", "pay by", "payable by", "due on", "pay before"]
BILL_LABELS = ["bill date", "invoice date", "date of issue", "bill generated on", "statement date", "billing date", "date of bill"]

PROVIDERS = {
    "MSEDCL": "electricity", "Mahavitaran": "electricity", "Maharashtra State Electricity": "electricity",
    "MSEB": "electricity", "Adani Electricity": "electricity", "Tata Power": "electricity",
    "Brihanmumbai Electric": "electricity", "BEST Undertaking": "electricity",
    "BSES": "electricity", "Torrent Power": "electricity", "BESCOM": "electricity", "TNEB": "electricity",
    "Jio": "mobile", "Airtel": "mobile", "Vodafone": "mobile", "BSNL": "mobile",
    "ACT Fibernet": "internet", "Hathway": "internet", "Excitel": "internet", "JioFiber": "internet",
    "Airtel Xstream": "internet", "Tata Play Fiber": "internet",
    "MCGM": "water", "Jal Board": "water", "Mahanagar Gas": "gas", "Indraprastha Gas": "gas", "Adani Total Gas": "gas",
}
KEYWORDS = {
    "electricity": ["kwh", "units consumed", "meter", "electricity", "energy charge", "fixed charge", "consumer no"],
    "mobile": ["postpaid", "mobile number", "data usage", "calls", "sms", "recharge", "roaming"],
    "internet": ["broadband", "fibre", "fiber", "mbps", "router", "ftth", "wifi"],
    "water": ["water", "sewerage", "kilolitre", "kl", "jal"],
    "gas": ["png", "scm", "cylinder", "piped gas", "lpg"],
}


@dataclass
class ParsedBill:
    category: str = "other"
    provider: Optional[str] = None
    account_number: Optional[str] = None
    bill_date: Optional[date] = None
    due_date: Optional[date] = None
    amount: Optional[float] = None
    units: Optional[float] = None
    confidence: float = 0.0
    missing: list[str] = field(default_factory=list)


def _to_float(s: str) -> Optional[float]:
    try:
        return float(s.replace(",", ""))
    except ValueError:
        return None


def _label_rx(label: str) -> str:
    """Allow OCR line breaks/extra spaces between the words of a label."""
    return r"\s+".join(re.escape(part) for part in label.split())


def find_amount(text: str) -> Optional[float]:
    for label in AMOUNT_LABELS:
        for m in re.finditer(rf"{_label_rx(label)}[^\d₹]{{0,40}}{MONEY}", text, re.I):
            v = _to_float(m.group(1))
            if v and 1 <= v <= 10_000_000:
                return v
    return None


def find_date(text: str, labels: list[str]) -> Optional[date]:
    for label in labels:
        for m in re.finditer(rf"{_label_rx(label)}[^\d]{{0,25}}{DATE_RX}", text, re.I):
            try:
                d = dparse.parse(m.group(1), dayfirst=True).date()
            except (ValueError, OverflowError):
                continue
            if 2000 <= d.year <= 2100:
                return d
    return None


def find_provider(text: str) -> tuple[Optional[str], Optional[str]]:
    low = text.lower()
    best = None
    for name, cat in PROVIDERS.items():
        if re.search(rf"\b{re.escape(name.lower())}\b", low):
            if best is None or len(name) > len(best[0]):  # prefer "Airtel Xstream" over "Airtel"
                best = (name, cat)
    return best if best else (None, None)


def guess_category(text: str, provider_cat: Optional[str]) -> str:
    if provider_cat:
        return provider_cat
    low = text.lower()
    scores = {c: sum(low.count(k) for k in ks) for c, ks in KEYWORDS.items()}
    cat, score = max(scores.items(), key=lambda kv: kv[1])
    return cat if score > 0 else "other"


def find_account(text: str) -> Optional[str]:
    m = re.search(r"(?:consumer|account|customer|connection|service)\s*(?:no\.?|number|id)[\s:\-#]*([A-Z0-9][A-Z0-9\-/]{4,19})", text, re.I)
    if m:
        return m.group(1)
    m = re.search(r"(?:mobile|phone)\s*(?:no\.?|number)?[\s:\-]*(?:\+?91[\s-]*)?([6-9]\d{9})", text, re.I)
    return m.group(1) if m else None


def find_units(text: str) -> Optional[float]:
    """Read consumption, staying on one line so an account number is never read as units."""
    m = re.search(r"units\s*consumed[^\d\n]{0,15}([\d,]+(?:\.\d+)?)", text, re.I) or re.search(
        r"([\d,]+(?:\.\d+)?)[ \t]*(?:kwh|kwhr|units)\b", text, re.I
    )
    value = _to_float(m.group(1)) if m else None
    # Meter readings and account numbers are not monthly consumption.
    if value is None or value <= 0 or value > 1_000_000:
        return None
    return value


def parse_bill_text(text: str) -> ParsedBill:
    p = ParsedBill()
    p.provider, pcat = find_provider(text)
    p.category = guess_category(text, pcat)
    p.amount = find_amount(text)
    p.due_date = find_date(text, DUE_LABELS)
    p.bill_date = find_date(text, BILL_LABELS)
    p.account_number = find_account(text)
    p.units = find_units(text) if p.category == "electricity" else None

    p.confidence = round(
        0.40 * (p.amount is not None) + 0.30 * (p.due_date is not None)
        + 0.15 * (p.bill_date is not None) + 0.15 * (p.provider is not None), 2,
    )
    p.missing = [n for n, v in (("amount", p.amount), ("due_date", p.due_date), ("bill_date", p.bill_date), ("provider", p.provider)) if v is None]
    return p
