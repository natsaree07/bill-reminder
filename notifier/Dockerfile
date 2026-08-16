# ---- Dockerfile: Notification Worker ----
# container นี้ไม่มี web server ทำหน้าที่เป็น background worker อย่างเดียว
FROM python:3.12-slim

WORKDIR /app

# copy requirements ก่อนเพื่อใช้ Docker layer cache
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# copy สคริปต์ worker เข้าไปใน image
COPY . .

# ไม่ต้อง EXPOSE port เพราะไม่มี web server
# รันสคริปต์แบบ loop ตลอดเวลา (ไม่ใช่ one-shot)
CMD ["python", "notifier.py"]
