"""Excel/CSV bulk import for bills.

The spreadsheet is deliberately simple: one row per bill, with the same fields as the
manual form. Invalid rows are reported individually; everything valid is still saved.
"""
import csv
import hashlib
import hmac
import io
import re
from datetime import date, datetime
from decimal import Decimal

from dateutil import parser as dparse
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils.datetime import from_excel
from openpyxl.worksheet.datavalidation import DataValidation
from pydantic import ValidationError
from sqlalchemy.orm import Session

from ..config import settings
from ..models import CATEGORIES, Bill, User
from ..schemas import BillIn, BulkRowError

HEADERS = ["category", "provider", "account_number", "bill_date", "due_date", "amount", "units", "notes"]
MAX_ROWS = 1000

ALIASES = {
    "type": "category", "bill_type": "category", "utility": "category",
    "provider_name": "provider", "company": "provider", "vendor": "provider",
    "account": "account_number", "account_no": "account_number", "consumer_no": "account_number",
    "bill_date": "bill_date", "invoice_date": "bill_date", "date": "bill_date",
    "due_date": "due_date", "pay_by": "due_date", "due": "due_date",
    "amount": "amount", "total": "amount", "total_amount": "amount", "amount_payable": "amount",
    "units": "units", "consumption": "units", "unit": "units", "kwh": "units",
    "notes": "notes", "note": "notes", "remarks": "notes",
}

CATEGORY_WORDS = {
    "electric": "electricity", "power": "electricity", "electricity_bill": "electricity",
    "phone": "mobile", "mobile_phone": "mobile", "cell": "mobile",
    "broadband": "internet", "wifi": "internet", "isp": "internet",
    "lpg": "gas", "piped_gas": "gas",
    "water_bill": "water",
}


def _norm_header(value) -> str:
    text = re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")
    return ALIASES.get(text, text)


def _norm_category(value) -> str:
    text = re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")
    text = CATEGORY_WORDS.get(text, text)
    if text not in CATEGORIES:
        raise ValueError(f"category must be one of: {', '.join(CATEGORIES)}")
    return text


def _clean_number(value, *, money: bool) -> float | None:
    if value is None or isinstance(value, bool) or str(value).strip() == "":
        return None
    if isinstance(value, (int, float, Decimal)):
        return float(value)
    text = str(value).strip().replace(",", "").replace("₹", "")
    text = re.sub(r"(?i)^(rs\.?|inr)\s*", "", text)
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        raise ValueError("amount/units must be a number")
    return float(match.group(0))


def _clean_date(value) -> date | None:
    if value is None or str(value).strip() == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)):
        try:
            return from_excel(value).date()
        except Exception:
            pass
    text = str(value).strip()
    parsed = None
    try:
        parsed = dparse.isoparse(text)
    except (ValueError, OverflowError):
        try:
            parsed = dparse.parse(text, dayfirst=True, fuzzy=True)
        except (ValueError, OverflowError):
            pass
    if parsed is None:
        raise ValueError(f"could not read date '{text}' (use YYYY-MM-DD or DD/MM/YYYY)")
    if not (2000 <= parsed.year <= 2100):
        raise ValueError("date year looks wrong")
    return parsed.date()


def _clean_text(value, limit: int) -> str | None:
    text = " ".join(str(value or "").split()).strip()
    return text[:limit] or None


def parse_row(raw: dict) -> BillIn:
    return BillIn(
        category=_norm_category(raw.get("category")),
        provider=_clean_text(raw.get("provider"), 80),
        account_number=_clean_text(raw.get("account_number"), 40),
        bill_date=_clean_date(raw.get("bill_date")),
        due_date=_clean_date(raw.get("due_date")),
        amount=_clean_number(raw.get("amount"), money=True),
        units=_clean_number(raw.get("units"), money=False),
        notes=_clean_text(raw.get("notes"), 300),
    )


def _error_message(error: Exception) -> str:
    if isinstance(error, ValidationError):
        first = error.errors()[0]
        field = ".".join(str(p) for p in first.get("loc", []) if p != "body")
        msg = str(first.get("msg", "invalid value")).replace("Value error, ", "")
        return f"{field}: {msg}" if field else msg
    return str(error)


