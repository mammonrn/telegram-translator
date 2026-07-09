from datetime import date, time
from decimal import Decimal

from ocr import parse_slip_text


def test_parse_english_slip():
    text = """
    Bangkok Bank
    Transfer Successful
    Date 09/07/2026 Time 14:35
    Amount 1,250.00 THB
    From: John Smith
    To: Jane Doe
    Ref: TXN123456789
    """
    result = parse_slip_text(text, base_confidence=0.9)

    assert result.bank == "Bangkok Bank"
    assert result.amount == Decimal("1250.00")
    assert result.slip_date == date(2026, 7, 9)
    assert result.slip_time == time(14, 35)
    assert result.reference_number == "TXN123456789"
    assert result.confidence > 0.5


def test_parse_thai_slip():
    text = """
    ธนาคารกสิกรไทย
    โอนเงินสำเร็จ
    วันที่ 9 ก.ค. 2569 เวลา 09:15
    จำนวนเงิน 500.50 บาท
    เลขที่รายการ ABC987654321
    """
    result = parse_slip_text(text, base_confidence=0.85)

    assert result.bank == "Kasikornbank (KBank)"
    assert result.amount == Decimal("500.50")
    assert result.slip_date == date(2026, 7, 9)
    assert result.slip_time == time(9, 15)
    assert result.reference_number == "ABC987654321"


def test_parse_slip_missing_fields_lowers_confidence():
    text = "some unrelated OCR noise with no useful fields"
    result = parse_slip_text(text, base_confidence=0.9)

    assert result.amount is None
    assert result.bank is None
    assert result.confidence < 0.6


def test_parse_slip_picks_largest_amount_when_unlabeled():
    text = "Fee 5.00 Total paid 999.00 Reference misc 12.34"
    result = parse_slip_text(text)

    assert result.amount == Decimal("999.00")
