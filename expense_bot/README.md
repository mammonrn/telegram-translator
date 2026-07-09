# Telegram Expense Tracker Bot

Send a photo (or PDF) of a bank transfer slip to a Telegram bot and have it:

1. Upload the original slip to Google Drive, organized as `Expenses/<Year>/<Month>/`.
2. Extract the amount, bank, date, time, sender, receiver and reference number with OCR
   (Google Cloud Vision, falling back to Tesseract).
3. Ask you to pick a category via inline buttons and optionally add a remark.
4. Save everything to a Google Sheet, with duplicate detection and an
   auto-generated Summary tab (totals, category breakdown, pie chart, monthly
   trend chart).

## Features

- Thai + English bank slip OCR, automatic bank detection (KBank, SCB, Bangkok
  Bank, Krungthai, Krungsri, TTB, GSB, PromptPay, and more).
- Image and PDF slip support, with automatic image compression before upload.
- Duplicate detection (by reference number, or amount+date+time) with a
  yes/no confirmation before saving a repeat.
- OCR confidence scoring; low-confidence amounts trigger a manual re-entry
  prompt instead of saving bad data.
- **Cash expenses (no slip)**: type a bare amount directly to the bot (e.g.
  "150" or "150 บาท"), or use `/cash [amount] [remark]`, to log a cash
  payment straight to the same category/remark flow — no OCR, no Drive
  upload, no duplicate check.
- **Ask for a summary directly**: type a phrase like "สรุปค่าใช้จ่าย" or
  "ค่าใช้จ่ายเดือนนี้เท่าไหร่" (or in English, "expense summary") and the bot
  replies with this month's total plus each category's amount and % share —
  no command needed, same output as `/stats_month`.
- Google Drive folder-ID caching to minimize API calls.
- Multi-user support, scoped by Telegram user ID (`ALLOWED_USER_IDS`).
- `/cash`, `/stats_month`, `/stats_year`, `/export_csv`, `/export_excel`,
  `/search_category`, `/search_date`, `/edit`, `/delete`.
- Daily automatic Google Sheet backup (Drive file copy), plus automatic
  monthly (1st of month) and yearly (Jan 1st) reports sent to each user.
- Retry with exponential backoff around Google/Telegram API calls.
- Unit tests for the parsing, dedup, and stats logic (no live credentials
  required to run them).

## Project Structure

```
expense_bot/
  main.py            # entry point, dependency wiring, job scheduling
  config.py          # env-based configuration, categories, sheet schema
  utils.py           # logging, retry decorator, image/date/amount helpers
  ocr.py             # Vision/Tesseract OCR + Thai/English slip parsing
  drive.py           # Drive folder management, caching, uploads, backups
  sheet.py           # Google Sheets Expenses + Summary worksheet management
  database.py        # domain layer: records, dedup, search, stats, export
  conversation.py    # Telegram ConversationHandler: slip -> category -> save
  handlers/
    commands.py      # /start /help /stats_* /export_* /search_* /edit /delete
  tests/             # pytest unit tests
  deploy/
    expense-bot.service  # systemd unit
  requirements.txt
  .env.example
```

## 1. Google Cloud setup

1. Create (or reuse) a Google Cloud project at https://console.cloud.google.com.
2. Enable APIs: **Google Drive API**, **Google Sheets API**, and (optionally,
   for better OCR) **Cloud Vision API**.
3. Create a **Service Account**: IAM & Admin → Service Accounts → Create
   Service Account. No project roles are required — access is granted by
   sharing the Drive folder/Sheet directly with the service account.
4. Create a JSON key for the service account and download it. This is your
   `GOOGLE_APPLICATION_CREDENTIALS` file.
5. **Share Google Drive**: create (or pick) a parent folder in Drive, share
   it with the service account's email (found in the JSON key, e.g.
   `xxx@yyy.iam.gserviceaccount.com`) with **Editor** access. Copy the
   folder ID from its URL into `GOOGLE_DRIVE_FOLDER_ID`.
6. **Share the Google Sheet**: create a new spreadsheet, share it with the
   same service account email as **Editor**. Copy the spreadsheet ID from
   its URL into `SPREADSHEET_ID`. The bot creates the `Expenses` and
   `Summary` worksheets automatically on first run.
