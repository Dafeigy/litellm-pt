FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml ./
COPY src/ ./src/
RUN python -m pip install --no-cache-dir . \
    && useradd --system --uid 10001 --no-create-home relay

USER relay
EXPOSE 3000

CMD ["python", "-m", "litellm_passthrough", "--config", "/app/relay.yaml"]
