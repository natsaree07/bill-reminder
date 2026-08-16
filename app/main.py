"""
Document / Bill Due Reminder
------------------------------
ระบบแจ้งเตือนบิล/เอกสารที่ต้องส่ง-ชำระก่อนวันครบกำหนด
ผู้ใช้บันทึกรายการ (บิล, งาน, เอกสาร) พร้อมวันครบกำหนด
Background job เช็คทุกวันว่ารายการไหน "ใกล้ครบกำหนด" หรือ "เลยกำหนดแล้ว"
แล้วจัดระดับความเร่งด่วนอัตโนมัติ พร้อมแจ้งเตือนบน dashboard
"""

import os
import time
import calendar
import psycopg2
from datetime import datetime, date
from flask import Flask, render_template, jsonify, request
from apscheduler.schedulers.background import BackgroundScheduler

app = Flask(__name__)

# ---------- ตั้งค่าการเชื่อมต่อฐานข้อมูล ----------
DB_HOST = os.environ.get("DB_HOST", "db")
DB_NAME = os.environ.get("DB_NAME", "reminderdb")
DB_USER = os.environ.get("DB_USER", "reminder_user")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "reminder_pass")

# ระดับการแจ้งเตือนล่วงหน้าหลายขั้น (ปรับได้ผ่าน REMINDER_THRESHOLDS เช่น "7,3,1")
_thresholds_raw = os.environ.get("REMINDER_THRESHOLDS", "7,3,1")
REMINDER_THRESHOLDS = sorted([int(x) for x in _thresholds_raw.split(",")], reverse=True)


def get_connection():
    """เชื่อมต่อฐานข้อมูล พร้อม retry เผื่อ container db ยังไม่พร้อม"""
    for attempt in range(10):
        try:
            conn = psycopg2.connect(
                host=DB_HOST, dbname=DB_NAME, user=DB_USER, password=DB_PASSWORD
            )
            return conn
        except psycopg2.OperationalError:
            print(f"Database not ready, retrying... ({attempt + 1}/10)")
            time.sleep(3)
    raise Exception("ไม่สามารถเชื่อมต่อฐานข้อมูลได้")


def init_db():
    """สร้างตารางเก็บรายการบิล/เอกสาร ถ้ายังไม่มี"""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS items (
            id SERIAL PRIMARY KEY,
            title TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT 'บิล',
            amount REAL,
            due_date DATE NOT NULL,
            urgency TEXT NOT NULL DEFAULT 'ปกติ',
            is_done BOOLEAN NOT NULL DEFAULT FALSE,
            is_recurring BOOLEAN NOT NULL DEFAULT FALSE,
            has_new_update BOOLEAN NOT NULL DEFAULT FALSE,
            notified_via_email BOOLEAN NOT NULL DEFAULT FALSE,
            created_at TIMESTAMP NOT NULL DEFAULT NOW()
        );
    """)
    # migration แบบง่าย: เผื่อกรณีตาราง items ถูกสร้างไว้ก่อนหน้าด้วยโครงสร้างเก่า
    # (เช่น จาก docker volume เดิมที่ยังไม่มีคอลัมน์พวกนี้) ให้เพิ่มคอลัมน์ที่ขาดหายอัตโนมัติ
    cur.execute("ALTER TABLE items ADD COLUMN IF NOT EXISTS is_recurring BOOLEAN NOT NULL DEFAULT FALSE;")
    cur.execute("ALTER TABLE items ADD COLUMN IF NOT EXISTS notified_via_email BOOLEAN NOT NULL DEFAULT FALSE;")
    cur.execute("ALTER TABLE items ADD COLUMN IF NOT EXISTS last_notified_at TIMESTAMP;")
    conn.commit()
    cur.close()
    conn.close()
    print("Database initialized.")


def add_one_month(d):
    """คำนวณวันที่เดือนถัดไป โดยจัดการกรณีวันที่ไม่มีในเดือนนั้น (เช่น 31 ม.ค. -> 28/29 ก.พ.)"""
    month = d.month + 1
    year = d.year
    if month > 12:
        month = 1
        year += 1
    last_day_of_month = calendar.monthrange(year, month)[1]
    day = min(d.day, last_day_of_month)
    return date(year, month, day)


def compute_urgency(due_date):
    """
    คำนวณระดับความเร่งด่วนจากวันครบกำหนด แบบหลายขั้น
    เช่น ถ้า REMINDER_THRESHOLDS = [7, 3, 1] จะได้ระดับ:
    เลยกำหนดแล้ว / ใกล้ครบกำหนดมาก (≤1 วัน) / ใกล้ครบกำหนด (≤3 วัน) / เตือนล่วงหน้า (≤7 วัน) / ปกติ
    """
    days_left = (due_date - date.today()).days

    if days_left < 0:
        return "เลยกำหนดแล้ว"

    # เรียง threshold จากน้อยไปมาก เพื่อเช็คระดับที่ใกล้สุด (เข้มงวดสุด) ก่อนเสมอ
    sorted_thresholds = sorted(REMINDER_THRESHOLDS)
    for i, threshold in enumerate(sorted_thresholds):
        if days_left <= threshold:
            if i == 0:
                return f"ใกล้ครบกำหนดมาก (≤{threshold} วัน)"
            elif i == len(sorted_thresholds) - 1:
                return f"เตือนล่วงหน้า (≤{threshold} วัน)"
            else:
                return f"ใกล้ครบกำหนด (≤{threshold} วัน)"

    return "ปกติ"


def refresh_urgency_levels():
    """
    Background job: เช็คทุกรายการที่ยังไม่เสร็จ แล้วอัปเดตระดับความเร่งด่วน
    ถ้าระดับเปลี่ยนไปจากเดิม (เช่น ปกติ -> ใกล้ครบกำหนด) จะตั้ง flag แจ้งเตือน
    """
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("SELECT id, due_date, urgency FROM items WHERE is_done = FALSE")
        items = cur.fetchall()

        for item_id, due_date, old_urgency in items:
            new_urgency = compute_urgency(due_date)
            if new_urgency != old_urgency:
                cur.execute(
                    """UPDATE items SET urgency = %s, has_new_update = TRUE,
                       notified_via_email = FALSE WHERE id = %s""",
                    (new_urgency, item_id),
                )
                print(f"[{datetime.now()}] Item #{item_id}: {old_urgency} -> {new_urgency}")

        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"Error refreshing urgency levels: {e}")


@app.route("/")
def dashboard():
    thresholds_text = ", ".join(f"{t} วัน" for t in sorted(REMINDER_THRESHOLDS, reverse=True))
    return render_template("index.html", thresholds_text=thresholds_text)


@app.route("/api/items")
def api_items():
    """คืนรายการทั้งหมด เรียงตามความเร่งด่วน (เลยกำหนด > ใกล้ครบกำหนด > ปกติ > เสร็จแล้ว)"""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT id, title, category, amount, due_date, urgency, is_done, has_new_update, is_recurring
        FROM items
        ORDER BY is_done ASC, due_date ASC
    """)
    rows = cur.fetchall()
    cur.close()
    conn.close()

    items = []
    for r in rows:
        days_left = (r[4] - date.today()).days
        items.append({
            "id": r[0],
            "title": r[1],
            "category": r[2],
            "amount": r[3],
            "due_date": r[4].strftime("%d/%m/%Y"),
            "days_left": days_left,
            "urgency": r[5],
            "is_done": r[6],
            "has_new_update": r[7],
            "is_recurring": r[8],
        })
    return jsonify(items)


