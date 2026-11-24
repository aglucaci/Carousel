# CAROUSEL API and scan worker (one image; docker-compose.yml runs it as both).
#
#   docker build -t carousel .
#   docker build --build-arg CAROUSEL_EXTRAS=server,semantic -t carousel .   # real embeddings (adds PyTorch, ~2 GB)
#
# Without the semantic extra, scans use the dependency-free hashing embedder.
FROM python:3.12-slim

ARG CAROUSEL_EXTRAS=server
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HOME=/data/hf \
    CAROUSEL_STORE=/data/engine.db

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install ".[${CAROUSEL_EXTRAS}]" \
 && useradd --system --uid 10001 --home-dir /data carousel \
 && mkdir -p /data \
 && chown carousel /data

USER carousel
EXPOSE 8000
CMD ["carousel", "api", "serve", "--host", "0.0.0.0", "--port", "8000"]
