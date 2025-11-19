import os
from typing import List, Optional, Dict, Any
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from database import db, create_document, get_documents

app = FastAPI(title="AutoDiag API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class DiagnoseRequest(BaseModel):
    name: str = Field(..., description="Car make/brand")
    model: str = Field(..., description="Car model + year")
    fault_code: Optional[str] = Field(None, description="OBD-II fault code like P0300")
    description: str = Field(..., description="Description of the problem")


class Suggestion(BaseModel):
    part: str
    likelihood: float = Field(..., ge=0, le=1)
    reason: str


class DiagnoseResponse(BaseModel):
    suggestions: List[Suggestion]
    id: Optional[str] = None


@app.get("/")
def read_root():
    return {"message": "AutoDiag backend running"}


@app.get("/test")
def test_database():
    response = {
        "backend": "✅ Running",
        "database": "❌ Not Available",
        "database_url": None,
        "database_name": None,
        "connection_status": "Not Connected",
        "collections": []
    }

    try:
        if db is not None:
            response["database"] = "✅ Available"
            response["database_url"] = "✅ Configured"
            response["database_name"] = db.name if hasattr(db, 'name') else "✅ Connected"
            response["connection_status"] = "Connected"
            try:
                collections = db.list_collection_names()
                response["collections"] = collections[:10]
                response["database"] = "✅ Connected & Working"
            except Exception as e:
                response["database"] = f"⚠️  Connected but Error: {str(e)[:50]}"
        else:
            response["database"] = "⚠️  Available but not initialized"
    except Exception as e:
        response["database"] = f"❌ Error: {str(e)[:50]}"

    response["database_url"] = "✅ Set" if os.getenv("DATABASE_URL") else "❌ Not Set"
    response["database_name"] = "✅ Set" if os.getenv("DATABASE_NAME") else "❌ Not Set"

    return response


# Heuristic knowledge base mapping fault code prefixes and keywords to likely parts
FAULT_CODE_MAP: Dict[str, List[Dict[str, Any]]] = {
    "P030": [
        {"part": "Spark Plugs", "base": 0.6, "reason": "Engine misfire detected"},
        {"part": "Ignition Coils", "base": 0.5, "reason": "Weak/no spark"},
        {"part": "Fuel Injectors", "base": 0.35, "reason": "Fuel delivery issues"},
        {"part": "Vacuum Leak", "base": 0.25, "reason": "Unmetered air causing lean"},
    ],
    "P017": [
        {"part": "O2 Sensor (Upstream)", "base": 0.5, "reason": "Fuel trim lean"},
        {"part": "MAF Sensor", "base": 0.45, "reason": "Airflow reading incorrect"},
        {"part": "Vacuum Leak", "base": 0.4, "reason": "Extra air entering intake"},
        {"part": "Fuel Pump/Filter", "base": 0.3, "reason": "Low fuel pressure"},
    ],
    "P042": [
        {"part": "Catalytic Converter", "base": 0.6, "reason": "Efficiency below threshold"},
        {"part": "O2 Sensor (Downstream)", "base": 0.4, "reason": "Sensor aging or slow"},
        {"part": "Exhaust Leak", "base": 0.25, "reason": "False oxygen readings"},
    ],
    "P012": [
        {"part": "Throttle Position Sensor", "base": 0.5, "reason": "TPS circuit issues"},
        {"part": "Wiring/Connector", "base": 0.35, "reason": "Signal interruption"},
    ],
}

KEYWORD_HINTS: List[Dict[str, Any]] = [
    {"keywords": ["rough idle", "shakes", "vibration"], "part": "Spark Plugs", "boost": 0.15},
    {"keywords": ["stalls", "dies", "no start"], "part": "Fuel Pump", "boost": 0.2},
    {"keywords": ["hesitation", "lag", "surge"], "part": "MAF Sensor", "boost": 0.12},
    {"keywords": ["rotten egg", "sulfur"], "part": "Catalytic Converter", "boost": 0.18},
    {"keywords": ["whistle", "hiss"], "part": "Vacuum Leak", "boost": 0.15},
]


@app.post("/api/diagnose", response_model=DiagnoseResponse)
def diagnose(req: DiagnoseRequest):
    # Base suggestions from fault code prefix (first 4 chars to capture families like P0300-P0306)
    suggestions: List[Dict[str, Any]] = []

    prefix = None
    if req.fault_code:
        clean = req.fault_code.strip().upper()
        prefix = clean[:4] if len(clean) >= 4 else clean
        for key, items in FAULT_CODE_MAP.items():
            if prefix.startswith(key):
                suggestions.extend(items)

    # If no direct code match, seed with common issues
    if not suggestions:
        suggestions = [
            {"part": "Battery", "base": 0.25, "reason": "Common electrical issue"},
            {"part": "Alternator", "base": 0.2, "reason": "Charging system faults"},
            {"part": "Spark Plugs", "base": 0.18, "reason": "Wear item, causes misfires"},
        ]

    # Apply keyword boosts from description
    desc = req.description.lower()
    for hint in KEYWORD_HINTS:
        if any(k in desc for k in hint["keywords"]):
            for s in suggestions:
                if s["part"].lower().startswith(hint["part"].lower().split()[0]):
                    s["base"] += hint["boost"]

    # Normalize, cap between 0 and 1, sort descending
    total = sum(max(0.0, min(1.0, s["base"])) for s in suggestions)
    if total == 0:
        total = 1.0

    ranked = []
    for s in suggestions:
        score = max(0.0, min(1.0, s["base"])) / total
        ranked.append({
            "part": s["part"],
            "likelihood": round(score, 3),
            "reason": s["reason"] + (f" (matched {prefix})" if prefix else "")
        })

    # Sort and keep top 5
    ranked.sort(key=lambda x: x["likelihood"], reverse=True)
    top = ranked[:5]

    # Save to DB
    try:
        doc_id = create_document("carissue", {
            "name": req.name,
            "model": req.model,
            "fault_code": req.fault_code,
            "description": req.description,
            "suggestions": top,
        })
    except Exception:
        doc_id = None

    return DiagnoseResponse(suggestions=[Suggestion(**s) for s in top], id=doc_id)


@app.get("/api/history")
def history(limit: int = 20):
    try:
        items = get_documents("carissue", {}, limit=limit)
        # Convert ObjectId if present
        for it in items:
            if "_id" in it:
                it["_id"] = str(it["_id"])
        return {"items": items}
    except Exception:
        return {"items": []}


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
