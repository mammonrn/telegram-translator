"""Google Sheets integration: the 'Expenses' ledger and 'Summary' dashboard.

Uses gspread (built on the Sheets v4 API) for row-level operations, and the
raw `spreadsheet.batch_update` API for chart creation, since gspread has no
first-class chart support.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from datetime import date, time as dt_time
from decimal import Decimal
from typing import Optional

import gspread
from google.oauth2 import service_account

from config import CATEGORIES, EXPENSES_SHEET_NAME, SHEET_HEADERS, SUMMARY_SHEET_NAME
from utils import sync_retry

logger = logging.getLogger("expense_bot.sheet")

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


@dataclass
class ExpenseRow:
    """One row of the Expenses worksheet."""

    date: str
    time: str
    amount: str
    bank: str
    sender: str
    receiver: str
    reference_number: str
    category: str
    remark: str
    drive_url: str
    telegram_file_id: str
    ocr_confidence: str
    user_id: str

    def as_list(self) -> list[str]:
        return [
            self.date, self.time, self.amount, self.bank, self.sender,
            self.receiver, self.reference_number, self.category, self.remark,
            self.drive_url, self.telegram_file_id, self.ocr_confidence, self.user_id,
        ]


class SheetManager:
    """Manages the Expenses ledger and the auto-generated Summary sheet."""

    def __init__(self, credentials_path: str, spreadsheet_id: str) -> None:
        creds = service_account.Credentials.from_service_account_file(
            credentials_path, scopes=SCOPES
        )
        self._client = gspread.authorize(creds)
        self._spreadsheet = self._client.open_by_key(spreadsheet_id)
        self._expenses_ws = self._ensure_expenses_sheet()
        self._summary_ws = self._ensure_summary_sheet()

    # -- setup -----------------------------------------------------------

    def _ensure_expenses_sheet(self) -> gspread.Worksheet:
        try:
            ws = self._spreadsheet.worksheet(EXPENSES_SHEET_NAME)
        except gspread.exceptions.WorksheetNotFound:
            ws = self._spreadsheet.add_worksheet(
                title=EXPENSES_SHEET_NAME, rows=1000, cols=len(SHEET_HEADERS)
            )
        existing_header = ws.row_values(1)
        if existing_header != SHEET_HEADERS:
            ws.update("A1", [SHEET_HEADERS])
            ws.freeze(rows=1)
        return ws

    def _ensure_summary_sheet(self) -> gspread.Worksheet:
        try:
            ws = self._spreadsheet.worksheet(SUMMARY_SHEET_NAME)
            created = False
        except gspread.exceptions.WorksheetNotFound:
            ws = self._spreadsheet.add_worksheet(title=SUMMARY_SHEET_NAME, rows=100, cols=10)
            created = True
        if created:
            self._build_summary_layout(ws)
        return ws

    def _build_summary_layout(self, ws: gspread.Worksheet) -> None:
        """Write formula-driven summary cells and a per-category breakdown."""
        data_range = f"'{EXPENSES_SHEET_NAME}'!C2:C"  # Amount column
        date_range = f"'{EXPENSES_SHEET_NAME}'!A2:A"  # Date column
        cat_range = f"'{EXPENSES_SHEET_NAME}'!H2:H"  # Category column

        rows: list[list[str]] = [
            ["Expense Summary", ""],
            ["Total Expenses", f"=SUM({data_range})"],
            [
                "This Month",
                f"=SUMIFS({data_range},{date_range},\">=\"&EOMONTH(TODAY(),-1)+1,"
                f"{date_range},\"<=\"&EOMONTH(TODAY(),0))",
            ],
            [
                "This Year",
                f"=SUMIFS({data_range},{date_range},\">=\"&DATE(YEAR(TODAY()),1,1),"
                f"{date_range},\"<=\"&DATE(YEAR(TODAY()),12,31))",
            ],
            ["", ""],
            ["Category", "Total"],
        ]
        start_category_row = len(rows) + 1
        for key, (emoji, label) in CATEGORIES.items():
            rows.append(
                [f"{emoji} {label}", f"=SUMIF({cat_range},\"{label}\",{data_range})"]
            )

        rows.append(["", ""])
        rows.append(["Month", "Total"])
        month_header_row = len(rows)
        for m in range(1, 13):
            rows.append(
                [
                    f"=TEXT(DATE(YEAR(TODAY()),{m},1),\"MMMM\")",
                    f"=SUMPRODUCT(({date_range}<>\"\")*(MONTH({date_range})={m})*"
                    f"(YEAR({date_range})=YEAR(TODAY()))*{data_range})",
                ]
            )

        ws.update("A1", rows, value_input_option="USER_ENTERED")
        ws.format("A1", {"textFormat": {"bold": True, "fontSize": 14}})
        ws.format("A2:A4", {"textFormat": {"bold": True}})
        ws.format(f"A6:B6", {"textFormat": {"bold": True}})
        ws.format(f"A{month_header_row}:B{month_header_row}", {"textFormat": {"bold": True}})

        self._add_charts(
            ws,
            category_start_row=start_category_row,
            category_end_row=start_category_row + len(CATEGORIES) - 1,
            month_start_row=month_header_row + 1,
            month_end_row=month_header_row + 12,
        )

    def _add_charts(
        self,
        ws: gspread.Worksheet,
        category_start_row: int,
        category_end_row: int,
        month_start_row: int,
        month_end_row: int,
    ) -> None:
        """Add a category pie chart and a monthly trend line chart."""
        sheet_id = ws.id
        requests = [
            {
                "addChart": {
                    "chart": {
                        "spec": {
                            "title": "Expenses by Category",
                            "pieChart": {
                                "legendPosition": "RIGHT_LEGEND",
                                "domain": {
                                    "sourceRange": {
                                        "sources": [
                                            {
                                                "sheetId": sheet_id,
                                                "startRowIndex": category_start_row - 1,
                                                "endRowIndex": category_end_row,
                                                "startColumnIndex": 0,
                                                "endColumnIndex": 1,
                                            }
                                        ]
                                    }
                                },
                                "series": {
                                    "sourceRange": {
                                        "sources": [
                                            {
                                                "sheetId": sheet_id,
                                                "startRowIndex": category_start_row - 1,
                                                "endRowIndex": category_end_row,
                                                "startColumnIndex": 1,
                                                "endColumnIndex": 2,
                                            }
                                        ]
                                    }
                                },
                            },
                        },
                        "position": {
                            "overlayPosition": {
                                "anchorCell": {"sheetId": sheet_id, "rowIndex": 0, "columnIndex": 3}
                            }
                        },
                    }
                }
            },
            {
                "addChart": {
                    "chart": {
                        "spec": {
                            "title": "Monthly Trend",
                            "basicChart": {
                                "chartType": "LINE",
                                "legendPosition": "BOTTOM_LEGEND",
                                "domains": [
                                    {
                                        "domain": {
                                            "sourceRange": {
                                                "sources": [
                                                    {
                                                        "sheetId": sheet_id,
                                                        "startRowIndex": month_start_row - 1,
                                                        "endRowIndex": month_end_row,
                                                        "startColumnIndex": 0,
                                                        "endColumnIndex": 1,
                                                    }
                                                ]
                                            }
                                        }
                                    }
                                ],
                                "series": [
                                    {
                                        "series": {
                                            "sourceRange": {
                                                "sources": [
                                                    {
                                                        "sheetId": sheet_id,
                                                        "startRowIndex": month_start_row - 1,
                                                        "endRowIndex": month_end_row,
                                                        "startColumnIndex": 1,
                                                        "endColumnIndex": 2,
                                                    }
                                                ]
                                            }
                                        },
                                        "targetAxis": "LEFT_AXIS",
                                    }
                                ],
                            },
                        },
                        "position": {
                            "overlayPosition": {
                                "anchorCell": {"sheetId": sheet_id, "rowIndex": 20, "columnIndex": 3}
                            }
                        },
                    }
                }
            },
        ]
        try:
            self._spreadsheet.batch_update({"requests": requests})
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not create summary charts: %s", exc)

    # -- row operations ----------------------------------------------------

    @sync_retry(exceptions=(Exception,))
    def append_expense(self, row: ExpenseRow) -> int:
        """Append a row and return its 1-indexed row number."""
        self._expenses_ws.append_row(row.as_list(), value_input_option="USER_ENTERED")
        return len(self._expenses_ws.col_values(1))

    @sync_retry(exceptions=(Exception,))
    def get_all_records(self) -> list[dict[str, str]]:
        """Return all expense rows as dicts keyed by header name."""
        return self._expenses_ws.get_all_records(expected_headers=SHEET_HEADERS)

    @sync_retry(exceptions=(Exception,))
    def find_duplicate(
        self, reference_number: str, amount: str, slip_date: str, slip_time: str
    ) -> Optional[int]:
        """Return the 1-indexed row number of a matching prior record, if any."""
        records = self.get_all_records()
        for idx, record in enumerate(records, start=2):  # row 1 is header
            if reference_number and record.get("Reference Number") == reference_number:
                return idx
            if (
                not reference_number
                and record.get("Amount") == amount
                and record.get("Date") == slip_date
                and record.get("Time") == slip_time
            ):
                return idx
        return None

    @sync_retry(exceptions=(Exception,))
    def update_row(self, row_number: int, values: dict[str, str]) -> None:
        """Update specific columns of an existing row by header name."""
        for header, value in values.items():
            if header not in SHEET_HEADERS:
                continue
            col = SHEET_HEADERS.index(header) + 1
            self._expenses_ws.update_cell(row_number, col, value)

    @sync_retry(exceptions=(Exception,))
    def delete_row(self, row_number: int) -> None:
        self._expenses_ws.delete_rows(row_number)

    @property
    def spreadsheet_id(self) -> str:
        return self._spreadsheet.id
