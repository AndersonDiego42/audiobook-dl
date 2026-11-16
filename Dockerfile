FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    XDG_CONFIG_HOME=/config \
    XDG_DATA_HOME=/config \
    AUDIOBOOK_DL_CONFIG=/config/audiobook-dl.toml \
    AUDIOBOOK_DL_ALLOW_PLAINTEXT_CREDENTIALS=1

RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg ca-certificates && \
    rm -rf /var/lib/apt/lists/* && \
    groupadd -r audiobookdl && \
    useradd -r -g audiobookdl -d /config -s /sbin/nologin audiobookdl && \
    mkdir -p /config /audiobooks && \
    chown -R audiobookdl:audiobookdl /config /audiobooks

WORKDIR /app
COPY pyproject.toml README.md ./
COPY audiobookdl ./audiobookdl

RUN pip install --no-cache-dir ".[export]"

USER audiobookdl
VOLUME ["/config", "/audiobooks"]
WORKDIR /audiobooks
EXPOSE 8080

ENTRYPOINT ["audiobook-dl"]
CMD ["sync", "--watch", "--interval", "3600"]
