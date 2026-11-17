# Bulwark Demo App — Dockerfile
# Inherits from Python 3.12-slim base (pre-warmed in Artifact Registry by scripts/prewarm_images.sh)

FROM python:3.12-slim

WORKDIR /app

# Install dependencies first (layer-cached on pre-warmed base image)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY main.py .
COPY prompts/ ./prompts/

# Non-root user for security
RUN useradd -m -u 1000 appuser
USER appuser

# Cloud Run requires port 8080
EXPOSE 8080

# DEMO_VERSION controls regression vs fixed behaviour
# Set to "regression" on the bad branch, "fixed" on the fix branch
ENV DEMO_VERSION=fixed
ENV WIREMOCK_BASE_URL=http://localhost:8082

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080", "--log-level", "info"]
