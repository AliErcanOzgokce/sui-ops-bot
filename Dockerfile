FROM python:3.12-slim

WORKDIR /app

# System certs are already in slim; no build deps needed for these wheels.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .

# Unbuffered so logs/audit stream straight to the container log.
ENV PYTHONUNBUFFERED=1

CMD ["python", "bot.py"]
