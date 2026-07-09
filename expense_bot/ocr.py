"""OCR extraction for bank transfer slips (Thai and English).

Primary engine: Google Cloud Vision (`google-cloud-vision`).
Fallback engine: Tesseract OCR (`pytesseract`), used automatically if Vision
credentials/API are unavailable or raise an error, and for local dev
without a Vision-enabled GCP project.

Both engines funnel into the same `parse_slip_text` regex/heuristic parser
so downstream code never has to care which engine produced the text.
"""

from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass, field
from datetime import date, time as dt_time
from decimal import Decimal
from typing import Optional

from utils import THAI_MONTHS, ENGLISH_MONTHS, parse_amount, thai_year_to_gregorian

logger = logging.getLogger("expense_bot.ocr")

# Known bank names/keywords -> canonical display name. Matched case-insensitively
# against OCR text. Covers major Thai banks (Thai + English slip variants) plus
# a handful of common international ones.
BANK_KEYWORDS: list[tuple[str, str]] = [
    ("กสิกรไทย", "Kasikornbank (KBank)"),
    ("kasikorn", "Kasikornbank (KBank)"),
    ("kbank", "Kasikornbank (KBank)"),
    ("ไทยพาณิชย์", "Siam Commercial Bank (SCB)"),
    ("scb", "Siam Commercial Bank (SCB)"),
    ("กรุงเทพ", "Bangkok Bank"),
    ("bangkok bank", "Bangkok Bank"),
    ("bbl", "Bangkok Bank"),
    ("กรุงไทย", "Krungthai Bank (KTB)"),
    ("krungthai", "Krungthai Bank (KTB)"),
    ("ktb", "Krungthai Bank (KTB)"),
    ("กรุงศรี", "Krungsri (Bank of Ayudhya)"),
    ("krungsri", "Krungsri (Bank of Ayudhya)"),
    ("bay", "Krungsri (Bank of Ayudhya)"),
    ("ทหารไทยธนชาต", "TTB Bank"),
    ("ttb", "TTB Bank"),
    ("ธนชาต", "TTB Bank"),
    ("ออมสิน", "Government Savings Bank"),
    ("gsb", "Government Savings Bank"),
    ("ธ.ก.ส", "BAAC"),
    ("baac", "BAAC"),
    ("ซีไอเอ็มบี", "CIMB Thai"),
    ("cimb", "CIMB Thai"),
    ("ยูโอบี", "UOB"),
    ("uob", "UOB"),
    ("แลนด์ แอนด์ เฮ้าส์", "LH Bank"),
    ("lh bank", "LH Bank"),
    ("promptpay", "PromptPay"),
    ("truemoney", "TrueMoney Wallet"),
    ("chase", "Chase Bank"),
    ("bank of america", "Bank of America"),
    ("wells fargo", "Wells Fargo"),
    ("hsbc", "HSBC"),
    ("citibank", "Citibank"),
]

_DATE_PATTERNS = [
    # 09/07/2026, 09-07-26, 09.07.2026
    re.compile(r"(?P<d>\d{1,2})[/.\-](?P<m>\d{1,2})[/.\-](?P<y>\d{2,4})"),
]
_THAI_DATE_PATTERN = re.compile(
    r"(?P<d>\d{1,2})\s*(?P<month>" + "|".join(map(re.escape, THAI_MONTHS.keys())) + r")\s*(?P<y>\d{2,4})"
)
_ENGLISH_TEXT_DATE_PATTERN = re.compile(
    r"(?P<d>\d{1,2})\s+(?P<month>" + "|".join(map(re.escape, ENGLISH_MONTHS.keys())) + r")\.?,?\s+(?P<y>\d{2,4})",
    re.IGNORECASE,
)
_TIME_PATTERN = re.compile(r"(?P<h>\d{1,2}):(?P<mi>\d{2})(?::(?P<s>\d{2}))?")
_AMOUNT_LABEL_PATTERN = re.compile(
    r"(?:amount|จำนวนเงิน|จำนวน|total)\D{0,5}([\d,]+\.\d{2}|[\d,]+)", re.IGNORECASE
)
_STANDALONE_AMOUNT_PATTERN = re.compile(r"\b(\d{1,3}(?:,\d{3})*\.\d{2}|\d+\.\d{2})\b")
_REF_PATTERN = re.compile(
    r"(?:ref(?:erence)?(?:\s*no\.?)?|เลขที่รายการ|รหัสอ้างอิง)\s*[:\-]?\s*([A-Za-z0-9]{6,25})",
    re.IGNORECASE,
)
_NAME_LINE_PATTERN = re.compile(r"^[A-Za-zก-๙\s.]{2,40}$")


