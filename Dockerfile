FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# INSTALL_DEV=1 adds the test dependencies (compose "test" profile)
ARG INSTALL_DEV=0

COPY pyproject.toml ./
COPY app ./app
COPY data ./data
COPY scripts ./scripts
COPY tests ./tests

RUN pip install . \
    && if [ "$INSTALL_DEV" = "1" ]; then pip install ".[dev]"; fi

RUN useradd --system --uid 10001 --no-create-home app \
    && chown -R app:app /app
USER app

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=5 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health').status==200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
