FROM python:3.11-slim

WORKDIR /app

RUN pip install --no-cache-dir "aiohttp[speedups]==3.9.5"

COPY auth_service.py .

EXPOSE 8080

CMD ["python", "-u", "auth_service.py"]