@dataclass
class OCRResult:
    """Structured fields extracted from a slip, plus confidence metadata."""

    amount: Optional[Decimal] = None
    bank: Optional[str] = None
    slip_date: Optional[date] = None
    slip_time: Optional[dt_time] = None
    sender: Optional[str] = None
    receiver: Optional[str] = None
    reference_number: Optional[str] = None
    raw_text: str = ""
    confidence: float = 0.0
    engine: str = "unknown"
    field_confidence: dict[str, bool] = field(default_factory=dict)

    @property
    def amount_is_confident(self) -> bool:
        return self.amount is not None and self.field_confidence.get("amount", False)


def parse_slip_text(text: str, base_confidence: float = 0.0) -> OCRResult:
    """Extract structured fields from raw OCR text of a bank slip.

    `base_confidence` is the OCR engine's own average confidence (0-1) if
    available (Vision provides per-symbol confidence; Tesseract provides
    per-word confidence). The final confidence score blends engine
    confidence with how many expected fields were successfully parsed.
    """
    result = OCRResult(raw_text=text)
    found: dict[str, bool] = {}

    # --- Bank ---
    lowered = text.lower()
    for keyword, canonical in BANK_KEYWORDS:
        if keyword.lower() in lowered or keyword in text:
            result.bank = canonical
            found["bank"] = True
            break

    # --- Amount --- (prefer a labelled "Amount: 1,234.00" over bare numbers)
    amount_match = _AMOUNT_LABEL_PATTERN.search(text)
    if amount_match:
        result.amount = parse_amount(amount_match.group(1))
        found["amount"] = True
    else:
        candidates = _STANDALONE_AMOUNT_PATTERN.findall(text)
        if candidates:
            # Heuristic: the transfer amount is usually the largest decimal
            # figure on the slip (fees/reference numbers are smaller or
            # integer-only).
            parsed = [parse_amount(c) for c in candidates]
            parsed = [p for p in parsed if p is not None]
            if parsed:
                result.amount = max(parsed)
                found["amount"] = False  # unlabeled guess - lower confidence

    # --- Date ---
    thai_match = _THAI_DATE_PATTERN.search(text)
    eng_text_match = _ENGLISH_TEXT_DATE_PATTERN.search(text)
    numeric_match = None
    for pat in _DATE_PATTERNS:
        m = pat.search(text)
        if m:
            numeric_match = m
            break

    if thai_match:
        day = int(thai_match.group("d"))
        month = THAI_MONTHS[thai_match.group("month")]
        year = thai_year_to_gregorian(int(thai_match.group("y")) if len(thai_match.group("y")) == 4 else 2500 + int(thai_match.group("y")))
        try:
            result.slip_date = date(year, month, day)
            found["date"] = True
        except ValueError:
            pass
    elif eng_text_match:
        day = int(eng_text_match.group("d"))
        month = ENGLISH_MONTHS[eng_text_match.group("month").lower()]
        year_raw = int(eng_text_match.group("y"))
        year = year_raw if year_raw >= 100 else 2000 + year_raw
        try:
            result.slip_date = date(year, month, day)
            found["date"] = True
        except ValueError:
            pass
    elif numeric_match:
        day = int(numeric_match.group("d"))
        month = int(numeric_match.group("m"))
        year_raw = int(numeric_match.group("y"))
        if year_raw < 100:
            year = 2000 + year_raw
        else:
            year = thai_year_to_gregorian(year_raw)
        try:
            result.slip_date = date(year, month, day)
            found["date"] = True
        except ValueError:
            # try day/month swapped (some slips use MM/DD)
            try:
                result.slip_date = date(year, day, month)
                found["date"] = True
            except ValueError:
                pass

    # --- Time ---
    time_match = _TIME_PATTERN.search(text)
    if time_match:
        try:
            result.slip_time = dt_time(
                int(time_match.group("h")),
                int(time_match.group("mi")),
                int(time_match.group("s") or 0),
            )
            found["time"] = True
        except ValueError:
            pass

    # --- Reference number ---
    ref_match = _REF_PATTERN.search(text)
    if ref_match:
        result.reference_number = ref_match.group(1)
        found["reference_number"] = True

    # --- Sender / Receiver --- best-effort: look for lines following
    # "From"/"จาก" and "To"/"ถึง"/"ไปยัง" labels.
    sender, receiver = _extract_names(text)
    if sender:
        result.sender = sender
        found["sender"] = True
    if receiver:
        result.receiver = receiver
        found["receiver"] = True

    result.field_confidence = found
    result.confidence = _score_confidence(found, base_confidence)
    return result


