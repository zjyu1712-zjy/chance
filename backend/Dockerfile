FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 COZE_WORKSPACE_PATH=/app
WORKDIR /app
COPY portable/requirements.lock /app/portable/requirements.lock
RUN pip install --no-cache-dir --require-hashes -r portable/requirements.lock
RUN useradd --uid 10001 --create-home app
COPY --chown=app:app src /app/src
COPY --chown=app:app config /app/config
COPY --chown=app:app assets/materials /app/assets/materials
COPY --chown=app:app assets/rules /app/assets/rules
COPY --chown=app:app portable /app/portable
USER app
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "portable.app:app", "--host", "0.0.0.0", "--port", "8000"]
