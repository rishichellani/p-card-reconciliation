# P-Card reconciliation app. Build:  docker build -t pcard-reconciler .
# Run:    docker run --rm -p 8600:8501 --env-file .env -v pcard-output:/app/output pcard-reconciler
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first, so code edits do not reinstall them.
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# Run as an unprivileged user. output/ is where batches and audit artifacts are written: mount a volume there.
RUN useradd --create-home --uid 1000 app && mkdir -p /app/output /app/exports && chown -R app:app /app
USER app
VOLUME ["/app/output"]

EXPOSE 8501
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request as u; u.urlopen('http://localhost:8501/_stcore/health', timeout=4)"

# API keys are never baked into the image: pass them at run time (--env-file .env or -e GROQ_API_KEY=...).
CMD ["streamlit", "run", "app.py", "--server.port=8501", "--server.address=0.0.0.0", "--server.headless=true"]
