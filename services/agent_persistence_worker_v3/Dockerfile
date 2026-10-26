FROM python:3.12-slim@sha256:44ff437bba879d4941b710a369a8f19266aea34b29002807f0c487fabc9eec9b

WORKDIR /app

COPY services/agent_persistence_worker_v3/requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir --require-hashes -r /tmp/requirements.lock

COPY --chown=10001:10001 . /app

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

USER 10001:10001

CMD ["sh", "-c", "uvicorn services.agent_persistence_worker_v3.app.main:app --host 0.0.0.0 --port ${PORT:-8080}"]

