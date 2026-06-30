FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TAIPY_HOST=0.0.0.0 \
    TAIPY_PORT=5000

WORKDIR /app

# Сначала зависимости — кэш слоёв не инвалидируется при правках кода.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Затем исходники (logins.db тоже копируется — служит сидом, если том не примонтирован).
COPY . .

EXPOSE 5000

# Healthcheck без лишних пакетов (curl нет в slim) — через stdlib.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:5000')" || exit 1

CMD ["python", "app.py"]
