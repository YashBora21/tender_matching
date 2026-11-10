# Backend image for the tender matching system (PRD 14).
# One of two published images: this is the backend, the frontend is separate.
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Tesseract is the OCR engine: a GeM tender page is often a picture of a
# typed BOQ, and a page with no text layer is unreadable without it. It is a
# program, not a package, so the image carries it (FR-9).
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
        tesseract-ocr tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*

# Dependencies first so code edits do not invalidate the install layer.
COPY requirements.txt requirements-dev.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app
COPY data/attribute_vocabulary.json data/categories.json data/doc_types.json data/doc_roles.json data/normalization_policy.json ./data/

# Packaged policy is separate from the writable corpus/upload/cache volume.
ENV DATA_DIR=/var/lib/tender/data \
    ATTRIBUTE_VOCABULARY_PATH=/app/data/attribute_vocabulary.json \
    CATEGORY_VOCABULARY_PATH=/app/data/categories.json \
    DOC_TYPES_PATH=/app/data/doc_types.json \
    NORMALIZATION_POLICY_PATH=/app/data/normalization_policy.json

# Run as a non-root user.
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /var/lib/tender/data \
    && chown -R appuser:appuser /app /var/lib/tender/data
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8000/api/health || exit 1

# Migrations run on start so a fresh database is usable without a separate step.
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
