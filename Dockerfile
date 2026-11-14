# Python 3.9 (the original base) is end-of-life since October 2025.
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

# Only the application code; see .dockerignore (no .git, no .env).
COPY app.py .

RUN useradd --system --uid 10001 --no-create-home app
USER app

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4).status == 200 else 1)"]

# The access log records the path only (%(U)s), never the query string: a
# PrivateBin ?link= query carries the paste's decryption key.
# gunicorn 27's control socket is disabled: it is unused and would try
# to write to $HOME on the read-only root filesystem.
CMD ["sh", "-c", "exec gunicorn --bind 0.0.0.0:8080 --workers \"${WEB_CONCURRENCY:-2}\" --timeout 30 --worker-tmp-dir /dev/shm --no-control-socket --error-logfile - --access-logfile - --access-logformat '%(h)s \"%(m)s %(U)s\" %(s)s %(B)s %(M)sms' 'app:create_app()'"]