def build_template() -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Bills"
    header_fill = PatternFill("solid", fgColor="0E7C66")
    for col, name in enumerate(HEADERS, start=1):
        cell = ws.cell(row=1, column=col, value=name)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = header_fill
        cell.alignment = Alignment(vertical="center")
        ws.column_dimensions[cell.column_letter].width = {
            "category": 16, "provider": 24, "account_number": 20, "bill_date": 14,
            "due_date": 14, "amount": 14, "units": 12, "notes": 36,
        }.get(name, 18)
    ws.freeze_panes = "A2"
    ws.row_dimensions[1].height = 24

    validation = DataValidation(type="list", formula1='"' + ",".join(CATEGORIES) + '"', allow_blank=False)
    validation.error = "Choose one of: " + ", ".join(CATEGORIES)
    validation.errorTitle = "Unknown category"
    ws.add_data_validation(validation)
    validation.add("A2:A1001")

    guide = wb.create_sheet("Instructions")
    guide.column_dimensions["A"].width = 26
    guide.column_dimensions["B"].width = 90
    rows = [
        ("Column", "What to enter"),
        ("category", "Required: electricity, mobile, internet, water, gas or other."),
        ("provider", "Company name, e.g. Adani Electricity (optional)."),
        ("account_number", "Consumer / account / mobile number (optional)."),
        ("bill_date", "Bill date: YYYY-MM-DD or DD/MM/YYYY (optional)."),
        ("due_date", "Required: payment due date, YYYY-MM-DD or DD/MM/YYYY."),
        ("amount", "Required: total amount in rupees, e.g. 2584.50. ₹ and commas are accepted."),
        ("units", "Consumption in kWh / kL / GB (optional)."),
        ("notes", "Anything to remember (optional, max 300 characters)."),
        ("", ""),
        ("Upload", "Save this file, fill the Bills sheet, then upload it on the Add a bill page."),
        ("Preview", "Every row is shown before anything is saved. Press Confirm to import the valid rows."),
        ("Import rules", "Rows with a problem are skipped and shown in the result; valid rows are saved."),
        ("Duplicates", "A bill with the same provider, due date and amount is skipped automatically."),
    ]
    for r, (a, b) in enumerate(rows, start=1):
        guide.cell(row=r, column=1, value=a).font = Font(bold=(r == 1))
        guide.cell(row=r, column=2, value=b).alignment = Alignment(wrap_text=True, vertical="top")
    return _save(wb)


def _save(wb) -> bytes:
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def read_rows(data: bytes, filename: str) -> list[tuple[int, dict]]:
    """Return [(row_number, values)] from an .xlsx/.xlsm/.csv file."""
    name = (filename or "").lower()
    if name.endswith(".csv"):
        text = data.decode("utf-8-sig", errors="replace")
        reader = csv.reader(io.StringIO(text))
        all_rows = list(reader)
        if not all_rows:
            raise ValueError("The CSV file is empty.")
        header = [_norm_header(h) for h in all_rows[0]]
        _check_header(header)
        out = []
        for number, values in enumerate(all_rows[1:], start=2):
            if not any(str(v).strip() for v in values):
                continue
            raw = {header[i]: values[i] for i in range(min(len(header), len(values)))}
            out.append((number, raw))
        return out

    if not (name.endswith(".xlsx") or name.endswith(".xlsm")):
        raise ValueError("Upload an Excel file (.xlsx) or a CSV file.")
    try:
        wb = load_workbook(io.BytesIO(data), data_only=True, read_only=True)
    except Exception as e:
        raise ValueError(f"Could not open the spreadsheet: {e}") from e
    ws = wb.active
    rows = ws.iter_rows(values_only=True)
    try:
        first = next(rows)
    except StopIteration:
        raise ValueError("The spreadsheet is empty.")
    header = [_norm_header(h) for h in first]
    _check_header(header)
    out = []
    for number, values in enumerate(rows, start=2):
        if not any(str(v).strip() for v in values if v is not None):
            continue
        raw = {header[i]: values[i] for i in range(min(len(header), len(values)))}
        out.append((number, raw))
    return out


