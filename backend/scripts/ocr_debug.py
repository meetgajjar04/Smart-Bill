"""Inspect what the OCR pipeline reads from one file.

    cd backend && .venv/bin/python -m scripts.ocr_debug /path/to/bill.jpg
    cd backend && .venv/bin/python -m scripts.ocr_debug bill.jpg --save-dir /tmp/ocr

Prints the chosen preprocessing view, orientation, confidence and the extracted text.
--save-dir also writes every preprocessing view (flattened, otsu, enhanced, inverted)
so you can see exactly which one Tesseract was given.
"""
import argparse
import time
from pathlib import Path

import cv2

from app.services import ocr


def main() -> None:
    ap = argparse.ArgumentParser(description="Debug local OCR on a bill photo/PDF.")
    ap.add_argument("file", help="JPG, PNG or PDF")
    ap.add_argument("--save-dir", help="write the preprocessing views here")
    args = ap.parse_args()

    path = Path(args.file)
    kind = "pdf" if path.suffix.lower() == ".pdf" else "image"

    t0 = time.time()
    if kind == "pdf":
        text = ocr.extract_text(str(path), "pdf")
        print(f"\nPDF text ({time.time() - t0:.1f}s, {len(text)} chars)\n{'-' * 60}\n{text}")
        return

    img = ocr._read_image(str(path))
    pt = ocr._tesseract()
    lang = ocr._lang()
    print(f"image: {img.shape[1]}x{img.shape[0]}px")

    turns = ocr._osd_rotation(pt, img)
    print(f"orientation: {'unknown' if turns is None else f'{turns * 90} deg'}")
    if turns:
        img = ocr._rotate_quarter(img, turns)

    prepared = ocr._prepare(img)
    print(f"prepared: {prepared.shape[1]}x{prepared.shape[0]}px (deskewed + resized)")

    save = Path(args.save_dir) if args.save_dir else None
    if save:
        save.mkdir(parents=True, exist_ok=True)

    for label, view in ocr._variants(prepared):
        t = time.time()
        cand = ocr._run(pt, view, 6, lang, label)
        print(f"  {label:16s} {time.time() - t:5.1f}s  conf {cand.mean_conf:5.1f}  "
              f"words {cand.words:3d}  rank {cand.rank:6.0f}")
        if save:
            cv2.imwrite(str(save / f"{label}.png"), view)

    final = ocr._read_page(pt, img, lang)
    if turns is None and (final.mean_conf < 70 or final.words < 5):
        guess = ocr._probe_orientation(pt, img, lang)
        print(f"orientation probe: {guess * 90} deg")
        if guess:
            cand = ocr._read_page(pt, ocr._rotate_quarter(img, guess), lang)
            if cand.rank > final.rank:
                final = cand

    total = time.time() - t0
    print(f"\npicked: {final.label or '-'} | conf {final.mean_conf:.1f} | "
          f"words {final.words} | total {total:.1f}s | {len(final.text)} chars")
    print("-" * 60)
    print(final.text)


if __name__ == "__main__":
    main()
