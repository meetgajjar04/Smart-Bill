"""Local OCR: Tesseract + OpenCV, tuned for phone-camera photos of bills.

Phone photos are the hard case: they are rotated, skewed, unevenly lit, blurry and
noisy. The pipeline therefore runs several independent views of the same page and
keeps the most convincing reading instead of trusting one fixed preprocessing:

  1. read (EXIF orientation respected) + size guard
  2. orientation detection (Tesseract OSD on a contrast-enhanced copy), with a
     confidence probe of 0/90/180/270 when the detector is unsure
  3. deskew via projection-profile search
  4. resize so glyphs are roughly the size Tesseract likes (~30 px)
  5. views: background-flattened (removes shadows/lamp hotspots), Otsu of that,
     CLAHE + denoise + unsharp, and inverted for dark-mode screenshots
  6. OCR each view with the LSTM engine (--oem 1) in block/column/auto layout
     modes, scored by confidence-weighted character count + bill vocabulary
  7. if still unreadable, detect the sheet of paper and correct perspective
  8. normalise the winning text (unicode, dashes, whitespace)

Everything is local; no paid API is involved.
"""
import logging
import os
import re
import time
import unicodedata
from dataclasses import dataclass
from typing import Iterable, Optional

import cv2
import numpy as np

from ..config import settings

log = logging.getLogger("smartbill.ocr")

MAX_PIXELS = 8_000           # longest side we keep in memory (48 MP phone photos fit)
MAX_PDF_PAGES = 4
MIN_OCR_SIDE = 1800          # upscale small/screenshot images so glyphs are ~30px tall
MAX_OCR_SIDE = 2800          # downscale huge camera photos (keeps Tesseract fast)
CONFIDENT = 60.0             # mean word confidence at which we stop trying views
WEAK_CONF = 55.0             # below this we try more layouts / perspective correction
TESS_TIMEOUT = 15            # seconds per pass: a struggling pass must not stall upload
PAGE_BUDGET = 45.0           # soft limit for all OCR attempts on one page

# Words that appear on almost every Indian utility bill. A tiny bonus steers the
# choice between two noisy readings towards the one that actually looks like a bill.
BILL_WORDS = (
    "total", "amount", "payable", "due", "date", "bill", "invoice", "consumer", "account",
    "customer", "units", "kwh", "meter", "electricity", "mobile", "broadband", "water", "gas",
    "rs", "inr", "number", "reading", "charges", "payment",
)


class OCRUnavailable(RuntimeError):
    pass


@dataclass
class _Candidate:
    text: str
    score: float = 0.0
    words: int = 0
    mean_conf: float = 0.0
    label: str = ""

    @property
    def rank(self) -> float:
        """Confidence-weighted text volume: favours long, confident readings."""
        return self.score * (0.55 + 0.45 * min(self.mean_conf, 100.0) / 100.0)


def _tesseract():
    try:
        import pytesseract
    except ImportError as e:  # pragma: no cover
        raise OCRUnavailable("pytesseract is not installed") from e
    if settings.tesseract_cmd:
        pytesseract.pytesseract.tesseract_cmd = settings.tesseract_cmd
    if settings.tessdata_prefix:
        # Portable Tesseract builds (e.g. conda-forge) keep *.traineddata outside
        # the binary folder; without this Tesseract reports zero languages.
        os.environ.setdefault("TESSDATA_PREFIX", settings.tessdata_prefix)
    return pytesseract


def _lang() -> str:
    return getattr(settings, "ocr_lang", "eng") or "eng"


# --------------------------------------------------------------------------- images
def _gray(img: np.ndarray) -> np.ndarray:
    if img.ndim == 2:
        return img
    if img.shape[2] == 4:
        return cv2.cvtColor(img, cv2.COLOR_BGRA2GRAY)
    return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)