7. If Vision OCR isn't enabled/available, the bot automatically falls back
   to Tesseract — no extra Google setup is required for that path, but you
   do need Tesseract installed on the host (see below).

## 2. Telegram Bot setup

1. Talk to [@BotFather](https://t.me/BotFather), run `/newbot`, and copy the
   token into `BOT_TOKEN`.
2. Get your numeric Telegram user ID (e.g. via [@userinfobot](https://t.me/userinfobot))
   and put it in `ALLOWED_USER_IDS` (comma-separated for multiple users).
   Leave empty to allow anyone to use the bot.

## 3. Local installation

```bash
cd expense_bot
python3.12 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# System dependency for the Tesseract OCR fallback:
#   Ubuntu/Debian: sudo apt-get install tesseract-ocr tesseract-ocr-tha poppler-utils
#   (poppler-utils provides pdftoppm, needed by pdf2image for PDF slips)

cp .env.example .env
# edit .env: BOT_TOKEN, GOOGLE_APPLICATION_CREDENTIALS, GOOGLE_DRIVE_FOLDER_ID,
# SPREADSHEET_ID, ALLOWED_USER_IDS

python main.py
```

## 4. Running tests

```bash
pip install pytest
pytest
```

Tests cover amount/date parsing, Thai/English slip OCR parsing, and the
duplicate-detection/search/stats logic against an in-memory fake sheet —
no Google or Telegram credentials needed.

## 5. Deploy on Ubuntu 24.04 (systemd)

```bash
sudo useradd --system --home /opt/expense_bot --shell /usr/sbin/nologin expensebot
sudo mkdir -p /opt/expense_bot /var/log/expense_bot
sudo chown -R expensebot:expensebot /opt/expense_bot /var/log/expense_bot

# Copy the project (including .env and your service-account JSON key) to
# /opt/expense_bot, then as the expensebot user:
sudo -u expensebot python3.12 -m venv /opt/expense_bot/venv
sudo -u expensebot /opt/expense_bot/venv/bin/pip install -r /opt/expense_bot/requirements.txt

sudo cp /opt/expense_bot/deploy/expense-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now expense-bot
sudo systemctl status expense-bot
journalctl -u expense-bot -f
```

The unit sets `Restart=always` so the bot auto-restarts on crash or reboot.

## 6. Backups

In addition to the daily automatic Google Sheet backup (a dated copy in
Drive, configured via `DAILY_BACKUP_HOUR_UTC` / `GOOGLE_DRIVE_BACKUP_FOLDER_ID`),
it's good practice to also periodically back up:

- The `.env` file and service-account JSON key (store securely, never in git).
- `.drive_folder_cache.json` (safe to delete — it will be rebuilt from Drive
  on next use, just costs a few extra API calls).

## Conversation Flow

```
User sends slip photo/PDF
  -> bot uploads original to Drive, runs OCR
  -> if amount unclear: bot asks user to type it
  -> if duplicate found: bot asks "Save again? Yes/No"
  -> bot shows extracted info + category buttons
  -> bot asks "Add a remark?" (Skip / Type Remark)
  -> bot saves to Google Sheets and replies "✅ Expense Recorded Successfully"
```

Cash expense (no slip) — skips OCR, Drive upload, and duplicate check:

```
User types "150" (or "/cash 150 coffee") directly to the bot
  -> bot shows category buttons
  -> bot asks "Add a remark?" (Skip / Type Remark), unless a remark was
     already given as part of "/cash <amount> <remark>"
  -> bot saves to Google Sheets and replies "✅ Expense Recorded Successfully"
```

Instant expense summary — no command needed:

```
User types "สรุปค่าใช้จ่าย" / "ค่าใช้จ่ายเดือนนี้เท่าไหร่" / "expense summary"
  -> bot replies with this month's total plus each category's
     amount and % share of the total (same as /stats_month)
```

## Categories

Food, Accommodation, Transportation, Entertainment, Education, Donation,
Shopping, Bills, Healthcare, Investment, Family, Business, Other.

## Notes on the Sheet schema

The `Expenses` worksheet has one extra column beyond the original spec —
**User ID** — appended at the end. It's required to scope multi-user
search/stats/export correctly and is additive, so it doesn't break any
formula or column expecting the original layout.
