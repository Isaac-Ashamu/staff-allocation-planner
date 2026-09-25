# Runs any booking_recommender console script (allocate-sessions, review-ui,
# import-standardized-csv, recommender-db, import-uk-postcodes, workbook-adapter).
# docker-compose.yml overrides `command` per service; review-ui is the only one
# that listens on a port. See docker-compose.yml and docs/DEPLOYMENT.md.
FROM python:3.12-slim

WORKDIR /app

# Pinned versions first so dependency layers cache independently of code changes.
COPY requirements-lock.txt ./
RUN pip install --no-cache-dir -r requirements-lock.txt

COPY pyproject.toml README.md ./
COPY core/python ./core/python
RUN pip install --no-cache-dir --no-deps .

# Tenant data, caches, and derived output all live under /data (see compose volumes).
VOLUME ["/data"]
WORKDIR /data

CMD ["allocate-sessions", "--help"]
