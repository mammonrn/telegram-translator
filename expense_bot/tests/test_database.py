from datetime import date
from decimal import Decimal

import pytest

from database import ExpenseDatabase, ExpenseRecord
from sheet import ExpenseRow


class FakeSheetManager:
    """In-memory stand-in for SheetManager, avoiding real Google API calls."""

    def __init__(self):
        self.rows: list[ExpenseRow] = []

    def append_expense(self, row: ExpenseRow) -> int:
        self.rows.append(row)
        return len(self.rows) + 1  # +1 to account for the header row

    def get_all_records(self) -> list[dict[str, str]]:
        from config import SHEET_HEADERS

        return [dict(zip(SHEET_HEADERS, row.as_list())) for row in self.rows]

    def find_duplicate(self, reference_number, amount, slip_date, slip_time):
        for idx, row in enumerate(self.rows, start=2):
            if reference_number and row.reference_number == reference_number:
                return idx
            if not reference_number and row.amount == amount and row.date == slip_date and row.time == slip_time:
                return idx
        return None

    def update_row(self, row_number, values):
        row = self.rows[row_number - 2]
        for k, v in values.items():
            setattr(row, _header_to_attr(k), v)

    def delete_row(self, row_number):
        del self.rows[row_number - 2]


def _header_to_attr(header: str) -> str:
    mapping = {
        "Amount": "amount", "Category": "category", "Remark": "remark",
        "Bank": "bank", "Sender": "sender", "Receiver": "receiver",
    }
    return mapping[header]


def make_record(**overrides) -> ExpenseRecord:
    defaults = dict(
        date=date(2026, 7, 9),
        time_str="14:35:00",
        amount=Decimal("1250.00"),
        bank="Bangkok Bank",
        sender="John",
        receiver="Jane",
        reference_number="TXN123",
        category="Food",
        remark="",
        drive_url="https://drive.google.com/x",
        telegram_file_id="file123",
        ocr_confidence=0.9,
        user_id=42,
    )
    defaults.update(overrides)
    return ExpenseRecord(**defaults)


@pytest.fixture
def db():
    return ExpenseDatabase(FakeSheetManager())


def test_save_then_find_duplicate_by_reference(db):
    record = make_record()
    db.save(record)

    dup_row = db.find_duplicate_row(make_record(amount=Decimal("999.00")))
    assert dup_row is not None


def test_no_duplicate_for_distinct_reference(db):
    db.save(make_record())
    dup_row = db.find_duplicate_row(make_record(reference_number="OTHER456"))
    assert dup_row is None


def test_duplicate_by_amount_date_time_when_no_reference(db):
    db.save(make_record(reference_number=""))
    dup_row = db.find_duplicate_row(make_record(reference_number=""))
    assert dup_row is not None


def test_search_by_category_filters_correctly(db):
    db.save(make_record(category="Food", reference_number="R1"))
    db.save(make_record(category="Bills", reference_number="R2"))

    results = db.search_by_category("Food", user_id=42)
    assert len(results) == 1
    assert results[0]["Reference Number"] == "R1"


def test_monthly_stats_aggregates_by_category(db):
    db.save(make_record(category="Food", amount=Decimal("100.00"), reference_number="R1"))
    db.save(make_record(category="Food", amount=Decimal("50.00"), reference_number="R2"))
    db.save(make_record(category="Bills", amount=Decimal("30.00"), reference_number="R3"))

    totals = db.monthly_stats(2026, 7, user_id=42)
    assert totals["Food"] == Decimal("150.00")
    assert totals["Bills"] == Decimal("30.00")


def test_delete_removes_record(db):
    db.save(make_record(reference_number="R1"))
    row = db.find_duplicate_row(make_record(reference_number="R1"))
    db.delete(row)

    assert db.all_records() == []


def test_edit_field_updates_value(db):
    db.save(make_record(reference_number="R1", category="Food"))
    row = db.find_duplicate_row(make_record(reference_number="R1"))
    db.edit_field(row, {"Category": "Bills"})

    records = db.all_records()
    assert records[0]["Category"] == "Bills"
