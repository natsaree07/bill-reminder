"""
Notification Worker
---------------------
Container แยกต่างหาก ไม่มีหน้าเว็บ ทำหน้าที่เดียวคือ:
เช็คฐานข้อมูลเป็นระยะ ว่ามีรายการบิล/เอกสารที่ "ใกล้ครบกำหนด" หรือ "เลยกำหนดแล้ว"
ที่ยังไม่เคยส่งอีเมลแจ้งเตือนหรือไม่ ถ้ามีจะส่งอีเมลสรุปแล้วบันทึกว่าแจ้งแล้ว

หมายเหตุ: เดิมทีตั้งใจใช้ LINE Notify แต่บริการนี้ปิดให้บริการไปแล้วตั้งแต่ 31 มี.ค. 2025
จึงเปลี่ยนมาใช้อีเมล (SMTP) แทน ซึ่งใช้งานได้จริงและไม่มีค่าใช้จ่าย
"""

import os
import time
import ssl
import smtplib
from email.mime.text import MIMEText
from datetime import datetime, timedelta
import psycopg2

# ---------- ตั้งค่าการเชื่อมต่อฐานข้อมูล (ใช้ฐานข้อมูลเดียวกับ web) ----------
DB_HOST = os.environ.get("DB_HOST", "db")
DB_NAME = os.environ.get("DB_NAME", "reminderdb")
DB_USER = os.environ.get("DB_USER", "reminder_user")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "reminder_pass")

# ---------- ตั้งค่าการส่งอีเมล ----------
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
NOTIFY_TO_EMAIL = os.environ.get("NOTIFY_TO_EMAIL", SMTP_USER)

# ระยะเวลาระหว่างการเช็คแต่ละรอบ (วินาที)
CHECK_INTERVAL_SECONDS = int(os.environ.get("CHECK_INTERVAL_SECONDS", "15"))

# เตือนซ้ำทุกกี่ชั่วโมง สำหรับรายการที่ "เลยกำหนดแล้ว" และยังไม่ทำเสร็จ (ค่าเริ่มต้น 24 ชม. = วันละครั้ง)
OVERDUE_REPEAT_HOURS = float(os.environ.get("OVERDUE_REPEAT_HOURS", "24"))

def get_connection():
    """เชื่อมต่อฐานข้อมูล พร้อม retry เผื่อ container db ยังไม่พร้อม"""
    for attempt in range(10):
        try:
            return psycopg2.connect(
                host=DB_HOST, dbname=DB_NAME, user=DB_USER, password=DB_PASSWORD
            )
        except psycopg2.OperationalError:
            print(f"Database not ready, retrying... ({attempt + 1}/10)")
            time.sleep(3)
    raise Exception("ไม่สามารถเชื่อมต่อฐานข้อมูลได้")

def ensure_schema(conn):
    """เพิ่มคอลัมน์ last_notified_at ถ้ายังไม่มี (เผื่อ deploy ทับของเก่า)"""
    cur = conn.cursor()
    cur.execute("ALTER TABLE items ADD COLUMN IF NOT EXISTS last_notified_at TIMESTAMP;")
    conn.commit()
    cur.close()

def fetch_items_to_notify(conn):
    """
    ดึงรายการที่ต้องแจ้งเตือน แบ่งเป็น 2 กรณี:
    1. ยังไม่เคยแจ้งเตือนเลย (notified_via_email = FALSE) เช่น เพิ่งเข้าเกณฑ์เตือน หรือเพิ่งทำเสร็จ
    2. เลยกำหนดแล้ว ยังไม่ทำเสร็จ และแจ้งเตือนครั้งล่าสุดผ่านมาเกิน OVERDUE_REPEAT_HOURS แล้ว (เตือนซ้ำ)
    """
    cutoff = datetime.now() - timedelta(hours=OVERDUE_REPEAT_HOURS)
    cur = conn.cursor()
    cur.execute("""
        SELECT id, title, category, due_date, urgency, is_done
        FROM items
        WHERE (notified_via_email = FALSE AND (is_done = TRUE OR urgency != 'ปกติ'))
           OR (is_done = FALSE AND urgency = 'เลยกำหนดแล้ว'
               AND (last_notified_at IS NULL OR last_notified_at <= %s))
        ORDER BY due_date ASC
    """, (cutoff,))
    rows = cur.fetchall()
    cur.close()
    return rows


