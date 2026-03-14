FROM python:3.11-slim

WORKDIR /app

# Install dependencies
RUN pip install --no-cache-dir \
    fastapi==0.111.0 \
    uvicorn[standard]==0.29.0 \
    httpx==0.27.0 \
    python-multipart==0.0.9 \
    websockets==12.0

COPY auth_service.py .

EXPOSE 8000

# --ws websockets  → tells uvicorn to use the websockets library for WS handling
CMD ["uvicorn", "auth_service:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--ws", "websockets"]
