import csv
import hashlib
import hmac
import io
import logging
import uuid
from datetime import date
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy import case
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import settings
from ..database import get_db
from ..deps import get_current_user
from ..models import Alert, Bill, User
from ..schemas import BillIn, BillList, BillOut, BulkImportOut, BulkPreviewOut, ExtractOut, FeedbackIn, PaymentIn
from ..services.notify import dispatch_analysis
from ..services.ocr import OCRUnavailable, extract_text
from ..services.parser import ParsedBill, parse_bill_text
from ..services import ai_cache, bulk, llm

router = APIRouter(prefix="/bills", tags=["bills"])
log = logging.getLogger("smartbill.bills")

MAGIC = [(b"%PDF", "pdf", "application/pdf"), (b"\x89PNG\r\n\x1a\n", "png", "image/png"), (b"\xff\xd8\xff", "jpg", "image/jpeg")]
UPLOAD_ROOT = Path(settings.upload_dir).resolve()


def sniff(data: bytes):
    """Trust file *content*, never the client-supplied name or Content-Type."""
    for sig, ext, mime in MAGIC:
        if data.startswith(sig):
            return ext, mime
    return None


def owned(db: Session, user: User, bill_id: int) -> Bill:
    bill = db.get(Bill, bill_id)
    if not bill or bill.user_id != user.id:  # 404 (not 403) so IDs can't be probed
        raise HTTPException(404, "Bill not found.")
    return bill


def apply(bill: Bill, data: BillIn) -> None:
    for k, v in data.model_dump().items():
        setattr(bill, k, v)


def _as_date(value):
    try:
        return date.fromisoformat(value) if value else None
    except (TypeError, ValueError):
        return None


def _merge_ai(parsed: ParsedBill, ai: dict) -> list[str]:
    """Fill fields the rule parser missed with the model's validated extraction.

    The rules stay in charge of anything they already found (label-anchored amounts are
    very reliable); the model is used to fill gaps and to flag disagreements.
    """
    warnings: list[str] = []
    for field in ("provider", "account_number", "amount", "units"):
        if getattr(parsed, field) is None and ai.get(field) is not None:
            setattr(parsed, field, ai[field])
    for field in ("bill_date", "due_date"):
        if getattr(parsed, field) is None and _as_date(ai.get(field)):
            setattr(parsed, field, _as_date(ai.get(field)))

    if parsed.category == "other" and ai.get("category"):
        parsed.category = ai["category"]
    if parsed.category == "electricity" and parsed.units is None and ai.get("units") is not None:
        parsed.units = ai["units"]

    if parsed.bill_date and parsed.due_date and parsed.due_date < parsed.bill_date:
        warnings.append("The due date looks earlier than the bill date — please check both.")

    ai_amount = ai.get("amount")
    if ai_amount is not None and parsed.amount is not None:
        ai_amount = float(ai_amount)
        parsed_amount = float(parsed.amount)
        if abs(ai_amount - parsed_amount) > max(1.0, 0.02 * parsed_amount):
            warnings.append(f"The AI read a different total (₹{ai_amount:,.2f}) — please check the amount.")

    parsed.confidence = round(
        0.40 * (parsed.amount is not None) + 0.30 * (parsed.due_date is not None)
        + 0.15 * (parsed.bill_date is not None) + 0.15 * (parsed.provider is not None), 2,
    )
    parsed.missing = [n for n, v in (("amount", parsed.amount), ("due_date", parsed.due_date),
                                     ("bill_date", parsed.bill_date), ("provider", parsed.provider)) if v is None]
    return warnings


