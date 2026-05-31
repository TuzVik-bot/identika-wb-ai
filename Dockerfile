FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md /app/
COPY app /app/app
RUN pip install --no-cache-dir .
RUN useradd -m identika
USER identika
CMD ["sh", "-c", "uvicorn identika.app:create_app --factory --app-dir app --host ${IDENTIKA_HOST:-0.0.0.0} --port ${IDENTIKA_PORT:-8787}"]