def _resize(gray: np.ndarray) -> np.ndarray:
    longest = max(gray.shape[:2])
    if longest < MIN_OCR_SIDE:
        scale, interp = MIN_OCR_SIDE / longest, cv2.INTER_CUBIC
    elif longest > MAX_OCR_SIDE:
        scale, interp = MAX_OCR_SIDE / longest, cv2.INTER_AREA
    else:
        return gray
    scale = min(scale, 4.0)  # never explode a thumbnail
    return cv2.resize(gray, None, fx=scale, fy=scale, interpolation=interp)


def _rotate(gray: np.ndarray, angle: float, border: int = 255) -> np.ndarray:
    if abs(angle) < 0.05:
        return gray
    h, w = gray.shape[:2]
    m = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
    cos, sin = abs(m[0, 0]), abs(m[0, 1])
    nw, nh = int(h * sin + w * cos), int(h * cos + w * sin)
    m[0, 2] += nw / 2 - w / 2
    m[1, 2] += nh / 2 - h / 2
    return cv2.warpAffine(gray, m, (nw, nh), flags=cv2.INTER_CUBIC,
                          borderMode=cv2.BORDER_CONSTANT, borderValue=border)


def _rotate_quarter(img: np.ndarray, turns: int) -> np.ndarray:
    turns %= 4
    if turns == 1:
        return cv2.rotate(img, cv2.ROTATE_90_CLOCKWISE)
    if turns == 2:
        return cv2.rotate(img, cv2.ROTATE_180)
    if turns == 3:
        return cv2.rotate(img, cv2.ROTATE_90_COUNTERCLOCKWISE)
    return img


def _memory_guard(img: np.ndarray) -> np.ndarray:
    h, w = img.shape[:2]
    if max(h, w) <= MAX_PIXELS:
        return img
    scale = MAX_PIXELS / max(h, w)
    return cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)


def _enhance(gray: np.ndarray) -> np.ndarray:
    """Even out lighting, suppress sensor noise, sharpen glyph edges."""
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    out = clahe.apply(gray)
    out = cv2.bilateralFilter(out, 5, 35, 35)
    blurred = cv2.GaussianBlur(out, (0, 0), 1.2)
    return cv2.addWeighted(out, 1.55, blurred, -0.55, 0)


def _flatten(gray: np.ndarray) -> np.ndarray:
    """Divide by a blurred copy to remove shadows and lamp hotspots.

    This is much gentler than a small-block adaptive threshold: it does not turn paper
    texture or JPEG noise into thousands of blobs that make Tesseract crawl.
    """
    sigma = max(9.0, min(gray.shape[:2]) / 20.0)
    background = cv2.GaussianBlur(gray, (0, 0), sigma)
    flat = cv2.divide(gray, background, scale=255)
    return cv2.normalize(flat, None, 0, 255, cv2.NORM_MINMAX)


