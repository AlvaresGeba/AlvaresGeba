FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements-phone.txt .
RUN pip install --no-cache-dir -r requirements-phone.txt

COPY phone_service.py phone_hub.py ./

ENV PHONE_HOST=0.0.0.0 PHONE_PORT=8002
EXPOSE 8002
CMD ["python", "phone_service.py"]
