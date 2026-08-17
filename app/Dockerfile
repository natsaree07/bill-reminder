# ใช้ Python 3.12 Slim เป็น Base Image
FROM python:3.12-slim

# กำหนดโฟลเดอร์ทำงานภายใน Container
WORKDIR /app

# Copy requirements และติดตั้ง Dependencies
# แยกไว้ก่อนเพื่อใช้ Docker Layer Cache
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy Source Code ของ Application เข้าไปใน Image
COPY . .

# กำหนด Port ที่ Flask Application ใช้งาน
EXPOSE 5000

# คำสั่งเริ่มต้นเมื่อ Container ทำงาน
CMD ["python", "main.py"]