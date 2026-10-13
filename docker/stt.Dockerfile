FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements-stt.txt .
RUN pip install --no-cache-dir -r requirements-stt.txt

COPY stt.py stt_service.py ./

EXPOSE 9000
CMD ["uvicorn", "stt_service:app", "--host", "0.0.0.0", "--port", "9000"]
