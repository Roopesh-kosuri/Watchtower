FROM python:3.12-slim

WORKDIR /app

# Dependencies in their own layer, cached across rebuilds that only
# touch application code.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY schema/ ./schema/
COPY config/ ./config/

# config/ and data/ are meant to be bind-mounted at runtime (see
# docker-compose.yml) so you can edit config and persist the SQLite file
# without rebuilding the image. The COPY of config/ above just makes the
# image runnable standalone (`docker run`, no compose) using the sample
# config as a demo default -- compose's bind mount shadows it.
ENV WATCHTOWER_CONFIG=/app/config/config.yaml
ENV WATCHTOWER_SCHEMA=/app/schema/001_init.sql

EXPOSE 8000

# No curl in python:slim -- use urllib instead of adding a package just
# for the healthcheck.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)" || exit 1

CMD ["python3", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
