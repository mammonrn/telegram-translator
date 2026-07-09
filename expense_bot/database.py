"""Expense record management: the domain layer on top of `SheetManager`.

Google Sheets is the system of record; this module provides a typed,
testable facade (dedup detection, search, edit, delete, stats, export)
so handlers never talk to gspread directly.
"""

from __future__ import annotations

import csv
import io
import logging
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from sheet import ExpenseRow, SheetManager

logger = logging.getLogger("expense_bot.database")


@dataclass
class ExpenseRecord:
    """A fully-resolved expense, ready to be written to the sheet."""

    date: date
    time_str: str
    amount: Decimal
    bank: str
    sender: str
    receiver: str
    reference_number: str
    category: str
    remark: str
    drive_url: str
    telegram_file_id: str
    ocr_confidence: float
    user_id: int

    def to_row(self) -> ExpenseRow:
        return ExpenseRow(
            date=self.date.isoformat(),
            time=self.time_str,
            amount=f"{self.amount:.2f}",
            bank=self.bank,
            sender=self.sender,
            receiver=self.receiver,
            reference_number=self.reference_number,
            category=self.category,
            remark=self.remark,
            drive_url=self.drive_url,
            telegram_file_id=self.telegram_file_id,
            ocr_confidence=f"{self.ocr_confidence:.2f}",
            user_id=str(self.user_id),
        )


class ExpenseDatabase:
    """High-level operations used by the Telegram handlers."""

    def __init__(self, sheet_manager: SheetManager) -> None:
        self._sheets = sheet_manager

    # -- writes --------------------------------------------------------------

    def find_duplicate_row(self, record: ExpenseRecord) -> Optional[int]:
        return self._sheets.find_duplicate(
            reference_number=record.reference_number,
            amount=f"{record.amount:.2f}",
            slip_date=record.date.isoformat(),
            slip_time=record.time_str,
        )

    def save(self, record: ExpenseRecord) -> int:
        row_number = self._sheets.append_expense(record.to_row())
        logger.info(
            "Saved expense: user=%s amount=%s category=%s row=%d",
            record.user_id, record.amount, record.category, row_number,
        )
        return row_number

    def edit_field(self, row_number: int, field_updates: dict[str, str]) -> None:
        self._sheets.update_row(row_number, field_updates)

    def delete(self, row_number: int) -> None:
        self._sheets.delete_row(row_number)

    # -- reads / search --------------------------------------------------------

    def all_records(self, user_id: Optional[int] = None) -> list[dict[str, str]]:
        records = self._sheets.get_all_records()
        if user_id is not None:
            records = [r for r in records if str(r.get("User ID")) == str(user_id)]
        return records

    def search_by_category(self, category: str, user_id: Optional[int] = None) -> list[dict[str, str]]:
        return [
            r for r in self.all_records(user_id)
            if r.get("Category", "").lower() == category.lower()
        ]

    def search_by_date(
        self, start: date, end: date, user_id: Optional[int] = None
    ) -> list[dict[str, str]]:
        results = []
        for r in self.all_records(user_id):
            try:
                record_date = datetime.strptime(r.get("Date", ""), "%Y-%m-%d").date()
            except ValueError:
                continue
            if start <= record_date <= end:
                results.append(r)
        return results

    def monthly_stats(self, year: int, month: int, user_id: Optional[int] = None) -> dict[str, Decimal]:
        return self._aggregate_by_category(
            self.search_by_date(date(year, month, 1), _month_end(year, month), user_id)
        )

    def yearly_stats(self, year: int, user_id: Optional[int] = None) -> dict[str, Decimal]:
        return self._aggregate_by_category(
            self.search_by_date(date(year, 1, 1), date(year, 12, 31), user_id)
        )

    @staticmethod
    def _aggregate_by_category(records: list[dict[str, str]]) -> dict[str, Decimal]:
        totals: dict[str, Decimal] = {}
        for r in records:
            category = r.get("Category", "Other") or "Other"
            try:
                amount = Decimal(str(r.get("Amount", "0")) or "0")
            except Exception:  # noqa: BLE001
                amount = Decimal("0")
            totals[category] = totals.get(category, Decimal("0")) + amount
        return totals

    # -- export ------------------------------------------------------------

    def export_csv(self, user_id: Optional[int] = None) -> bytes:
        records = self.all_records(user_id)
        buf = io.StringIO()
        if records:
            writer = csv.DictWriter(buf, fieldnames=list(records[0].keys()))
            writer.writeheader()
            writer.writerows(records)
        return buf.getvalue().encode("utf-8-sig")

    def export_excel(self, user_id: Optional[int] = None) -> bytes:
        import pandas as pd

        records = self.all_records(user_id)
        df = pd.DataFrame(records)
        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine="openpyxl") as writer:
            df.to_excel(writer, index=False, sheet_name="Expenses")
        return buf.getvalue()


def _month_end(year: int, month: int) -> date:
    if month == 12:
        return date(year, 12, 31)
    next_month_first = date(year, month + 1, 1)
    from datetime import timedelta

    return next_month_first - timedelta(days=1)
