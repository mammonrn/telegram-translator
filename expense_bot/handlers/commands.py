"""Command handlers: stats, export, search, edit, delete, help.

Grouped into a `CommandHandlers` class so dependencies (config, database)
are injected once in `main.py` rather than pulled from globals.
"""

from __future__ import annotations

import io
import logging
import re
from datetime import date, datetime
from decimal import Decimal

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes, ConversationHandler, MessageHandler, filters

from config import CATEGORIES, Config
from database import ExpenseDatabase

logger = logging.getLogger("expense_bot.commands")

# States for the /edit and /delete conversational flows.
EDIT_WAIT_REF, EDIT_WAIT_FIELD, EDIT_WAIT_VALUE = range(3)
DELETE_WAIT_REF, DELETE_WAIT_CONFIRM = range(3, 5)

EDITABLE_FIELDS = ["Amount", "Category", "Remark", "Bank", "Sender", "Receiver"]

# Matches free-text messages asking for an expense summary, so users can
# just type it directly to the bot instead of remembering /stats_month.
EXPENSE_QUERY_REGEX = re.compile(
    r"(สรุปค่าใช้จ่าย|ค่าใช้จ่ายเดือนนี้|ยอดใช้จ่าย|รายจ่ายเดือนนี้|ใช้เงินไปเท่าไหร่|"
    r"ใช้จ่ายไปเท่าไหร่|เดือนนี้ใช้ไป|expense\s*summary|spending\s*this\s*month|how\s*much.*spent)",
    re.IGNORECASE,
)


