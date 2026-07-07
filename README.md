# Telegram Group Translator Bot

แปลข้อความในกลุ่ม Telegram (จีน / อังกฤษ / พม่า) เป็นไทยอัตโนมัติ โดยใช้ Claude API
รันผ่าน GitHub Actions ตามตารางเวลาที่กำหนด (ค่าเริ่มต้น: 18:00-19:00 น. เวลาปักกิ่งทุกวัน)

## วิธีตั้งค่า

1. สร้าง repo ใหม่บน GitHub (Public หรือ Private ก็ได้) แล้วอัปโหลดไฟล์ทั้งหมดในโฟลเดอร์นี้ขึ้นไป
   (ต้องมีไฟล์ `translate_bot.py`, `state.json`, และ `.github/workflows/translate.yml`)

2. ไปที่ Settings → Secrets and variables → Actions → New repository secret
   เพิ่ม secret ทั้งหมดนี้:

   | Name | Value |
   |---|---|
   | `TRANSLATOR_BOT_TOKEN` | token ของ "thai translation bot" (จาก BotFather) |
   | `TELEGRAM_GROUP_CHAT_ID` | chat id ของกลุ่ม (ค่าติดลบ เช่น `-5543368117`) |
   | `ANTHROPIC_API_KEY` | Anthropic API key ของคุณ |

3. ไปที่แท็บ Actions → เปิดใช้งาน workflow ถ้ายังไม่เปิด

4. ทดสอบ: ไปที่ workflow "Translate Group Messages to Thai" → กด "Run workflow"
   เพื่อรันทันที (ไม่ต้องรอถึงเวลา 18:00)

5. พิมพ์ข้อความจีน/อังกฤษ/พม่าในกลุ่ม Telegram แล้วลอง Run workflow อีกครั้ง
   ควรเห็นคำแปลไทยถูกส่งกลับเข้ากลุ่มเป็นข้อความ reply

## การทำงาน

- ทุกครั้งที่รัน สคริปต์จะเช็คข้อความใหม่ในกลุ่มตั้งแต่ครั้งก่อนหน้า (เก็บตำแหน่งไว้ใน `state.json`)
- ข้ามข้อความที่ส่งมาจากบอทแปลเอง (กันไม่ให้แปลวนลูป)
- ข้ามข้อความที่ดูเหมือนเป็นภาษาไทยอยู่แล้ว
- ข้อความที่เหลือ (จีน/อังกฤษ/พม่า/อื่นๆ) จะถูกส่งไปแปลผ่าน Claude แล้วตอบกลับในกลุ่มแบบ reply

## ปรับตารางเวลา

แก้ไขบรรทัด `cron` ในไฟล์ `.github/workflows/translate.yml`
เวลาที่ใช้ใน cron เป็น **UTC เสมอ** (เวลาปักกิ่ง/ไทย = UTC + 7 หรือ +8 แล้วแต่ตลาด)

ตัวอย่าง: อยากรันช่วง 18:00-19:00 เวลาปักกิ่ง (UTC+8) ทุกวัน ทุก 5 นาที
```yaml
schedule:
  - cron: "*/5 10 * * *"
```

## หมายเหตุด้านความปลอดภัย

- อย่า commit ไฟล์ `.env` หรือ token ตรงๆ ลง repo ใช้ GitHub Secrets เท่านั้น
- ถ้า repo เป็น Public คนอื่นจะเห็นโค้ดได้ (แต่เห็น Secrets ไม่ได้)