def _extract_names(text: str) -> tuple[Optional[str], Optional[str]]:
    sender = None
    receiver = None
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for i, line in enumerate(lines):
        low = line.lower()
        if sender is None and re.search(r"\b(from|จาก|ผู้โอน)\b", low):
            sender = _next_name_value(line, lines, i)
        if receiver is None and re.search(r"\b(to|ถึง|ไปยัง|ผู้รับ)\b", low):
            receiver = _next_name_value(line, lines, i)
    return sender, receiver


def _next_name_value(line: str, lines: list[str], index: int) -> Optional[str]:
    # Value may be on the same line after a colon, or on the next line.
    if ":" in line:
        candidate = line.split(":", 1)[1].strip()
        if candidate:
            return candidate[:60]
    if index + 1 < len(lines):
        candidate = lines[index + 1].strip()
        if _NAME_LINE_PATTERN.match(candidate):
            return candidate[:60]
    return None


def _score_confidence(found: dict[str, bool], base_confidence: float) -> float:
    """Blend engine OCR confidence with parse completeness of key fields."""
    key_fields = ["amount", "date", "bank"]
    hits = sum(1 for f in key_fields if found.get(f))
    completeness = hits / len(key_fields)
    if base_confidence > 0:
        return round(0.6 * base_confidence + 0.4 * completeness, 3)
    return round(completeness, 3)


class OCREngine:
    """Runs OCR against image/PDF bytes, preferring Google Vision.

    Falls back to Tesseract automatically on any Vision failure (missing
    credentials, API not enabled, quota, network error) so the bot keeps
    working in degraded mode instead of failing the whole upload.
    """

    def __init__(self, tesseract_cmd: str | None = None) -> None:
        self._vision_client = None
        if tesseract_cmd:
            import pytesseract

            pytesseract.pytesseract.tesseract_cmd = tesseract_cmd
        self._init_vision()

    def _init_vision(self) -> None:
        try:
            from google.cloud import vision

            self._vision_client = vision.ImageAnnotatorClient()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Google Vision OCR unavailable, will use Tesseract fallback: %s", exc)
            self._vision_client = None

    def extract_text(self, image_bytes: bytes, is_pdf: bool = False) -> tuple[str, float, str]:
        """Return (raw_text, engine_confidence, engine_name)."""
        if is_pdf:
            image_bytes = _pdf_first_page_to_png(image_bytes)

        if self._vision_client is not None:
            try:
                return self._extract_with_vision(image_bytes)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Vision OCR failed, falling back to Tesseract: %s", exc)

        return self._extract_with_tesseract(image_bytes)

    def _extract_with_vision(self, image_bytes: bytes) -> tuple[str, float, str]:
        from google.cloud import vision

        image = vision.Image(content=image_bytes)
        response = self._vision_client.text_detection(image=image)
        if response.error.message:
            raise RuntimeError(response.error.message)

        annotations = response.text_annotations
        if not annotations:
            return "", 0.0, "vision"

        text = annotations[0].description
        # Vision's text_detection doesn't return a single confidence score;
        # approximate using per-page confidence from full_text_annotation.
        confidence = 0.0
        try:
            pages = response.full_text_annotation.pages
            if pages:
                confidence = sum(p.confidence for p in pages) / len(pages)
        except Exception:  # noqa: BLE001
            confidence = 0.0
        return text, confidence, "vision"

    def _extract_with_tesseract(self, image_bytes: bytes) -> tuple[str, float, str]:
        import pytesseract
        from PIL import Image as PILImage

        with PILImage.open(io.BytesIO(image_bytes)) as img:
            img = img.convert("RGB")
            data = pytesseract.image_to_data(
                img, lang="tha+eng", output_type=pytesseract.Output.DICT
            )
            words = [w for w in data["text"] if w.strip()]
            confidences = [
                int(c) for c, w in zip(data["conf"], data["text"]) if w.strip() and int(c) >= 0
            ]
            text = " ".join(words)
            avg_conf = (sum(confidences) / len(confidences) / 100.0) if confidences else 0.0
        return text, avg_conf, "tesseract"


def _pdf_first_page_to_png(pdf_bytes: bytes) -> bytes:
    from pdf2image import convert_from_bytes

    pages = convert_from_bytes(pdf_bytes, first_page=1, last_page=1)
    if not pages:
        raise ValueError("PDF has no pages")
    buf = io.BytesIO()
    pages[0].save(buf, format="PNG")
    return buf.getvalue()
