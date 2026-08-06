# Auto-tracker image (Slack Socket Mode long-running process).
# Build from the repo root:  docker build -f deploy/Dockerfile -t sui-ops-bot .
FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .

# Unbuffered so logs/audit stream straight to the container log.
ENV PYTHONUNBUFFERED=1

CMD ["python", "-m", "sui_ops_bot.slackbot"]
