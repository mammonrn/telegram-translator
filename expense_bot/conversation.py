"""The core Telegram conversation: slip photo/PDF -> OCR -> category -> remark -> save.

Two entry points feed the same category/remark/save pipeline:

Slip flow:
    1. User sends a photo or PDF document of a transfer slip.
    2. Bot downloads + compresses it, uploads the original to Drive, runs OCR.
    3. If OCR isn't confident about the amount, ask the user to type it.
    4. Duplicate check (by reference number, else amount+date+time). If a
       duplicate is found, ask for confirmation before saving again.
    5. Show extracted info and ask for a category via inline keyboard.
    6. Ask for an optional remark.
    7. Save to Google Sheets and confirm.

Cash flow (no slip):
    1. User runs /cash (optionally with an amount, e.g. "/cash 150 coffee"),
       or simply types a bare amount (e.g. "150") directly to the bot.
    2. Bot skips OCR/Drive/duplicate-check and goes straight to step 5 above.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date as date_cls, datetime, time as time_cls
from decimal import Decimal, InvalidOperation
from typing import Optional

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from config import CATEGORIES, Config
from database import ExpenseDatabase, ExpenseRecord
from drive import DriveManager
from ocr import OCREngine, OCRResult, parse_slip_text
from utils import MONTH_NAMES, compress_image, parse_amount

logger = logging.getLogger("expense_bot.conversation")

# Conversation states (slip flow)
WAITING_AMOUNT_CORRECTION, WAITING_DUPLICATE_CONFIRM, WAITING_CATEGORY, WAITING_REMARK_CHOICE, WAITING_REMARK_TEXT = range(5)

# Conversation states (cash flow) - a separate ConversationHandler instance,
# so reusing the shared WAITING_CATEGORY/WAITING_REMARK_* values below is safe.
CASH_WAITING_AMOUNT = 100

CB_CATEGORY_PREFIX = "cat:"
CB_REMARK_SKIP = "remark:skip"
CB_REMARK_TYPE = "remark:type"
CB_DUP_YES = "dup:yes"
CB_DUP_NO = "dup:no"

PENDING_KEY = "pending_expense"

# Matches a bare cash amount typed directly to the bot, e.g. "150",
# "150.50", "1,200", "150 บาท", "฿150". Used as a conversation entry point
# for logging a cash expense without a slip.
CASH_AMOUNT_REGEX = re.compile(
    r"^\s*฿?\s*\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?\s*(?:บาท|บ\.|thb|baht)?\s*$", re.IGNORECASE
)


@dataclass
class PendingExpense:
    """State accumulated across the conversation for a single slip or cash entry."""

    amount: Optional[Decimal]
    bank: str
    slip_date: date_cls
    slip_time: time_cls
    sender: str
    receiver: str
    reference_number: str
    drive_url: str
    telegram_file_id: str
    ocr_confidence: float
    category: Optional[str] = None
    remark: str = ""
    remark_prefilled: bool = False


class SlipConversation:
    """Bundles the dependencies needed by the conversation's callbacks.

    Instantiated once in `main.py` and its bound methods registered as
    python-telegram-bot handlers (dependency injection instead of globals).
    """

    def __init__(
        self,
        config: Config,
        drive: DriveManager,
        db: ExpenseDatabase,
        ocr_engine: OCREngine,
    ) -> None:
        self._config = config
        self._drive = drive
        self._db = db
        self._ocr = ocr_engine

    def is_authorized(self, user_id: int) -> bool:
        if not self._config.allowed_user_ids:
            return True
        return user_id in self._config.allowed_user_ids

    # -- entry point: photo or PDF document ---------------------------------

    async def handle_slip(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        user = update.effective_user
        message = update.effective_message
        if user is None or message is None:
            return ConversationHandler.END

        if not self.is_authorized(user.id):
            await message.reply_text("You're not authorized to use this bot.")
            return ConversationHandler.END

        try:
            file_bytes, filename, mime_type, is_pdf = await self._download_slip(message, context)
        except ValueError as exc:
            await message.reply_text(str(exc))
            return ConversationHandler.END
        except Exception:  # noqa: BLE001
            logger.exception("Failed to download slip")
            await message.reply_text(
                "⚠️ I couldn't download that file. Please try sending it again."
            )
            return ConversationHandler.END

        telegram_file_id = self._extract_file_id(message)

        processing_msg = await message.reply_text("🔎 Reading your slip, one moment...")

        try:
            ocr_result = await self._run_ocr(file_bytes, is_pdf)
        except Exception:  # noqa: BLE001
            logger.exception("OCR failed")
            await processing_msg.edit_text(
                "⚠️ OCR failed for this slip. Please type the amount manually (e.g. 1250.00)."
            )
            context.user_data[PENDING_KEY] = PendingExpense(
                amount=None, bank="Unknown", slip_date=date_cls.today(), slip_time=time_cls(0, 0),
                sender="", receiver="", reference_number="",
                drive_url="", telegram_file_id=telegram_file_id, ocr_confidence=0.0,
            )
            # Upload still happens so the slip isn't lost even if OCR failed.
            await self._upload_and_stash_url(file_bytes, filename, mime_type, context)
            return WAITING_AMOUNT_CORRECTION

        upload_url = await self._upload_and_stash_url(
            file_bytes, filename, mime_type, context, ocr_result=ocr_result
        )

        pending = PendingExpense(
            amount=ocr_result.amount,
            bank=ocr_result.bank or "Unknown",
            slip_date=ocr_result.slip_date or date_cls.today(),
            slip_time=ocr_result.slip_time or time_cls(0, 0),
            sender=ocr_result.sender or "",
            receiver=ocr_result.receiver or "",
            reference_number=ocr_result.reference_number or "",
            drive_url=upload_url,
            telegram_file_id=telegram_file_id,
            ocr_confidence=ocr_result.confidence,
        )
        context.user_data[PENDING_KEY] = pending

        if pending.amount is None or ocr_result.confidence < self._config.ocr_confidence_threshold:
            await processing_msg.edit_text(
                "🤔 I couldn't read the amount clearly.\nPlease type the correct amount."
            )
            return WAITING_AMOUNT_CORRECTION

        await processing_msg.delete()
        return await self._after_amount_known(update, context)

    async def _download_slip(self, message, context) -> tuple[bytes, str, str, bool]:
        if message.photo:
            photo = message.photo[-1]
            tg_file = await context.bot.get_file(photo.file_id)
            raw = bytes(await tg_file.download_as_bytearray())
            return compress_image(raw), f"slip_{photo.file_unique_id}.jpg", "image/jpeg", False

        if message.document:
            doc = message.document
            mime = doc.mime_type or ""
            if mime == "application/pdf":
                tg_file = await context.bot.get_file(doc.file_id)
                raw = bytes(await tg_file.download_as_bytearray())
                return raw, doc.file_name or f"slip_{doc.file_unique_id}.pdf", "application/pdf", True
            if mime.startswith("image/"):
                tg_file = await context.bot.get_file(doc.file_id)
                raw = bytes(await tg_file.download_as_bytearray())
                return compress_image(raw), doc.file_name or f"slip_{doc.file_unique_id}.jpg", "image/jpeg", False
            raise ValueError("Please send the slip as an image or PDF file.")

        raise ValueError("Please send a photo or PDF of your transfer slip.")

    @staticmethod
    def _extract_file_id(message) -> str:
        if message.photo:
            return message.photo[-1].file_id
        if message.document:
            return message.document.file_id
        return ""

    async def _run_ocr(self, file_bytes: bytes, is_pdf: bool) -> OCRResult:
        text, engine_confidence, engine_name = self._ocr.extract_text(file_bytes, is_pdf=is_pdf)
        result = parse_slip_text(text, base_confidence=engine_confidence)
        result.engine = engine_name
        return result

    async def _upload_and_stash_url(
        self, file_bytes: bytes, filename: str, mime_type: str, context, ocr_result: Optional[OCRResult] = None
    ) -> str:
        slip_date = ocr_result.slip_date if ocr_result else None
        target_date = slip_date or date_cls.today()
        month_name = MONTH_NAMES[target_date.month - 1]
        try:
            _, url = self._drive.upload_slip(
                file_bytes, filename, target_date.year, month_name, mime_type
            )
            return url
        except Exception:  # noqa: BLE001
            logger.exception("Drive upload failed")
            return ""

    # -- amount correction ---------------------------------------------------

    async def handle_amount_correction(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        pending: PendingExpense = context.user_data.get(PENDING_KEY)
        message = update.effective_message
        if pending is None:
            await message.reply_text("Session expired, please resend the slip.")
            return ConversationHandler.END

        amount = parse_amount(message.text or "")
        if amount is None:
            await message.reply_text("That doesn't look like a valid amount. Please try again, e.g. 1250.00")
            return WAITING_AMOUNT_CORRECTION

        pending.amount = amount
        pending.ocr_confidence = max(pending.ocr_confidence, 0.99)  # user-confirmed
        return await self._after_amount_known(update, context)

    async def _after_amount_known(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        pending: PendingExpense = context.user_data[PENDING_KEY]
        message = update.effective_message

        dup_row = self._db.find_duplicate_row(
            ExpenseRecord(
                date=pending.slip_date,
                time_str=pending.slip_time.strftime("%H:%M:%S"),
                amount=pending.amount,
                bank=pending.bank,
                sender=pending.sender,
                receiver=pending.receiver,
                reference_number=pending.reference_number,
                category="",
                remark="",
                drive_url=pending.drive_url,
                telegram_file_id=pending.telegram_file_id,
                ocr_confidence=pending.ocr_confidence,
                user_id=update.effective_user.id,
            )
        )
        if dup_row:
            keyboard = InlineKeyboardMarkup(
                [[InlineKeyboardButton("Yes", callback_data=CB_DUP_YES),
                  InlineKeyboardButton("No", callback_data=CB_DUP_NO)]]
            )
            await message.reply_text(
                "⚠️ This slip already exists.\nSave again?", reply_markup=keyboard
            )
            return WAITING_DUPLICATE_CONFIRM

        return await self._ask_category(message, pending)

    async def _ask_category(self, message, pending: PendingExpense) -> int:
        summary = (
            "I found the following information.\n\n"
            f"Amount: {pending.amount if pending.amount is not None else 'Unknown'}\n"
            f"Bank: {pending.bank}\n"
            f"Date: {pending.slip_date.isoformat()}\n"
            f"Time: {pending.slip_time.strftime('%H:%M')}\n\n"
            "Please choose the expense category."
        )
        await message.reply_text(summary, reply_markup=_category_keyboard())
        return WAITING_CATEGORY

    # -- duplicate confirmation ------------------------------------------------

    async def handle_duplicate_confirm(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        await query.answer()
        pending: PendingExpense = context.user_data.get(PENDING_KEY)
        if pending is None:
            await query.edit_message_text("Session expired, please resend the slip.")
            return ConversationHandler.END

        if query.data == CB_DUP_NO:
            context.user_data.pop(PENDING_KEY, None)
            await query.edit_message_text("Okay, not saved.")
            return ConversationHandler.END

        return await self._ask_category(query.message, pending)

    # -- category selection ---------------------------------------------------

    async def handle_category(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        await query.answer()
        pending: PendingExpense = context.user_data.get(PENDING_KEY)
        if pending is None:
            await query.edit_message_text("Session expired, please resend the slip.")
            return ConversationHandler.END

        category_key = query.data.removeprefix(CB_CATEGORY_PREFIX)
        emoji, label = CATEGORIES.get(category_key, ("📦", "Other"))
        pending.category = label

        if pending.remark_prefilled:
            return await self._finalize(update, context, remark=pending.remark, message=query.message, edit=True)

        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("Skip", callback_data=CB_REMARK_SKIP),
              InlineKeyboardButton("Type Remark", callback_data=CB_REMARK_TYPE)]]
        )
        await query.edit_message_text(
            f"Category set to {emoji} {label}.\n\nWould you like to add a remark?",
            reply_markup=keyboard,
        )
        return WAITING_REMARK_CHOICE

    # -- remark ------------------------------------------------------------

    async def handle_remark_choice(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        await query.answer()

        if query.data == CB_REMARK_SKIP:
            return await self._finalize(update, context, remark="", message=query.message, edit=True)

        await query.edit_message_text("Please type your remark:")
        return WAITING_REMARK_TEXT

    async def handle_remark_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        message = update.effective_message
        return await self._finalize(update, context, remark=message.text or "", message=message, edit=False)

    # -- save ------------------------------------------------------------------

    async def _finalize(self, update: Update, context: ContextTypes.DEFAULT_TYPE, remark: str, message, edit: bool) -> int:
        pending: PendingExpense = context.user_data.get(PENDING_KEY)
        user = update.effective_user
        if pending is None or pending.category is None:
            text = "Session expired, please resend the slip."
            await (message.edit_text(text) if edit else message.reply_text(text))
            return ConversationHandler.END

        record = ExpenseRecord(
            date=pending.slip_date,
            time_str=pending.slip_time.strftime("%H:%M:%S"),
            amount=pending.amount or Decimal("0"),
            bank=pending.bank,
            sender=pending.sender,
            receiver=pending.receiver,
            reference_number=pending.reference_number,
            category=pending.category,
            remark=remark,
            drive_url=pending.drive_url,
            telegram_file_id=pending.telegram_file_id,
            ocr_confidence=pending.ocr_confidence,
            user_id=user.id,
        )

        try:
            self._db.save(record)
        except Exception:  # noqa: BLE001
            logger.exception("Failed to save expense record")
            text = "⚠️ I couldn't save this to Google Sheets. Please try again in a moment."
            await (message.edit_text(text) if edit else message.reply_text(text))
            return ConversationHandler.END

        context.user_data.pop(PENDING_KEY, None)
        confirmation = (
            "✅ Expense Recorded Successfully\n\n"
            f"Amount: {record.amount:.2f}\n"
            f"Category: {record.category}\n"
            f"Date: {record.date.isoformat()}"
        )
        if edit:
            await message.edit_text(confirmation)
        else:
            await message.reply_text(confirmation)
        return ConversationHandler.END

    # -- cancel ------------------------------------------------------------

    async def cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        context.user_data.pop(PENDING_KEY, None)
        await update.effective_message.reply_text("Cancelled.")
        return ConversationHandler.END

    # -- cash expense entry (no slip) -----------------------------------------
    #
    # Reuses the same PendingExpense / category / remark / save pipeline as
    # the slip flow, just skipping OCR, Drive upload, and duplicate checking
    # (a cash entry has no reference number to dedup against).

    async def handle_cash_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        user = update.effective_user
        message = update.effective_message
        if not self.is_authorized(user.id):
            await message.reply_text("You're not authorized to use this bot.")
            return ConversationHandler.END

        if context.args:
            amount = parse_amount(context.args[0])
            if amount is not None:
                remark = " ".join(context.args[1:]).strip()
                return await self._start_cash_pending(update, context, amount, remark)
            await message.reply_text(
                "ไม่พบจำนวนเงินที่ถูกต้อง กรุณาลองใหม่ เช่น /cash 150 ค่ากาแฟ"
            )

        await message.reply_text("💵 กรุณาพิมพ์จำนวนเงินสดที่จ่าย (เช่น 150 หรือ 150.50)")
        return CASH_WAITING_AMOUNT

    async def handle_cash_amount_entry(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Entry point: user typed a bare amount directly, with no /cash command."""
        user = update.effective_user
        message = update.effective_message
        if not self.is_authorized(user.id):
            return ConversationHandler.END

        amount = parse_amount(message.text or "")
        if amount is None:
            return ConversationHandler.END
        return await self._start_cash_pending(update, context, amount, remark="")

    async def handle_cash_amount_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Amount reply after a bare `/cash` prompt."""
        message = update.effective_message
        amount = parse_amount(message.text or "")
        if amount is None:
            await message.reply_text("จำนวนเงินไม่ถูกต้อง กรุณาลองใหม่ เช่น 150 หรือ 150.50")
            return CASH_WAITING_AMOUNT
        return await self._start_cash_pending(update, context, amount, remark="")

    async def _start_cash_pending(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, amount: Decimal, remark: str
    ) -> int:
        now = datetime.now()
        user = update.effective_user
        pending = PendingExpense(
            amount=amount,
            bank="Cash (No Slip)",
            slip_date=now.date(),
            slip_time=now.time().replace(microsecond=0),
            sender=user.full_name or "",
            receiver="",
            reference_number="",
            drive_url="",
            telegram_file_id="",
            ocr_confidence=1.0,  # manually entered, fully trusted
            remark=remark,
            remark_prefilled=bool(remark),
        )
        context.user_data[PENDING_KEY] = pending
        return await self._ask_category(update.effective_message, pending)

    # -- handler registration ------------------------------------------------

    def entry_filters(self) -> filters.BaseFilter:
        return filters.PHOTO | filters.Document.IMAGE | filters.Document.PDF

    def cash_entry_filters(self) -> filters.BaseFilter:
        return filters.Regex(CASH_AMOUNT_REGEX)

    def build_states(self) -> dict[int, list]:
        return {
            WAITING_AMOUNT_CORRECTION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_amount_correction)
            ],
            WAITING_DUPLICATE_CONFIRM: [
                CallbackQueryHandler(self.handle_duplicate_confirm, pattern=r"^dup:")
            ],
            WAITING_CATEGORY: [
                CallbackQueryHandler(self.handle_category, pattern=f"^{CB_CATEGORY_PREFIX}")
            ],
            WAITING_REMARK_CHOICE: [
                CallbackQueryHandler(self.handle_remark_choice, pattern=r"^remark:")
            ],
            WAITING_REMARK_TEXT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_remark_text)
            ],
        }

    def build_cash_states(self) -> dict[int, list]:
        return {
            CASH_WAITING_AMOUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_cash_amount_text)
            ],
            WAITING_CATEGORY: [
                CallbackQueryHandler(self.handle_category, pattern=f"^{CB_CATEGORY_PREFIX}")
            ],
            WAITING_REMARK_CHOICE: [
                CallbackQueryHandler(self.handle_remark_choice, pattern=r"^remark:")
            ],
            WAITING_REMARK_TEXT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_remark_text)
            ],
        }


def _category_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(f"{emoji} {label}", callback_data=f"{CB_CATEGORY_PREFIX}{key}")
        for key, (emoji, label) in CATEGORIES.items()
    ]
    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    return InlineKeyboardMarkup(rows)
