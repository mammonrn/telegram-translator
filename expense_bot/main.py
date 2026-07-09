"""Entry point: wires dependencies together and starts the Telegram bot.

Run with `python main.py` from inside this directory (with a virtualenv
that has `requirements.txt` installed and a populated `.env`).
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time as dt_time, timedelta

from telegram import Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from config import Config, load_config
from conversation import SlipConversation
from database import ExpenseDatabase
from drive import DriveManager
from handlers.commands import (
    DELETE_WAIT_CONFIRM,
    DELETE_WAIT_REF,
    EDIT_WAIT_FIELD,
    EDIT_WAIT_REF,
    EDIT_WAIT_VALUE,
    EXPENSE_QUERY_REGEX,
    CommandHandlers,
)
from ocr import OCREngine
from sheet import SheetManager
from utils import setup_logging

logger = logging.getLogger("expense_bot.main")


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Global error handler: log the exception and notify the user politely."""
    logger.error("Unhandled exception while processing update: %s", update, exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "⚠️ Something went wrong on my end. Please try again."
            )
        except Exception:  # noqa: BLE001
            logger.exception("Failed to notify user about error")


def build_application(config: Config) -> Application:
    drive = DriveManager(
        credentials_path=config.google_application_credentials,
        parent_folder_id=config.google_drive_folder_id,
        cache_path=config.folder_cache_path,
    )
    sheets = SheetManager(
        credentials_path=config.google_application_credentials,
        spreadsheet_id=config.spreadsheet_id,
    )
    db = ExpenseDatabase(sheets)
    ocr_engine = OCREngine(tesseract_cmd=config.tesseract_cmd)

    slip_flow = SlipConversation(config, drive, db, ocr_engine)
    commands = CommandHandlers(config, db)

    application = Application.builder().token(config.bot_token).build()
    application.bot_data["drive"] = drive
    application.bot_data["db"] = db
    application.bot_data["config"] = config

    slip_conversation = ConversationHandler(
        entry_points=[MessageHandler(slip_flow.entry_filters(), slip_flow.handle_slip)],
        states=slip_flow.build_states(),
        fallbacks=[CommandHandler("cancel", slip_flow.cancel)],
        name="slip_conversation",
    )

    edit_conversation = ConversationHandler(
        entry_points=[CommandHandler("edit", commands.edit_start)],
        states={
            EDIT_WAIT_REF: [MessageHandler(filters.TEXT & ~filters.COMMAND, commands.edit_receive_ref)],
            EDIT_WAIT_FIELD: [MessageHandler(filters.TEXT & ~filters.COMMAND, commands.edit_receive_field)],
            EDIT_WAIT_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, commands.edit_receive_value)],
        },
        fallbacks=[CommandHandler("cancel", commands.cancel)],
        name="edit_conversation",
    )

    delete_conversation = ConversationHandler(
        entry_points=[CommandHandler("delete", commands.delete_start)],
        states={
            DELETE_WAIT_REF: [MessageHandler(filters.TEXT & ~filters.COMMAND, commands.delete_receive_ref)],
            DELETE_WAIT_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, commands.delete_confirm)],
        },
        fallbacks=[CommandHandler("cancel", commands.cancel)],
        name="delete_conversation",
    )

    # Logs a cash expense with no slip: /cash, /cash <amount> [remark], or
    # simply typing a bare amount (e.g. "150") directly to the bot.
    cash_conversation = ConversationHandler(
        entry_points=[
            CommandHandler("cash", slip_flow.handle_cash_command),
            MessageHandler(slip_flow.cash_entry_filters(), slip_flow.handle_cash_amount_entry),
        ],
        states=slip_flow.build_cash_states(),
        fallbacks=[CommandHandler("cancel", slip_flow.cancel)],
        name="cash_conversation",
    )

    application.add_handler(CommandHandler("start", commands.start))
    application.add_handler(CommandHandler("help", commands.help_cmd))
    application.add_handler(CommandHandler("stats_month", commands.stats_month))
    application.add_handler(CommandHandler("stats_year", commands.stats_year))
    application.add_handler(CommandHandler("export_csv", commands.export_csv))
    application.add_handler(CommandHandler("export_excel", commands.export_excel))
    application.add_handler(CommandHandler("search_category", commands.search_category))
    application.add_handler(CommandHandler("search_date", commands.search_date))
    # Conversations that consume free-text replies must be registered before
    # cash_conversation's broad "any bare number" entry point, so an active
    # edit/delete/slip conversation always gets first refusal on a user's
    # text reply instead of it being misread as a new cash entry.
    application.add_handler(edit_conversation)
    application.add_handler(delete_conversation)
    application.add_handler(slip_conversation)
    application.add_handler(cash_conversation)
    # Free-text "how much have I spent" trigger - registered last so it
    # never intercepts a reply that belongs to an in-progress conversation.
    application.add_handler(MessageHandler(filters.Regex(EXPENSE_QUERY_REGEX), commands.stats_month))
    application.add_error_handler(on_error)

    if application.job_queue is not None:
        _schedule_jobs(application, config, drive, sheets)
    else:
        logger.warning(
            "JobQueue unavailable (install 'python-telegram-bot[job-queue]') - "
            "automatic backups/reports are disabled."
        )

    return application


