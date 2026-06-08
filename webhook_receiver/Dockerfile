FROM python:3.12-slim

WORKDIR /app

# Install dependencies
COPY webhook_receiver/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy shared modules
COPY shared/ ./shared/

# Copy GitLab helper package used for override handling
COPY orchestrator/ ./orchestrator/

# Copy webhook receiver
COPY webhook_receiver/ ./

# Expose port
EXPOSE 8080

# Health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import httpx; httpx.get('http://localhost:8080/health').raise_for_status()"

# Run with uvicorn
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
