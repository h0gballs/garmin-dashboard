FROM python:3.13-slim AS builder
WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

FROM python:3.13-slim
RUN groupadd --gid 10001 appuser && useradd --uid 10001 --gid 10001 --no-create-home appuser
COPY --from=builder /install /usr/local
WORKDIR /app
COPY app ./app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MPLCONFIGDIR=/tmp/mpl \
    GARMIN_DB_PATH=/data/garmin.db

USER appuser
EXPOSE 8080
ENTRYPOINT ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
