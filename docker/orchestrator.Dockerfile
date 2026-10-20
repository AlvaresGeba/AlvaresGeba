FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements-orchestrator.txt .
RUN pip install --no-cache-dir -r requirements-orchestrator.txt

COPY gateway.py elevation.py history.py log_buffer.py ./

# data/ holds the request history (a docker volume; world-writable so the
# volume works for whatever uid the container runs as). certs/ is mounted
# at runtime (see docker-compose.yml) - never baked into the image.
RUN mkdir -m 1777 /app/data
# 8443: the mTLS gateway. 8001: elevation for the other services.
EXPOSE 8443 8001
CMD ["python", "gateway.py"]
