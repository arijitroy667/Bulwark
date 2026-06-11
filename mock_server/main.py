import asyncio
import json
from typing import AsyncGenerator
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(title="Mock Bulwark Server")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class SetupRequest(BaseModel):
    pat: str
    project_id: str
    webhook_url: str

@app.post("/api/setup")
async def setup(req: SetupRequest):
    return {"status": "success", "message": "Webhook configured"}

@app.get("/api/simulations")
async def get_simulations():
    return {
        "data": [
            {
                "_id": "sim_demo_123",
                "mr_id": 6,
                "service_name": "default",
                "created_at": "2026-06-11T17:00:00Z",
                "status": "queued",
                "head_sha": "42029195"
            }
        ]
    }

async def generate_simulation_stream() -> AsyncGenerator[str, None]:
    base_data = {
        "_id": "sim_demo_123",
        "mr_id": 6,
        "service_name": "default",
        "head_sha": "42029195a1b2c3d4",
        "error_log": None,
        "verdict": None,
    }
    
    yield f"data: {json.dumps({**base_data, 'status': 'queued'})}\n\n"
    await asyncio.sleep(1)

    steps = [
        "ingesting",
        "fetching_baseline",
        "history",
        "provisioning",
        "replaying",
        "evaluating",
        "teardown",
        "reasoning",
        "enforcing"
    ]
    
    for step in steps:
        yield f"data: {json.dumps({**base_data, 'status': step})}\n\n"
        await asyncio.sleep(2.5)  # Make it slow enough to be read on video
        
    completed_data = {
        **base_data,
        "status": "completed",
        "verdict": {
            "verdict": "blocked",
            "reasoning": "The p95 latency regression of +187ms is 5.3x the measurement noise floor of ±35ms — high-confidence regression signal. The novel ConnectionPool timeout aligns with the loop-per-record pattern visible in the diff."
        },
        "dynatrace": {
            "replay_result": {
                "p95_latency_ms": 329.0,
                "latency_delta_p95_ms": 187.0,
                "error_rate_percent": 0.18,
                "error_rate_delta_percent": 0.06
            }
        }
    }
    yield f"data: {json.dumps(completed_data)}\n\n"

@app.get("/api/simulation/{sim_id}/stream")
async def stream_simulation(sim_id: str):
    return StreamingResponse(
        generate_simulation_stream(), 
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive"
        }
    )

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000)
