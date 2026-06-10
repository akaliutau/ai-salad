FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    RUN_ROOT=/tmp/leetcode-runs

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && python -m playwright install --with-deps chromium

COPY . ./

# Cloud Run Job entrypoint. Execution-time --args should contain only the problem URL.
ENTRYPOINT ["python", "cloud_run_job.py"]
CMD ["--help"]
