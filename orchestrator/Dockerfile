FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY shared/ ./shared/
COPY orchestrator/ ./orchestrator/
COPY environment_builder/ ./environment_builder/
COPY traffic_replay/ ./traffic_replay/
COPY verdict_writer/ ./verdict_writer/

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import httpx; httpx.get('http://localhost:8080/health').raise_for_status()"

CMD ["uvicorn", "orchestrator.main:app", "--host", "0.0.0.0", "--port", "8080"]
