FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY switchyard ./switchyard
COPY runs ./runs
RUN pip install --no-cache-dir ".[tracing]"
ENV SWITCHYARD_PUBLIC=1 PORT=7860 SWITCHYARD_RUNS=/app/runs
EXPOSE 7860
CMD ["sh", "-c", "uvicorn switchyard.server:app --host 0.0.0.0 --port ${PORT}"]
