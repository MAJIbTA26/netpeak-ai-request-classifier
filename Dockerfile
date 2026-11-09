FROM python:3.13-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY *.py .
WORKDIR /data
EXPOSE 8000
# За замовчуванням - пакетна обробка CSV (як раніше).
# Для HTTP-сервісу команду перевизначає docker-compose.yml (сервіс api).
CMD ["python", "/app/main.py"]