def send_email(items):
    """ส่งอีเมลสรุปรายการที่ต้องแจ้งเตือน (ทั้งเตือนใกล้กำหนด และแจ้งยืนยันเมื่อทำเสร็จ)"""
    subject = f"🔔 อัปเดตรายการบิล/เอกสาร {len(items)} รายการ"
    lines = []
    for (_id, title, category, due_date, urgency, is_done) in items:
        if is_done:
            lines.append(f"- ✅ {title} ({category}) จ่าย/ดำเนินการเสร็จเรียบร้อยแล้ว")
        else:
            lines.append(f"- ⏰ {title} ({category}) | ครบกำหนด {due_date} | สถานะ: {urgency}")
    body = "รายการอัปเดตล่าสุด:\n\n" + "\n".join(lines)

    if not SMTP_USER or not SMTP_PASSWORD:
        # ยังไม่ได้ตั้งค่า SMTP -> พิมพ์ลง log แทน เพื่อให้ยังเห็นว่าระบบทำงานถูกต้อง
        print("⚠️  SMTP ยังไม่ได้ตั้งค่า (SMTP_USER / SMTP_PASSWORD ว่างอยู่)")
        print("จะส่งอีเมลนี้เมื่อมีการตั้งค่า:\n")
        print(f"Subject: {subject}")
        print(body)
        return True

    msg = MIMEText(body, _charset="utf-8")
    msg["Subject"] = subject
    msg["From"] = SMTP_USER
    msg["To"] = NOTIFY_TO_EMAIL

    try:
        context = ssl.create_default_context()
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls(context=context)
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(SMTP_USER, NOTIFY_TO_EMAIL, msg.as_string())
        print(f"✅ ส่งอีเมลแจ้งเตือนสำเร็จ ({len(items)} รายการ)")
        return True
    except Exception as e:
        print(f"❌ ส่งอีเมลไม่สำเร็จ: {e}")
        return False


def mark_as_notified(conn, item_ids):
    """บันทึกว่าส่งอีเมลแจ้งเตือนรายการเหล่านี้แล้ว พร้อมเวลาที่แจ้งล่าสุด"""
    cur = conn.cursor()
    cur.execute(
        "UPDATE items SET notified_via_email = TRUE, last_notified_at = NOW() WHERE id = ANY(%s)",
        (item_ids,),
    )
    conn.commit()
    cur.close()


def run_check_cycle():
    """เช็ค 1 รอบ: ดึงรายการที่ต้องแจ้งเตือน -> ส่งอีเมล -> บันทึกว่าแจ้งแล้ว"""
    conn = get_connection()
    try:
        items = fetch_items_to_notify(conn)
        if items:
            success = send_email(items)
            if success:
                mark_as_notified(conn, [item[0] for item in items])
        else:
            print("ไม่มีรายการใหม่ที่ต้องแจ้งเตือนในรอบนี้")
    finally:
        conn.close()


if __name__ == "__main__":
    print("🔔 Notification Worker เริ่มทำงาน")
    print(f"เช็คทุก {CHECK_INTERVAL_SECONDS} วินาที | เตือนซ้ำรายการเลยกำหนดทุก {OVERDUE_REPEAT_HOURS} ชม. | ส่งอีเมลไปที่: {NOTIFY_TO_EMAIL or '(ยังไม่ได้ตั้งค่า)'}")
    ensure_schema(get_connection())
    while True:
        try:
            run_check_cycle()
        except Exception as e:
            print(f"เกิดข้อผิดพลาดระหว่างเช็ค: {e}")
        time.sleep(CHECK_INTERVAL_SECONDS)
