import time
START = time.time()
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from fastapi import FastAPI, Request
from pydantic import BaseModel
from composer import build_tick_actions, handle_reply, note_context_push

app = FastAPI(title="Vera Message Engine", version="1.0.0")

# In-memory storage: { (scope, context_id): {"version": int, "payload": dict} }
STORE: Dict[tuple, Dict[str, Any]] = {}

class ContextRequest(BaseModel):
    scope: str
    context_id: str
    version: int
    payload: Dict[str, Any]
    delivered_at: Optional[str] = None

class TickRequest(BaseModel):
    tick_id: Optional[str] = None
    timestamp: Optional[str] = None

class ReplyRequest(BaseModel):
    conversation_id: str
    sender_id: str
    message: str
    context_id: Optional[str] = None

@app.get("/v1/healthz")
async def healthz():
    counts = {"category": 0, "merchant": 0, "customer": 0, "trigger": 0}
    for (scope, _) in STORE.keys():
        counts[scope] = counts.get(scope, 0) + 1
    return {"status": "ok",
            "uptime_seconds": int(time.time() - START),
            "contexts_loaded": counts}

@app.get("/v1/metadata")
async def metadata():
    return {
        "team_name": "Shourya Kumawat",
        "team_members": ["Shourya Kumawat"],
        "model": "deterministic-composer",
        "approach": "rule-based composer with per-trigger playbooks",
        "contact_email": "shouryakumawat_23en061@dtu.ac.in",
        "version": "1.0.0",
        "submitted_at": "2026-09-27T00:00:00Z",
    }

@app.post("/v1/context")
async def push_context(request: Request):
    data = await request.json()
    note_context_push(data["scope"], data["version"])
    key = (data["scope"], data["context_id"])
    version = data["version"]
    cur = STORE.get(key)
    if cur and cur["version"] > version:
        return {"accepted": False, "reason": "stale_version",
                "current_version": cur["version"]}
    STORE[key] = {"version": version, "payload": data["payload"]}
    return {"accepted": True, "ack_id": f"ack_{data['context_id']}_v{version}",
            "stored_at": datetime.utcnow().isoformat() + "Z"}

@app.post("/v1/tick")
async def tick(request: Request):
    data = await request.json()
    return {"actions": build_tick_actions(data.get("available_triggers", []), STORE)}

@app.post("/v1/reply")
async def reply(request: Request):
    data = await request.json()
    return handle_reply(
        data.get("conversation_id"),
        data.get("merchant_id") or data.get("sender_id"),
        data.get("message"),
        data.get("turn_number", 1),
    )