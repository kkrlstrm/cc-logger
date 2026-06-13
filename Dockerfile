# cc-logger container image
# Multi-stage: install deps with uv, then ship a minimal runtime image.

FROM python:3.13-slim AS builder

RUN pip install --no-cache-dir uv==0.11.14

WORKDIR /app

# Copy only files needed for dependency resolution first (better layer caching)
COPY pyproject.toml ./
COPY README.md ./
COPY src/ ./src/

# Install dependencies into a project-local venv
RUN uv sync --no-dev

# ----------- runtime stage -----------
FROM python:3.13-slim AS runtime

WORKDIR /app

# Copy the venv and source from builder
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/src /app/src
COPY pyproject.toml ./
COPY migrations/ ./migrations/
COPY queries/ ./queries/

ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONUNBUFFERED=1
ENV HOOK_PORT=8787

EXPOSE 8787

# Healthcheck hits /healthz
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8787/healthz').read()" || exit 1

CMD ["python", "-m", "cc_logger.cli", "serve"]
