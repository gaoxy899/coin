FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Etc/UTC \
    TELEGRAM_STATE_FILE=/data/pinbar_telegram_state.json \
    IPHONE_STATE_FILE=/data/pinbar_iphone_state.json

WORKDIR /app

COPY requirements-runtime.txt ./
RUN pip install --no-cache-dir -r requirements-runtime.txt \
    && groupadd --gid 10001 pinbar \
    && useradd --uid 10001 --gid pinbar --no-create-home pinbar \
    && mkdir /data \
    && chown pinbar:pinbar /data

# Copy only runtime files; local secrets and notification history stay out.
COPY main.py pinbar_detector.py ./
COPY docker.env ./.env

USER 10001:10001
VOLUME ["/data"]

# This is a background worker. Deploy with docker-compose.yml on Cloady;
# a standalone Dockerfile deployment otherwise defaults to a web port.
ENTRYPOINT ["python", "main.py"]
CMD ["--monitor"]