def _osd_rotation(pt, img: np.ndarray) -> Optional[int]:
    """Ask Tesseract which quarter-turn makes the page upright, but only trust it
    when its own orientation confidence is meaningful (dark/blurred photos mislead)."""
    small = _memory_guard(img)
    if max(small.shape[:2]) > 1600:
        s = 1600 / max(small.shape[:2])
        small = cv2.resize(small, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
    probe = cv2.cvtColor(_enhance(_gray(small)), cv2.COLOR_GRAY2BGR)
    try:
        osd = pt.image_to_osd(probe, config="--psm 0")
    except Exception:  # too little text / very dark image
        return None
    m = re.search(r"Rotate:\s*(\d+)", osd)
    c = re.search(r"Orientation confidence:\s*([\d.]+)", osd)
    if not m:
        return None
    confidence = float(c.group(1)) if c else 0.0
    return (int(m.group(1)) // 90) % 4 if confidence >= 0.5 else None


def _skew_angle(gray: np.ndarray) -> float:
    """Projection-profile search: the angle whose horizontal profile is sharpest."""
    h, w = gray.shape[:2]
    scale = min(1.0, 900 / max(h, w))
    small = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA) if scale < 1 else gray
    inv = cv2.threshold(small, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)[1]
    inv = cv2.morphologyEx(inv, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    if cv2.countNonZero(inv) < 500:
        return 0.0
    best_angle, best_score = 0.0, -1.0
    for angle in np.arange(-8.0, 8.01, 0.5):
        m = cv2.getRotationMatrix2D((inv.shape[1] / 2, inv.shape[0] / 2), angle, 1.0)
        rot = cv2.warpAffine(inv, m, (inv.shape[1], inv.shape[0]),
                             flags=cv2.INTER_NEAREST, borderValue=0)
        profile = rot.sum(axis=1, dtype=np.float64)
        score = float(np.var(np.diff(profile)))
        if score > best_score:
            best_angle, best_score = float(angle), score
    return best_angle if abs(best_angle) >= 0.4 else 0.0


def _prepare(img: np.ndarray) -> np.ndarray:
    gray = _resize(_gray(img))
    angle = _skew_angle(gray)
    if angle:
        gray = _rotate(gray, angle)
        log.debug("deskewed by %.1f deg", angle)
    return gray


def _sane(binary: np.ndarray) -> bool:
    """Reject threshold results that are almost entirely ink or almost entirely paper."""
    black = 1.0 - cv2.countNonZero(binary) / binary.size
    return 0.005 < black < 0.65


def _variants(gray: np.ndarray) -> Iterable[tuple[str, np.ndarray]]:
    flat = _flatten(gray)
    yield "flattened", flat                 # shadows / lamp hotspots
    _, flat_otsu = cv2.threshold(flat, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
    if _sane(flat_otsu):
        yield "flattened-otsu", flat_otsu
    yield "enhanced", _enhance(gray)        # clean scan / screenshot
    if float(np.mean(gray)) < 125:          # dark-mode screenshot / night photo
        # inverted *and* flattened, so it is black text on white again
        yield "inverted", cv2.bitwise_not(_flatten(gray))


# ----------------------------------------------------------------------------- OCR
_TESS_BASE = "--oem 1 --dpi 300 -c preserve_interword_spaces=1"


def _run(pt, image: np.ndarray, psm: int, lang: str, label: str) -> _Candidate:
    config = f"{_TESS_BASE} --psm {psm}"
    try:
        data = pt.image_to_data(image, lang=lang, config=config,
                                output_type=pt.Output.DICT, timeout=TESS_TIMEOUT)
    except pt.TesseractNotFoundError as e:
        raise OCRUnavailable("Tesseract is not installed on the server") from e
    except RuntimeError as e:  # pytesseract timeout
        log.warning("OCR timed out on %s: %s", label, e)
        return _Candidate(text="", label=label)
    except pt.TesseractError as e:
        if lang != "eng":
            log.warning("OCR language %r failed (%s); retrying with eng", lang, e)
            return _run(pt, image, psm, "eng", label)
        raise OCRUnavailable(f"Tesseract failed: {e}") from e

    lines: dict[tuple[int, int, int], list[str]] = {}
    confs: list[float] = []
    weighted = 0.0
    words = 0
    for i, raw in enumerate(data.get("text", [])):
        token = (raw or "").strip()
        if not token:
            continue
        conf = float(data["conf"][i]) if data.get("conf") else -1.0
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        lines.setdefault(key, []).append(token)
        if conf > 0:
            confs.append(conf)
            weighted += conf * len(token) / 100.0
        if any(c.isalnum() for c in token):
            words += 1

    text = "\n".join(" ".join(parts) for _, parts in sorted(lines.items()))
    text = _clean_text(text)
    hits = sum(1 for w in BILL_WORDS if w in text.lower())
    return _Candidate(text=text, score=weighted + 4.0 * hits, words=words,
                      mean_conf=round(float(np.mean(confs)) if confs else 0.0, 1), label=label)


_DASHES = "\u2010\u2011\u2012\u2013\u2014\u2015\u2212"
_INVISIBLE = "\u200b\u200c\u200d\ufeff"


def _clean_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    for ch in _INVISIBLE:
        text = text.replace(ch, "")
    text = text.replace("\u00a0", " ")
    for dash in _DASHES:
        text = text.replace(dash, "-")
    text = (text.replace("\u2018", "'").replace("\u2019", "'")
                .replace("\u201c", '"').replace("\u201d", '"'))
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return "\n".join(line.strip() for line in text.splitlines()).strip()


def _evaluate(pt, variants, psms, lang: str, deadline: Optional[float] = None) -> tuple[_Candidate, np.ndarray]:
    """Run views at the given layout modes; keep the best candidate + image.

    With a single layout mode we stop as soon as a view reads the page confidently,
    which keeps the common (clean photo) case to one or two Tesseract passes.
    """
    best, best_img = _Candidate(text="", label="none"), variants[0][1]
    for label, variant in variants:
        for psm in psms:
            cand = _run(pt, variant, psm, lang, f"{label}/psm{psm}")
            log.debug("OCR %s -> score %.0f, words %d, conf %.1f",
                      cand.label, cand.score, cand.words, cand.mean_conf)
            if cand.rank > best.rank:
                best, best_img = cand, variant
        if len(psms) == 1 and best.mean_conf >= CONFIDENT and best.words >= 15:
            break
        if deadline and time.monotonic() > deadline:
            log.info("OCR page budget exhausted; stopping after %s", best.label)
            break
    return best, best_img


def _probe_orientation(pt, img: np.ndarray, lang: str) -> int:
    """Cheap low-resolution OCR at 0/90/180/270; the upright view reads best."""
    h, w = img.shape[:2]
    scale = min(1.0, 1400 / max(h, w))
    small = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA) if scale < 1 else img

    def value(turns: int) -> float:
        cand = _run(pt, _flatten(_prepare(_rotate_quarter(small, turns))), 6, lang,
                    f"probe-{turns * 90}")
        return cand.rank

    scores = {0: value(0), 2: value(2)}
    best = max(scores, key=scores.get)
    if scores[best] < 250:  # maybe the page is sideways, not merely upside down
        scores[1] = value(1)
        scores[3] = value(3)
        best = max(scores, key=scores.get)
    log.info("orientation probe %s -> %d deg", {k: round(v) for k, v in scores.items()}, best * 90)
    return best


def _read_page(pt, img: np.ndarray, lang: str) -> _Candidate:
    """Best reading of an upright page: views, then layout modes, then page warp."""
    deadline = time.monotonic() + PAGE_BUDGET
    variants = _prepared_variants(img, "page")
    best, best_img = _evaluate(pt, variants, (6,), lang, deadline)  # uniform block: bills

    if time.monotonic() < deadline and (best.mean_conf < 70 or best.words < 10):
        cand, cand_img = _evaluate(pt, [(best.label, best_img)], (4,), lang, deadline)
        if cand.rank > best.rank:
            best, best_img = cand, cand_img
    if time.monotonic() < deadline and best.mean_conf < WEAK_CONF:   # automatic layout
        cand, cand_img = _evaluate(pt, [(best.label, best_img)], (3,), lang, deadline)
        if cand.rank > best.rank:
            best, best_img = cand, cand_img

    if time.monotonic() < deadline and (best.mean_conf < 45 or best.words < 3):
        warped = _page_warp(img)                                # last resort: crop the paper
        if warped is not None:
            cand, _ = _evaluate(pt, _prepared_variants(warped, "warp"), (6, 4), lang, deadline)
            if cand.rank > best.rank:
                best = cand
                log.info("used perspective-corrected page")
    return best


def _ocr_array(img: np.ndarray) -> str:
    pt = _tesseract()
    lang = _lang()
    img = _memory_guard(img)

    # Tesseract's OSD is accurate when it is confident; when it is not we first try
    # the page as-is and only run the (more expensive) quarter-turn probe if the
    # reading is weak. That keeps normal upright photos to a single pass.
    turns = _osd_rotation(pt, img)
    if turns:
        img = _rotate_quarter(img, turns)
    best = _read_page(pt, img, lang)

    if turns is None and (best.mean_conf < 70 or best.words < 5):
        guess = _probe_orientation(pt, img, lang)
        if guess:
            cand = _read_page(pt, _rotate_quarter(img, guess), lang)
            if cand.rank > best.rank:
                best = cand

    if not best.text:
        log.warning("OCR found no text (best score %.0f)", best.score)
    else:
        log.info("OCR picked %s: %d words, mean confidence %.1f, score %.0f",
                 best.label, best.words, best.mean_conf, best.score)
    return best.text


def _page_warp(img: np.ndarray) -> Optional[np.ndarray]:
    """Detect the sheet of paper and correct perspective. Returns None if unsure."""
    color = img if img.ndim == 3 else cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
    gray = _gray(img)
    h, w = gray.shape[:2]
    scale = min(1.0, 1000 / max(h, w))
    small = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA) if scale < 1 else gray
    edges = cv2.Canny(cv2.GaussianBlur(small, (5, 5), 0), 40, 120)
    edges = cv2.dilate(edges, np.ones((5, 5), np.uint8), iterations=2)
    contours = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)[0]
    if not contours:
        return None
    cnt = max(contours, key=cv2.contourArea)
    if cv2.contourArea(cnt) < 0.25 * small.shape[0] * small.shape[1]:
        return None
    approx = cv2.approxPolyDP(cnt, 0.02 * cv2.arcLength(cnt, True), True)
    if len(approx) != 4 or not cv2.isContourConvex(approx):
        return None
    pts = approx.reshape(4, 2).astype(np.float32) / scale
    s = pts.sum(axis=1)
    d = np.diff(pts, axis=1).ravel()
    tl, br = pts[np.argmin(s)], pts[np.argmax(s)]
    tr, bl = pts[np.argmin(d)], pts[np.argmax(d)]
    width = int(max(np.linalg.norm(tr - tl), np.linalg.norm(br - bl)))
    height = int(max(np.linalg.norm(bl - tl), np.linalg.norm(br - tr)))
    if min(width, height) < 300 or not (0.2 <= width / height <= 5.0):
        return None
    dst = np.array([[0, 0], [width - 1, 0], [width - 1, height - 1], [0, height - 1]], np.float32)
    return cv2.warpPerspective(color, cv2.getPerspectiveTransform(
        np.array([tl, tr, br, bl], np.float32), dst), (width, height))


