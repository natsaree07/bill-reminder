# ---- Dockerfile: Document / Bill Due Reminder (Flask app) ----
FROM python:3.12-slim

WORKDIR /app

# copy requirements ก่อนเพื่อใช้ Docker layer cache
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# copy โค้ดทั้งหมดของแอปเข้าไปใน image
COPY . .

EXPOSE 5000

CMD ["python", "main.py"]
