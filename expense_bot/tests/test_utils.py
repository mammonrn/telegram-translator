import io
from decimal import Decimal

from PIL import Image

from utils import compress_image, parse_amount, safe_filename, thai_year_to_gregorian


def test_parse_amount_plain():
    assert parse_amount("1250.00") == Decimal("1250.00")


def test_parse_amount_with_thousands_separator():
    assert parse_amount("1,234.56") == Decimal("1234.56")


def test_parse_amount_with_currency_symbol():
    assert parse_amount("฿1,000") == Decimal("1000")


def test_parse_amount_invalid_returns_none():
    assert parse_amount("not a number") is None


def test_parse_amount_empty_returns_none():
    assert parse_amount("") is None


def test_thai_year_to_gregorian_converts_buddhist_era():
    assert thai_year_to_gregorian(2569) == 2026


def test_thai_year_to_gregorian_passes_through_gregorian():
    assert thai_year_to_gregorian(2026) == 2026


def test_safe_filename_strips_unsafe_chars():
    assert safe_filename("slip #1 (bank).jpg") == "slip_1_bank_.jpg"


def test_compress_image_reduces_large_image_size():
    img = Image.new("RGB", (4000, 3000), color=(120, 40, 200))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    original = buf.getvalue()

    compressed = compress_image(original, max_dimension=1000, quality=80)

    assert len(compressed) < len(original)
    with Image.open(io.BytesIO(compressed)) as out:
        assert max(out.size) <= 1000


def test_compress_image_handles_undecodable_bytes():
    garbage = b"not-an-image"
    assert compress_image(garbage) == garbage