def _prepared_variants(img: np.ndarray, tag: str) -> list[tuple[str, np.ndarray]]:
    return [(f"{tag}/{label}", variant) for label, variant in _variants(_prepare(img))]


def preprocess(img: np.ndarray) -> np.ndarray:
    """Backwards-compatible helper: the background-flattened view."""
    return next(iter(_variants(_prepare(img))))[1]


def _read_image(path: str) -> np.ndarray:
    img = cv2.imread(path, cv2.IMREAD_COLOR)
    if img is None:  # non-ASCII path on some builds; retry from bytes
        try:
            img = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)
        except Exception:
            img = None
    if img is None:
        raise ValueError("Could not decode image")
    return _memory_guard(img)


def extract_text(path: str, kind: str) -> str:
    if kind != "pdf":
        return _ocr_array(_read_image(path))

    import pdfplumber

    text = ""
    try:
        with pdfplumber.open(path) as pdf:
            text = "\n".join((p.extract_text() or "") for p in pdf.pages[:MAX_PDF_PAGES])
    except Exception:
        log.exception("pdfplumber failed")
    if len(text.strip()) >= 40:  # born-digital PDF: use the real text layer
        return _clean_text(text)

    from pdf2image import convert_from_path  # scanned PDF -> rasterise -> OCR

    pages = convert_from_path(path, dpi=300, first_page=1, last_page=MAX_PDF_PAGES,
                             poppler_path=settings.poppler_path or None)
    return "\n\n".join(_ocr_array(cv2.cvtColor(np.array(p), cv2.COLOR_RGB2BGR)) for p in pages)
