"""OCR regression tests. They need the system Tesseract binary (skipped otherwise)."""
import shutil

import cv2
import numpy as np
import pytest

from app.services.ocr import extract_text

pytestmark = pytest.mark.skipif(shutil.which("tesseract") is None,
                                reason="Tesseract is not installed")

LINES = [
    "MAHARASHTRA STATE ELECTRICITY DISTRIBUTION CO",
    "Electricity Bill - Tax Invoice",
    "Consumer No: 1234567890",
    "Bill Date: 05/10/2026",
    "Due Date: 25/10/2026",
    "Units Consumed: 721 kWh",
    "TOTAL AMOUNT PAYABLE    3860.00",
]


def _bill_image() -> np.ndarray:
    img = np.full((700, 1300, 3), 255, np.uint8)
    for i, line in enumerate(LINES):
        scale = 1.1 if i else 0.9
        cv2.putText(img, line, (45, 90 + i * 80), cv2.FONT_HERSHEY_SIMPLEX, scale, (15, 15, 15), 2, cv2.LINE_AA)
    return img


def _phone_photo(img: np.ndarray) -> np.ndarray:
    """Sideways, skewed, unevenly lit, blurry, noisy and JPEG-compressed."""
    h, w = img.shape[:2]
    src = np.float32([[0, 0], [w, 0], [w, h], [0, h]])
    dst = np.float32([[30, 20], [w - 8, 55], [w - 45, h - 10], [12, h - 35]])
    out = cv2.warpPerspective(img, cv2.getPerspectiveTransform(src, dst), (w, h), borderValue=(80, 80, 80))
    out = cv2.rotate(out, cv2.ROTATE_90_CLOCKWISE)

    yy, xx = np.mgrid[0:out.shape[0], 0:out.shape[1]]
    shade = 0.62 + 0.38 * np.clip(1 - np.abs(xx - out.shape[1] * 0.3) / out.shape[1], 0, 1)
    out = (out.astype(np.float32) * shade[..., None]).clip(0, 255).astype(np.uint8)
    out = cv2.GaussianBlur(out, (0, 0), 1.1)
    noise = np.random.default_rng(3).normal(0, 7, out.shape)
    out = (out.astype(np.float32) + noise).clip(0, 255).astype(np.uint8)
    ok, enc = cv2.imencode(".jpg", out, [cv2.IMWRITE_JPEG_QUALITY, 70])
    assert ok
    return cv2.imdecode(enc, cv2.IMREAD_COLOR)


def test_reads_a_sideways_shadowed_phone_photo(tmp_path):
    path = tmp_path / "bill.jpg"
    cv2.imwrite(str(path), _phone_photo(_bill_image()))

    text = extract_text(str(path), "jpg")
    flat = " ".join(text.split()).upper()

    assert "3860" in flat
    assert "25/10/2026" in flat
    assert "TOTAL AMOUNT PAYABLE" in flat
    assert "UNITS CONSUMED" in flat
    assert "1234567890" in flat


def test_blank_image_returns_empty_text(tmp_path):
    path = tmp_path / "blank.png"
    cv2.imwrite(str(path), np.full((600, 800, 3), 255, np.uint8))
    assert extract_text(str(path), "png") == ""
