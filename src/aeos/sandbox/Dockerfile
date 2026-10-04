FROM python:3.12-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir pytest==8.3.4 \
    && useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin sandbox
# Node.js runtime so JavaScript repos can be syntax-checked (no npm, no network).
COPY --from=node:20-slim /usr/local/bin/node /usr/local/bin/node
WORKDIR /workspace
