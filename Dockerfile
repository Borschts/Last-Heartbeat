FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml ./
COPY last_heartbeat ./last_heartbeat
RUN pip install --no-cache-dir . && useradd --uid 10001 --create-home heartbeat \
    && mkdir /data && chown heartbeat:heartbeat /data
USER heartbeat
ENV DATA_DIR=/data PYTHONUNBUFFERED=1
ENTRYPOINT ["last-heartbeat"]
CMD ["run"]