@app.route("/api/items", methods=["POST"])
def add_item():
    """เพิ่มรายการบิล/เอกสารใหม่"""
    data = request.get_json()
    title = data.get("title", "").strip()
    category = data.get("category", "บิล").strip()
    amount = data.get("amount") or None
    due_date_str = data.get("due_date", "").strip()
    is_recurring = bool(data.get("is_recurring", False))

    if not title or not due_date_str:
        return jsonify({"error": "กรุณากรอกชื่อรายการและวันครบกำหนด"}), 400

    try:
        due_date = datetime.strptime(due_date_str, "%Y-%m-%d").date()
    except ValueError:
        return jsonify({"error": "รูปแบบวันที่ไม่ถูกต้อง"}), 400

    urgency = compute_urgency(due_date)

    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO items (title, category, amount, due_date, urgency, is_recurring)
           VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
        (title, category, amount, due_date, urgency, is_recurring),
    )
    new_id = cur.fetchone()[0]
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"status": "ok", "id": new_id})


@app.route("/api/items/<int:item_id>/done", methods=["POST"])
def mark_done(item_id):
    """
    ทำเครื่องหมายว่ารายการนี้เสร็จ/ชำระแล้ว
    ถ้าเป็นบิลรายเดือน (is_recurring) จะสร้างรายการของเดือนถัดไปให้อัตโนมัติ
    """
    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        "SELECT title, category, amount, due_date, is_recurring FROM items WHERE id = %s",
        (item_id,),
    )
    item = cur.fetchone()

    cur.execute(
        "UPDATE items SET is_done = TRUE, has_new_update = FALSE, notified_via_email = FALSE WHERE id = %s",
        (item_id,),
    )

    next_bill_created = False
    if item and item[4]:  # is_recurring == True
        title, category, amount, due_date, _ = item
        next_due_date = add_one_month(due_date)
        next_urgency = compute_urgency(next_due_date)
        cur.execute(
            """INSERT INTO items (title, category, amount, due_date, urgency, is_recurring)
               VALUES (%s, %s, %s, %s, %s, TRUE)""",
            (title, category, amount, next_due_date, next_urgency),
        )
        next_bill_created = True

    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"status": "ok", "next_bill_created": next_bill_created})


@app.route("/api/items/<int:item_id>/mark-read", methods=["POST"])
def mark_read(item_id):
    """ล้าง flag แจ้งเตือนเมื่อผู้ใช้กดดูแล้ว"""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("UPDATE items SET has_new_update = FALSE WHERE id = %s", (item_id,))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"status": "ok"})


@app.route("/api/items/<int:item_id>", methods=["DELETE"])
def delete_item(item_id):
    """ลบรายการ"""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM items WHERE id = %s", (item_id,))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"status": "ok"})


@app.route("/api/check-now")
def check_now():
    """endpoint ไว้กดเช็คระดับความเร่งด่วนทันที (สะดวกตอน demo ในคลิป)"""
    refresh_urgency_levels()
    return jsonify({"status": "ok", "message": "เช็คสถานะล่าสุดเรียบร้อย"})


# ---------- ตั้งเวลาให้เช็คความเร่งด่วนอัตโนมัติทุกวัน ----------
# (ตั้งเป็นทุก 1 นาทีเพื่อให้เห็นผลตอน demo ในคลิป ในระบบจริงควรตั้งเป็นทุกวันแทน)
scheduler = BackgroundScheduler()
scheduler.add_job(refresh_urgency_levels, "interval", minutes=1)
scheduler.start()

if __name__ == "__main__":
    init_db()
    refresh_urgency_levels()
    app.run(host="0.0.0.0", port=5000, debug=False)