def _schedule_jobs(application: Application, config: Config, drive: DriveManager, sheets: SheetManager) -> None:
    application.job_queue.run_daily(
        _daily_backup_job,
        time=dt_time(hour=config.daily_backup_hour_utc, minute=0),
        name="daily_backup",
        data={"drive": drive, "sheets": sheets, "config": config},
    )
    # Monthly report: first day of each month at 09:00 UTC, summarizing the
    # previous month.
    application.job_queue.run_monthly(
        _monthly_report_job, when=dt_time(hour=9, minute=0), day=1, name="monthly_report",
    )
    # Yearly report: Jan 1st at 09:30 UTC, summarizing the previous year.
    application.job_queue.run_daily(
        _yearly_report_job,
        time=dt_time(hour=9, minute=30),
        name="yearly_report_check",
    )


async def _daily_backup_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    data = context.job.data
    drive: DriveManager = data["drive"]
    sheets: SheetManager = data["sheets"]
    config: Config = data["config"]
    backup_parent = config.backup_folder_id or drive.get_root_folder_id()
    backup_name = f"Expenses-backup-{date.today().isoformat()}"
    try:
        drive.copy_file(sheets.spreadsheet_id, backup_name, backup_parent)
        logger.info("Daily spreadsheet backup completed: %s", backup_name)
    except Exception:  # noqa: BLE001
        logger.exception("Daily backup failed")


async def _monthly_report_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send the previous month's category totals to every allowed user."""
    config: Config = context.application.bot_data["config"]
    db: ExpenseDatabase = context.application.bot_data["db"]
    if not config.allowed_user_ids:
        return
    today = date.today()
    prev_month_last_day = today.replace(day=1) - timedelta(days=1)
    for user_id in config.allowed_user_ids:
        totals = db.monthly_stats(prev_month_last_day.year, prev_month_last_day.month, user_id)
        if not totals:
            continue
        lines = [f"📅 Monthly report - {prev_month_last_day.strftime('%B %Y')}"]
        total = sum(totals.values())
        for category, amount in sorted(totals.items(), key=lambda kv: kv[1], reverse=True):
            lines.append(f"  {category}: {amount:.2f}")
        lines.append(f"\nTotal: {total:.2f}")
        try:
            await context.bot.send_message(chat_id=user_id, text="\n".join(lines))
        except Exception:  # noqa: BLE001
            logger.exception("Failed to send monthly report to %s", user_id)


async def _yearly_report_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Runs daily but only sends a report on Jan 1st, summarizing last year."""
    today = date.today()
    if today.month != 1 or today.day != 1:
        return
    config: Config = context.application.bot_data["config"]
    db: ExpenseDatabase = context.application.bot_data["db"]
    if not config.allowed_user_ids:
        return
    last_year = today.year - 1
    for user_id in config.allowed_user_ids:
        totals = db.yearly_stats(last_year, user_id)
        if not totals:
            continue
        lines = [f"📆 Yearly report - {last_year}"]
        total = sum(totals.values())
        for category, amount in sorted(totals.items(), key=lambda kv: kv[1], reverse=True):
            lines.append(f"  {category}: {amount:.2f}")
        lines.append(f"\nTotal: {total:.2f}")
        try:
            await context.bot.send_message(chat_id=user_id, text="\n".join(lines))
        except Exception:  # noqa: BLE001
            logger.exception("Failed to send yearly report to %s", user_id)


def main() -> None:
    config = load_config()
    setup_logging(config.log_file, config.log_level)
    logger.info("Starting Expense Tracker Bot")
    application = build_application(config)
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
