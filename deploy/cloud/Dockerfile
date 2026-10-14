FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VARYZ_CLOUD_DATABASE=/var/lib/varyz-cloud/cloud.sqlite3 \
    PORT=8080 \
    FORWARDED_ALLOW_IPS=127.0.0.1

WORKDIR /opt/varyz
ARG VARYZ_CLOUD_SEARCH=0
COPY pyproject.toml README.md LICENSE LICENSE.additional-terms NOTICE ./
COPY src/ ./src/
COPY deploy/cloud/requirements.txt ./requirements.txt
COPY deploy/cloud/requirements-search.txt ./requirements-search.txt
# The base image has keyword search; the optional archive build adds local ONNX.
# Install the package without local dependencies, then the `cloud` extra from
# hash-pinned requirements so every build gets the versions that were tested.
RUN python -m pip install --no-cache-dir --no-deps . \
    && python -m pip install --no-cache-dir --no-deps --require-hashes -r requirements.txt \
    && groupadd --gid 10001 varyz \
    && useradd --uid 10001 --gid 10001 --no-create-home varyz \
    && mkdir -p /var/lib/varyz-cloud \
    && chmod 700 /var/lib/varyz-cloud \
    && chown 10001:10001 /var/lib/varyz-cloud

# Explicit build-time provisioning. HTTP requests never download model weights
# or send passages/queries to a model provider. No private records enter the image.
RUN if [ "$VARYZ_CLOUD_SEARCH" = "1" ]; then \
      python -m pip install --no-cache-dir --no-deps --require-hashes -r requirements-search.txt \
      && python -c "from fastembed import TextEmbedding; next(iter(TextEmbedding('sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2', cache_dir='/opt/varyz/models', threads=2).query_embed('initialize')))" \
      && chown -R 10001:10001 /opt/varyz/models; \
    fi

USER 10001:10001
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.getenv('PORT', '8080') + '/health', timeout=3)"
# One process: the SQLite layout is single-instance. Request and queue capacity
# remain bounded with either backend. Access logs must not expose queries.
# Client addresses come from X-Forwarded-For only on connections from
# FORWARDED_ALLOW_IPS (the edge proxy); Compose sets it to its network gateway.
CMD ["sh", "-c", "umask 077; exec python -m uvicorn varyz.cloud.app:create_app --factory --host 0.0.0.0 --port \"${PORT:-8080}\" --workers 1 --limit-concurrency 128 --backlog 128 --timeout-keep-alive 2 --no-access-log --proxy-headers"]
