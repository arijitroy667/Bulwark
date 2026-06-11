import asyncio
import os
from datetime import datetime, timezone
from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv
import httpx
import urllib.parse

# Load env variables from .env file safely
load_dotenv()

MONGODB_URI = os.getenv("MONGODB_URI", "mongodb+srv://arix666phoenix_db_user:Y4C1Xvy1uhjDinW4@cluster0.nqntnpm.mongodb.net/?appName=Cluster0")
DB_NAME = os.getenv("MONGODB_DB_NAME", "Bulwark")
GITLAB_URL = os.getenv("GITLAB_URL", "https://gitlab.com")
GITLAB_PAT = os.getenv("GITLAB_PAT", "")

async def post_warning_comment(project_id: str, mr_id: int):
    if not GITLAB_PAT or not project_id:
        print("  Skipping GitLab comment (missing PAT or project_id).")
        return
    encoded_project = urllib.parse.quote(project_id, safe="")
    url = f"{GITLAB_URL.rstrip('/')}/api/v4/projects/{encoded_project}/merge_requests/{mr_id}/notes"
    body = (
        "## 🛡️ Bulwark Simulation Warning\n\n"
        "**Verdict: BLOCKED** ❌\n\n"
        "This MR introduces a significant latency regression (p95 +187ms) compared to the production baseline. "
        "The error rate also increased by 0.06%. The novel `ConnectionPool` timeout aligns with a loop-per-record pattern visible in the diff.\n\n"
        "⚠️ *Note: We will not block the merge button, but this should not be merged until the regression is fixed.*"
    )
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.post(url, headers={"PRIVATE-TOKEN": GITLAB_PAT}, json={"body": body})
            resp.raise_for_status()
            print(f"  [{project_id}!{mr_id}] Posted warning comment to GitLab.")
        except Exception as e:
            print(f"  Failed to post comment to GitLab: {e}")

async def mock_execution(db, sim_id, project_id, mr_id):
    print(f"Intercepting simulation {sim_id} for MR {mr_id}...")
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
        print(f"  [{sim_id}] Setting status to: {step}")
        await db.simulations.update_one(
            {"_id": sim_id},
            {"$set": {"status": step, "error_log": None}}
        )
        await asyncio.sleep(7.5)
        
    print(f"  [{sim_id}] Setting final verdict (BLOCKED)")
    completed_data = {
        "status": "completed",
        "error_log": None,
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
    await db.simulations.update_one(
        {"_id": sim_id},
        {"$set": completed_data}
    )
    print(f"Mock execution for {sim_id} completed successfully in DB.")
    
    # Post the comment to GitLab
    if mr_id and mr_id != "Unknown":
        await post_warning_comment(project_id, int(mr_id))

async def main():
    client = AsyncIOMotorClient(MONGODB_URI)
    db = client[DB_NAME]
    print(f"Connected to MongoDB Atlas: {MONGODB_URI[:30]}...")
    print(f"Watching for new MRs to intercept for the demo...")
    
    processed_sims = set()
    
    start_time = datetime.now(timezone.utc)
    
    while True:
        cursor = db.simulations.find({"created_at": {"$gte": start_time}})
        simulations = await cursor.to_list(length=100)
        
        for sim in simulations:
            sim_id = sim["_id"]
            if sim_id not in processed_sims:
                processed_sims.add(sim_id)
                project_id = sim.get("project_id", "")
                asyncio.create_task(mock_execution(db, sim_id, project_id, sim.get("mr_id", "Unknown")))
                
        await asyncio.sleep(1)

if __name__ == "__main__":
    asyncio.run(main())
