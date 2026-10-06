FROM python:3.12-slim

WORKDIR /srv
COPY app/ ./app/

ENV LISTEN_HOST=0.0.0.0 \
    LISTEN_PORT=8080 \
    DB_PATH=/data/workspace.db \
    LOCK_TIMEOUT_SECONDS=300

RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8080

HEALTHCHECK --interval=10s --timeout=3s --retries=5 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/__editor__', timeout=3).status==200 else 1)"

CMD ["python", "-m", "app"]
