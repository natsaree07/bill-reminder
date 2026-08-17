# Container นี้เป็น Background Worker ไม่มี Web Server
FROM python:3.12-slim

# กำหนดโฟลเดอร์ทำงานภายใน Container
WORKDIR /app

# Copy requirements และติดตั้ง Dependencies
# แยกไว้ก่อนเพื่อใช้ Docker Layer Cache
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy Source Code ของ Worker เข้าไปใน Image
COPY . .

# ไม่ต้อง EXPOSE เพราะไม่มี Web Server
# รัน Worker เพื่อทำงานตรวจสอบและส่งการแจ้งเตือน
CMD ["python", "notifier.py"]