@router.post("/extract", response_model=ExtractOut, status_code=201)
def extract(file: UploadFile = File(...), user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    limit = settings.max_upload_mb * 1024 * 1024
    data = file.file.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(413, f"File is larger than {settings.max_upload_mb} MB.")
    kind = sniff(data)
    if not kind:
        raise HTTPException(415, "Upload a PDF, JPG or PNG file.")
    ext, mime = kind

    digest = hashlib.sha256(data).hexdigest()
    dup = db.query(Bill.id).filter(Bill.user_id == user.id, Bill.file_hash == digest).first()
    if dup:
        raise HTTPException(409, f"You already uploaded this file (bill #{dup[0]}).")
    if db.query(Bill).filter(Bill.user_id == user.id, Bill.reviewed.is_(False)).count() >= settings.max_drafts_per_user:
        raise HTTPException(429, "Too many unreviewed uploads. Confirm or discard some first.")

    folder = UPLOAD_ROOT / str(user.id)
    folder.mkdir(parents=True, exist_ok=True)
    name = f"{uuid.uuid4().hex}.{ext}"  # random server-chosen name: no path traversal, no overwrites
    path = folder / name
    path.write_bytes(data)

    stored = False
    try:
        warnings, text = [], ""
        try:
            text = extract_text(str(path), ext)
        except OCRUnavailable as e:
            warnings.append(f"Automatic reading is unavailable ({e}). Please enter the details manually.")
        except Exception:
            log.exception("OCR failed")
            warnings.append("We couldn't read this file automatically. Please enter the details manually.")
        parsed = parse_bill_text(text)
        ai, ai_used = None, False
        if text.strip() and settings.ollama_extract and settings.ollama_enabled:
            try:
                ai = llm.extract_fields(text)
                warnings.extend(_merge_ai(parsed, ai))
                ai_used = True
            except llm.LLMError as e:
                log.info("Smart Bill AI extraction skipped: %s", e)
            except Exception:
                log.exception("Smart Bill AI extraction failed")
        if text and parsed.confidence < 0.6:
            warnings.append("Some details were hard to read — please double-check them.")

        bill = Bill(user_id=user.id, category=parsed.category, provider=parsed.provider, account_number=parsed.account_number,
                    bill_date=parsed.bill_date, due_date=parsed.due_date, amount=parsed.amount, units=parsed.units,
                    notes=(ai or {}).get("notes"), confidence=parsed.confidence, reviewed=False,
                    file_path=f"{user.id}/{name}", file_mime=mime, file_hash=digest, raw_text=text[:20000])
        db.add(bill)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            path.unlink(missing_ok=True)  # a concurrent upload won: do not leave an orphan file
            raise HTTPException(409, "You already uploaded this file.")
        stored = True
    except HTTPException:
        raise
    except Exception:
        db.rollback()
        if not stored:
            path.unlink(missing_ok=True)
        raise

    if ai:
        ai_cache.save(db, user.id, "extract", ai, bill_id=bill.id)  # keeps line items for the review screen
    return ExtractOut(bill=bill, confidence=parsed.confidence, missing=parsed.missing, warnings=warnings,
                      raw_text=text[:20000], ai_used=ai_used, ai=ai)


@router.post("", response_model=BillOut, status_code=201)
def create_manual(data: BillIn, bg: BackgroundTasks, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    bill = Bill(user_id=user.id, reviewed=True)
    apply(bill, data)
    db.add(bill)
    db.commit()
    ai_cache.invalidate_portfolio(db, user.id)
    bg.add_task(dispatch_analysis, bill.id)
    return bill


@router.get("", response_model=BillList)
def list_bills(category: Optional[str] = None, month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
               status: Optional[str] = Query(None, pattern="^(paid|unpaid|overdue|draft)$"),
               limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
               user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    q = db.query(Bill).filter(Bill.user_id == user.id)
    q = q.filter(Bill.reviewed.is_(status != "draft"))
    if category:
        q = q.filter(Bill.category == category)
    if month:
        y, m = map(int, month.split("-"))
        start, end = date(y, m, 1), date(y + (m == 12), m % 12 + 1, 1)
        q = q.filter(Bill.due_date >= start, Bill.due_date < end)
    if status == "paid":
        q = q.filter(Bill.paid.is_(True))
    elif status == "unpaid":
        q = q.filter(Bill.paid.is_(False), Bill.due_date >= date.today())
    elif status == "overdue":
        q = q.filter(Bill.paid.is_(False), Bill.due_date < date.today())
    total = q.count()
    items = (q.order_by(case((Bill.due_date.is_(None), 1), else_=0), Bill.due_date.desc(), Bill.id.desc())
             .offset(offset).limit(limit).all())
    return BillList(items=items, total=total)


def _safe_cell(v):
    s = "" if v is None else str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@") else s  # neutralise CSV/Excel formula injection


@router.get("/export.csv")
def export_csv(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id", "category", "provider", "account", "bill_date", "due_date", "amount", "units", "status", "paid_on"])
    for b in db.query(Bill).filter(Bill.user_id == user.id, Bill.reviewed.is_(True)).order_by(Bill.due_date):
        w.writerow([_safe_cell(x) for x in (b.id, b.category, b.provider, b.account_number, b.bill_date, b.due_date,
                                            b.amount, b.units, b.state, b.paid_on)])
    buf.seek(0)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=smartbill-export.csv"})


@router.get("/bulk-template")
def bulk_template(_: User = Depends(get_current_user)):
    """Excel template with the expected columns and an instructions sheet."""
    data = bulk.build_template()
    return StreamingResponse(iter([data]), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                             headers={"Content-Disposition": "attachment; filename=smartbill-bulk-template.xlsx"})


@router.post("/bulk-preview", response_model=BulkPreviewOut)
def bulk_preview(file: UploadFile = File(...), user: User = Depends(get_current_user),
                 db: Session = Depends(get_db)):
    """Validate an Excel/CSV file and return every row for review. Nothing is saved yet."""
    limit = settings.max_upload_mb * 1024 * 1024
    data = file.file.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(413, f"File is larger than {settings.max_upload_mb} MB.")
    try:
        rows = bulk.read_rows(data, file.filename or "")
        preview = bulk.preview_rows(db, user, rows)
    except ValueError as e:
        raise HTTPException(422, str(e))
    return BulkPreviewOut(
        total=len(preview),
        ready=sum(1 for r in preview if r["status"] == "ready"),
        duplicates=sum(1 for r in preview if r["status"] == "duplicate"),
        invalid=sum(1 for r in preview if r["status"] == "invalid"),
        token=bulk.preview_token(data, user.id),
        rows=preview,
    )


@router.post("/bulk-import", response_model=BulkImportOut, status_code=201)
def bulk_import(bg: BackgroundTasks, file: UploadFile = File(...),
                preview_token: str = Form(default=""),
                user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Create many bills from a previewed Excel/CSV file. Valid rows are saved; bad rows are reported."""
    limit = settings.max_upload_mb * 1024 * 1024
    data = file.file.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(413, f"File is larger than {settings.max_upload_mb} MB.")
    if not preview_token:
        raise HTTPException(409, "Preview this file before importing it.")
    try:
        valid = hmac.compare_digest(preview_token, bulk.preview_token(data, user.id))
    except TypeError:
        valid = False
    if not valid:
        raise HTTPException(409, "Preview this file before importing it.")
    try:
        rows = bulk.read_rows(data, file.filename or "")
        ids, skipped, errors = bulk.import_rows(db, user, rows)
    except ValueError as e:
        raise HTTPException(422, str(e))
    if ids:
        ai_cache.invalidate_portfolio(db, user.id)
        for bill_id in ids:
            bg.add_task(dispatch_analysis, bill_id)
    return BulkImportOut(total=len(rows), created=len(ids), skipped=skipped, errors=errors)


@router.get("/{bill_id}", response_model=BillOut)
def get_bill(bill_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return owned(db, user, bill_id)


@router.put("/{bill_id}", response_model=BillOut)
def update_bill(bill_id: int, data: BillIn, bg: BackgroundTasks, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    bill = owned(db, user, bill_id)
    apply(bill, data)
    db.commit()
    ai_cache.invalidate_for_bill(db, user.id, bill.id)
    if bill.reviewed:
        bg.add_task(dispatch_analysis, bill.id)
    return bill


@router.post("/{bill_id}/confirm", response_model=BillOut)
def confirm(bill_id: int, data: BillIn, bg: BackgroundTasks, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """User has checked/corrected the OCR result -> save for real and trigger the n8n analysis."""
    bill = owned(db, user, bill_id)
    apply(bill, data)
    bill.reviewed = True
    db.commit()
    ai_cache.invalidate_for_bill(db, user.id, bill.id)
    bg.add_task(dispatch_analysis, bill.id)
    return bill


@router.patch("/{bill_id}/payment", response_model=BillOut)
def set_payment(bill_id: int, data: PaymentIn, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    bill = owned(db, user, bill_id)
    bill.paid = data.paid
    bill.paid_on = (data.paid_on or date.today()) if data.paid else None
    db.commit()
    ai_cache.invalidate_for_bill(db, user.id, bill.id)  # payment status affects bill and portfolio answers
    return bill


@router.delete("/{bill_id}", status_code=204)
def delete_bill(bill_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    bill = owned(db, user, bill_id)
    file_path = bill.file_path
    db.delete(bill)
    db.commit()  # never remove the file while the database still points to it
    if file_path:
        (UPLOAD_ROOT / file_path).unlink(missing_ok=True)
    ai_cache.invalidate_for_bill(db, user.id, bill_id)


@router.get("/{bill_id}/file")
def get_file(bill_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    bill = owned(db, user, bill_id)
    if not bill.file_path:
        raise HTTPException(404, "No file attached.")
    path = (UPLOAD_ROOT / bill.file_path).resolve()
    if UPLOAD_ROOT not in path.parents or not path.is_file():  # belt-and-braces traversal check
        raise HTTPException(404, "File missing.")
    return FileResponse(path, media_type=bill.file_mime or "application/octet-stream")


FEEDBACK_KIND = {"guests": "temporary", "travel": "temporary", "meter_issue": "temporary",
                 "new_appliance": "permanent", "tariff_change": "permanent", "seasonal": "none", "other": "none"}
FEEDBACK_REPLY = {
    "temporary": "Thanks. We'll treat this month as a one-off and keep it out of your forecasts.",
    "permanent": "Thanks. We'll use this month as your new baseline. Update 'My home' so the profile model knows about the change.",
    "none": "Thanks, noted.",
}


@router.post("/{bill_id}/feedback")
def feedback(bill_id: int, data: FeedbackIn, bg: BackgroundTasks, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """The 'why was this bill high?' answer. It changes how future forecasts and anomaly checks treat this month."""
    bill = owned(db, user, bill_id)
    kind = FEEDBACK_KIND[data.reason]
    bill.feedback = {"reason": data.reason, "note": data.note, "kind": kind}
    for a in db.query(Alert).filter(Alert.bill_id == bill.id, Alert.kind == "anomaly"):
        a.is_read = True
    msg = FEEDBACK_REPLY[kind]
    if data.reason == "meter_issue":
        msg = "Thanks. This looks like a meter or billing issue — consider raising a dispute with the provider. We'll keep it out of your forecasts."
    db.commit()
    ai_cache.invalidate_for_bill(db, user.id, bill.id)
    return {"kind": kind, "message": msg}
