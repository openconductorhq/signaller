FROM python:3.14-slim
WORKDIR /app
COPY *.py ./
COPY ui ui
RUN mkdir /data && chown 1000:1000 /data
USER 1000:1000
ENV PYTHONUNBUFFERED=1 \
    SIGNALLER_IN_CONTAINER=1 \
    SIGNALLER_LISTEN=0.0.0.0:8787 \
    SIGNALLER_CONFIG_DIR=/config \
    SIGNALLER_STATS_DIR=/data \
    CLAUDE_CONFIG_DIR=/claude
EXPOSE 8787
CMD ["python3", "-E", "-s", "signaller.py"]
