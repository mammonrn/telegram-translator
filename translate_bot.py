"""
Telegram Group Translator Bot
------------------------------
ดึงข้อความใหม่ในกลุ่ม Telegram (จีน/อังกฤษ/พม่า) แล้วแปลเป็นไทยด้วย Claude API
จากนั้นส่งคำแปลกลับเข้ากลุ่มเดิม

ออกแบบมาให้รันแบบ "ครั้งเดียวจบ" (one-shot) เหมาะกับ GitHub Actions
ที่ตั้งเวลารันเป็นช่วงๆ (cron) ไม่ใช่โปรแกรมที่รันค้างตลอดเวลา

State (last_update_id) จะถูกเก็บไว้ในไฟล์ state.json แล้ว commit กลับเข้า repo
เพื่อให้รอบถัดไปรู้ว่าอ่านข้อความไปถึงไหนแล้ว (ไม่แปลซ้ำ)
"""

import os
import json
import sys
import time
import urllib.request
import urllib.error

# ---------- ค่าคงที่ / environment variables ----------
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]       # token ของ "บอทแปล" (thai translation bot)
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]           # chat id ของกลุ่ม (ค่าติดลบ เช่น -5543368117)
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]

STATE_FILE = "state.json"
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
ANTHROPIC_API = "https://api.anthropic.com/v1/messages"
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")

# ป้องกันลูป: ต้องรู้ id ของบอทแปลตัวเอง เพื่อไม่แปลข้อความที่ตัวเองส่ง
ME = None


def http_post_json(url, payload, headers=None, timeout=30):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    if headers:
        for k, v in headers.items():
            req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def http_get_json(url, timeout=30):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"last_update_id": 0}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def get_me():
    global ME
    if ME is None:
        result = http_get_json(f"{TELEGRAM_API}/getMe")
        ME = result["result"]["id"]
    return ME


def get_updates(offset):
    url = f"{TELEGRAM_API}/getUpdates?offset={offset}&timeout=0"
    result = http_get_json(url)
    return result.get("result", [])


def send_message(chat_id, text, reply_to_message_id=None):
    payload = {
        "chat_id": chat_id,
        "text": text,
    }
    if reply_to_message_id:
        payload["reply_to_message_id"] = reply_to_message_id
    return http_post_json(f"{TELEGRAM_API}/sendMessage", payload)


def looks_like_thai_only(text):
    """เดาแบบง่ายๆ ว่าข้อความนี้เป็นภาษาไทยอยู่แล้วหรือเปล่า (เพื่อข้าม ไม่แปลซ้ำ)"""
    thai_chars = sum(1 for ch in text if "\u0e00" <= ch <= "\u0e7f")
    letters = sum(1 for ch in text if ch.isalpha())
    if letters == 0:
        return False
    return thai_chars / letters > 0.5


def translate_to_thai(text):
    """เรียก Claude API แปลข้อความ (จีน/อังกฤษ/พม่า) เป็นไทย"""
    system_prompt = (
        "คุณเป็นนักแปลมืออาชีพ หน้าที่ของคุณคือแปลข้อความที่ได้รับ "
        "(อาจเป็นภาษาจีน อังกฤษ หรือพม่า) ให้เป็นภาษาไทยที่อ่านลื่นและเป็นธรรมชาติ "
        "รักษาความหมาย ตัวเลข และรูปแบบตาราง/บรรทัดเดิมไว้ให้มากที่สุด "
        "ตอบกลับเฉพาะคำแปลภาษาไทยเท่านั้น ห้ามใส่คำอธิบายเพิ่มเติมหรือคำนำ"
    )
    payload = {
        "model": ANTHROPIC_MODEL,
        "max_tokens": 4000,
        "system": system_prompt,
        "messages": [{"role": "user", "content": text}],
    }
    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
    }
    result = http_post_json(ANTHROPIC_API, payload, headers=headers, timeout=90)
    parts = [b["text"] for b in result.get("content", []) if b.get("type") == "text"]
    return "".join(parts).strip()


def main():
    my_id = get_me()
    state = load_state()
    last_update_id = state.get("last_update_id", 0)

    updates = get_updates(offset=last_update_id + 1)
    if not updates:
        print("ไม่มีข้อความใหม่")
        return

    max_update_id = last_update_id

    for update in updates:
        update_id = update["update_id"]
        max_update_id = max(max_update_id, update_id)

        message = update.get("message")
        if not message:
            continue

        chat = message.get("chat", {})
        if str(chat.get("id")) != str(TELEGRAM_CHAT_ID):
            continue  # ไม่ใช่กลุ่มที่เราสนใจ ข้าม

        sender = message.get("from", {})
        if sender.get("id") == my_id:
            continue  # ข้อความจากตัวเอง (บอทแปล) ข้ามเพื่อกันลูป

        text = message.get("text")
        if not text:
            continue  # ข้ามถ้าไม่ใช่ข้อความตัวอักษร (เช่น รูปภาพ/สติกเกอร์)

        if looks_like_thai_only(text):
            print(f"ข้าม (ดูเหมือนเป็นไทยอยู่แล้ว): {text[:30]}...")
            continue

        try:
            translated = translate_to_thai(text)
        except Exception as e:
            print(f"แปลไม่สำเร็จ: {e}", file=sys.stderr)
            continue

        if not translated:
            continue

        try:
            send_message(
                TELEGRAM_CHAT_ID,
                translated,
                reply_to_message_id=message.get("message_id"),
            )
            print(f"แปลและส่งสำเร็จ: {text[:30]}... -> {translated[:30]}...")
        except Exception as e:
            print(f"ส่งข้อความไม่สำเร็จ: {e}", file=sys.stderr)

        time.sleep(1)  # กันโดน rate limit ของ Telegram

    save_state({"last_update_id": max_update_id})


if __name__ == "__main__":
    main()
