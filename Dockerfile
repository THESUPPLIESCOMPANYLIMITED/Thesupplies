FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 AUTOMATION_HOST=0.0.0.0 AUTOMATION_PORT=8080
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN useradd --system --uid 10001 --create-home appuser \
    && mkdir -p /data \
    && chown -R appuser:appuser /app /data
USER 10001:10001
ENV PYTHONPATH=/app/src AUTOMATION_DB_PATH=/data/receipts.sqlite3
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD python -c "from urllib.request import urlopen; urlopen('http://127.0.0.1:8080/healthz', timeout=2)"
CMD ["python", "-m", "supply_automation.server"]
