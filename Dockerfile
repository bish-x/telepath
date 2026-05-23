# syntax=docker/dockerfile:1.7

FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TELEPATH_DATA_DIR=/app/data

# tini for proper PID 1 / signal handling; ca-certificates for HTTPS to LLM APIs.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tini ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first for build cache friendliness.
COPY pyproject.toml ./
COPY telepath ./telepath
RUN pip install --no-cache-dir .

# Persistent data dir (Telegram session, sqlite). Mount a host volume here.
RUN mkdir -p "${TELEPATH_DATA_DIR}"
VOLUME ["/app/data"]

# Single-user personal tool: run as root inside the container so host-side
# volume permissions never block first install. The data is private; the host
# directory owns its real permissions.

ENTRYPOINT ["tini", "--"]
CMD ["telepath"]
