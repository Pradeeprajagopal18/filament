FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
# Optional build args for corporate networks (HTTP_PROXY/HTTPS_PROXY are passed through by
# Docker automatically): an internal package mirror for pip.
ARG PIP_INDEX_URL
ARG PIP_TRUSTED_HOST
COPY pyproject.toml README.md ./
COPY filament ./filament
# postgres: the database driver; code: tree-sitter grammars for the code graph.
RUN pip install --no-cache-dir ".[postgres,code]"
ENV FILAMENT_DATA_DIR=/data
VOLUME /data
EXPOSE 8000
CMD ["uvicorn", "filament.main:app", "--host", "0.0.0.0", "--port", "8000"]