def _check_header(header: list[str]) -> None:
    missing = [h for h in ("category", "due_date", "amount") if h not in header]
    if missing:
        raise ValueError("Missing required column(s): " + ", ".join(missing)
                         + ". Download the template and keep the header row unchanged.")


def preview_token(data: bytes, user_id: int) -> str:
    """Proof that this exact file was previewed by this user.

    Stateless: the import endpoint recomputes it, so a file cannot be imported
    without first going through the preview step.
    """
    digest = hashlib.sha256(data).digest()
    return hmac.new(settings.secret_key.encode(), f"{user_id}:".encode() + digest,
                    hashlib.sha256).hexdigest()


def existing_keys(db: Session, user: User) -> set[tuple]:
    """Bills already saved for this user, used to flag duplicates."""
    return {(b.category, b.due_date, float(b.amount), (b.provider or "").lower())
            for b in db.query(Bill).filter(Bill.user_id == user.id, Bill.reviewed.is_(True),
                                           Bill.amount.isnot(None))}


def _display(value) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return " ".join(str(value).split()).strip()


def preview_rows(db: Session, user: User, rows: list[tuple[int, dict]]) -> list[dict]:
    """Validate every row without writing anything. One entry per spreadsheet row."""
    if len(rows) > MAX_ROWS:
        raise ValueError(f"Too many rows ({len(rows)}). Split the file into batches of {MAX_ROWS} or fewer.")
    if not rows:
        raise ValueError("There are no bill rows to import.")

    existing = existing_keys(db, user)
    out: list[dict] = []
    for number, raw in rows:
        entry = {
            "row": number,
            "category": _display(raw.get("category")),
            "provider": _display(raw.get("provider")),
            "account_number": _display(raw.get("account_number")),
            "bill_date": _display(raw.get("bill_date")),
            "due_date": _display(raw.get("due_date")),
            "amount": _display(raw.get("amount")),
            "units": _display(raw.get("units")),
            "notes": _display(raw.get("notes")),
            "status": "ready",
            "message": "",
        }
        try:
            data = parse_row(raw)
        except Exception as e:
            entry["status"] = "invalid"
            entry["message"] = _error_message(e)
            out.append(entry)
            continue

        entry.update({
            "category": data.category,
            "provider": data.provider or "",
            "account_number": data.account_number or "",
            "bill_date": data.bill_date.isoformat() if data.bill_date else "",
            "due_date": data.due_date.isoformat() if data.due_date else "",
            "amount": f"{float(data.amount):.2f}",
            "units": "" if data.units is None else f"{float(data.units):g}",
            "notes": data.notes or "",
        })
        key = (data.category, data.due_date, float(data.amount), (data.provider or "").lower())
        if key in existing:
            entry["status"] = "duplicate"
            entry["message"] = "Already saved, or the same bill appears twice in this file."
        else:
            existing.add(key)  # catch repeats inside this file too
        out.append(entry)
    return out


def import_rows(db: Session, user: User, rows: list[tuple[int, dict]]) -> tuple[list[int], int, list[BulkRowError]]:
    """Validate and insert rows. Returns (created_bill_ids, skipped_duplicates, row_errors)."""
    if len(rows) > MAX_ROWS:
        raise ValueError(f"Too many rows ({len(rows)}). Split the file into batches of {MAX_ROWS} or fewer.")
    if not rows:
        raise ValueError("There are no bill rows to import.")

    existing = existing_keys(db, user)
    created: list[int] = []
    skipped = 0
    errors: list[BulkRowError] = []
    for number, raw in rows:
        try:
            data = parse_row(raw)
        except Exception as e:
            errors.append(BulkRowError(row=number, message=_error_message(e)))
            continue
        key = (data.category, data.due_date, float(data.amount), (data.provider or "").lower())
        if key in existing:
            skipped += 1
            continue
        bill = Bill(user_id=user.id, reviewed=True)
        for field, value in data.model_dump().items():
            setattr(bill, field, value)
        db.add(bill)
        existing.add(key)
        created.append(bill)

    try:
        db.commit()
    except Exception:
        db.rollback()
        raise
    return [b.id for b in created], skipped, errors