class CommandHandlers:
    def __init__(self, config: Config, db: ExpenseDatabase) -> None:
        self._config = config
        self._db = db

    def _authorized(self, user_id: int) -> bool:
        if not self._config.allowed_user_ids:
            return True
        return user_id in self._config.allowed_user_ids

    async def _guard(self, update: Update) -> bool:
        user = update.effective_user
        if user is None or not self._authorized(user.id):
            await update.effective_message.reply_text("You're not authorized to use this bot.")
            return False
        return True

    # -- basic --------------------------------------------------------------

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        await update.effective_message.reply_text(
            "👋 Send me a photo or PDF of a bank transfer slip and I'll record it as an expense.\n\n"
            "Paid with cash instead? Just type the amount (e.g. \"150\") or use /cash.\n"
            "Want to know how much you've spent this month? Just ask, e.g. \"สรุปค่าใช้จ่าย\".\n\n"
            "Type /help to see everything I can do."
        )

    async def help_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        text = (
            "*Expense Tracker Bot*\n\n"
            "📷 Send a slip photo/PDF to record an expense.\n"
            "💵 No slip? Type a bare amount (e.g. \"150\") or use /cash `<amount>` `[remark]` "
            "to log a cash expense directly.\n"
            "🗣 Ask directly, e.g. \"สรุปค่าใช้จ่าย\" / \"ค่าใช้จ่ายเดือนนี้เท่าไหร่\", "
            "and I'll reply with this month's totals and category breakdown.\n\n"
            "*Commands*\n"
            "/cash `[amount]` `[remark]` - log a cash expense (no slip)\n"
            "/stats\\_month - this month's totals by category, with %\n"
            "/stats\\_year - this year's totals by category, with %\n"
            "/export\\_csv - export your records as CSV\n"
            "/export\\_excel - export your records as Excel\n"
            "/search\\_category `<category>` - list expenses in a category\n"
            "/search\\_date `<YYYY-MM-DD> <YYYY-MM-DD>` - list expenses in a date range\n"
            "/edit - edit a saved record by reference number\n"
            "/delete - delete a saved record by reference number\n"
            "/cancel - cancel the current action\n"
        )
        await update.effective_message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

    async def cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        context.user_data.clear()
        await update.effective_message.reply_text("Cancelled.")
        return ConversationHandler.END

    # -- stats --------------------------------------------------------------

    async def stats_month(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        today = date.today()
        totals = self._db.monthly_stats(today.year, today.month, update.effective_user.id)
        await update.effective_message.reply_text(_format_totals(f"{today.strftime('%B %Y')}", totals))

    async def stats_year(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        today = date.today()
        totals = self._db.yearly_stats(today.year, update.effective_user.id)
        await update.effective_message.reply_text(_format_totals(str(today.year), totals))

    # -- export --------------------------------------------------------------

    async def export_csv(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        data = self._db.export_csv(update.effective_user.id)
        await update.effective_message.reply_document(
            document=io.BytesIO(data), filename="expenses.csv", caption="📄 Your expense export (CSV)"
        )

    async def export_excel(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        data = self._db.export_excel(update.effective_user.id)
        await update.effective_message.reply_document(
            document=io.BytesIO(data), filename="expenses.xlsx", caption="📊 Your expense export (Excel)"
        )

    # -- search --------------------------------------------------------------

    async def search_category(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        if not context.args:
            valid = ", ".join(label for _, label in CATEGORIES.values())
            await update.effective_message.reply_text(
                f"Usage: /search_category <category>\nValid categories: {valid}"
            )
            return
        category = " ".join(context.args)
        results = self._db.search_by_category(category, update.effective_user.id)
        await update.effective_message.reply_text(_format_records(results, f"Category: {category}"))

    async def search_date(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        if len(context.args) != 2:
            await update.effective_message.reply_text(
                "Usage: /search_date <YYYY-MM-DD> <YYYY-MM-DD>"
            )
            return
        try:
            start = datetime.strptime(context.args[0], "%Y-%m-%d").date()
            end = datetime.strptime(context.args[1], "%Y-%m-%d").date()
        except ValueError:
            await update.effective_message.reply_text("Dates must be in YYYY-MM-DD format.")
            return
        results = self._db.search_by_date(start, end, update.effective_user.id)
        await update.effective_message.reply_text(
            _format_records(results, f"{start.isoformat()} to {end.isoformat()}")
        )

    # -- edit (conversation) -----------------------------------------------

    async def edit_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        if not await self._guard(update):
            return ConversationHandler.END
        await update.effective_message.reply_text(
            "Please send the Reference Number of the record you want to edit."
        )
        return EDIT_WAIT_REF

    async def edit_receive_ref(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        ref = (update.effective_message.text or "").strip()
        row = self._find_row_by_reference(ref, update.effective_user.id)
        if row is None:
            await update.effective_message.reply_text("No record found with that reference number.")
            return ConversationHandler.END
        context.user_data["edit_row"] = row
        await update.effective_message.reply_text(
            "Which field would you like to edit?\n" + ", ".join(EDITABLE_FIELDS)
        )
        return EDIT_WAIT_FIELD

    async def edit_receive_field(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        field_name = (update.effective_message.text or "").strip()
        matches = [f for f in EDITABLE_FIELDS if f.lower() == field_name.lower()]
        if not matches:
            await update.effective_message.reply_text(
                "Not an editable field. Choose one of: " + ", ".join(EDITABLE_FIELDS)
            )
            return EDIT_WAIT_FIELD
        context.user_data["edit_field"] = matches[0]
        await update.effective_message.reply_text(f"New value for {matches[0]}?")
        return EDIT_WAIT_VALUE

    async def edit_receive_value(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        value = (update.effective_message.text or "").strip()
        field_name = context.user_data.get("edit_field")
        row = context.user_data.get("edit_row")
        if field_name == "Amount":
            try:
                Decimal(value)
            except Exception:  # noqa: BLE001
                await update.effective_message.reply_text("Amount must be numeric, try again.")
                return EDIT_WAIT_VALUE
        self._db.edit_field(row, {field_name: value})
        await update.effective_message.reply_text(f"✅ Updated {field_name}.")
        context.user_data.pop("edit_row", None)
        context.user_data.pop("edit_field", None)
        return ConversationHandler.END

    # -- delete (conversation) -----------------------------------------------

    async def delete_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        if not await self._guard(update):
            return ConversationHandler.END
        await update.effective_message.reply_text(
            "Please send the Reference Number of the record you want to delete."
        )
        return DELETE_WAIT_REF

    async def delete_receive_ref(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        ref = (update.effective_message.text or "").strip()
        row = self._find_row_by_reference(ref, update.effective_user.id)
        if row is None:
            await update.effective_message.reply_text("No record found with that reference number.")
            return ConversationHandler.END
        context.user_data["delete_row"] = row
        await update.effective_message.reply_text(
            f"Delete record at row {row}? Reply 'yes' to confirm or 'no' to cancel."
        )
        return DELETE_WAIT_CONFIRM

    async def delete_confirm(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        answer = (update.effective_message.text or "").strip().lower()
        row = context.user_data.pop("delete_row", None)
        if answer not in ("yes", "y") or row is None:
            await update.effective_message.reply_text("Not deleted.")
            return ConversationHandler.END
        self._db.delete(row)
        await update.effective_message.reply_text("🗑 Record deleted.")
        return ConversationHandler.END

    def _find_row_by_reference(self, reference_number: str, user_id: int) -> int | None:
        records = self._db.all_records(user_id)
        for idx, record in enumerate(records, start=2):  # header is row 1
            if record.get("Reference Number") == reference_number:
                return idx
        return None


def _format_totals(period_label: str, totals: dict[str, Decimal]) -> str:
    """Render a total + per-category breakdown with each category's share (%)."""
    if not totals:
        return f"ไม่มีรายการค่าใช้จ่ายในช่วง {period_label}"
    grand_total: Decimal = sum(totals.values())
    lines = [f"📊 สรุปค่าใช้จ่าย - {period_label}", ""]
    for category, amount in sorted(totals.items(), key=lambda kv: kv[1], reverse=True):
        pct = (amount / grand_total * 100) if grand_total else Decimal("0")
        lines.append(f"  • {category}: {amount:,.2f} บาท ({pct:.1f}%)")
    lines.append("")
    lines.append(f"รวมทั้งหมด: {grand_total:,.2f} บาท")
    return "\n".join(lines)


def _format_records(records: list[dict[str, str]], label: str, limit: int = 20) -> str:
    if not records:
        return f"No expenses found for {label}."
    lines = [f"🔍 {len(records)} result(s) for {label}:"]
    for r in records[:limit]:
        lines.append(
            f"  {r.get('Date')} {r.get('Time')} - {r.get('Amount')} "
            f"({r.get('Category')}) ref:{r.get('Reference Number') or '-'}"
        )
    if len(records) > limit:
        lines.append(f"  ...and {len(records) - limit} more")
    return "\n".join(lines)
