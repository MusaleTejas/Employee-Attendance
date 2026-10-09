"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB  (a local .env is loaded for convenience)
"""
import os
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError
from pydantic import BaseModel, Field, field_validator, model_validator

load_dotenv()

# ---------------------------------------------------------------------------
# Database setup
# ---------------------------------------------------------------------------
mongo_uri = os.getenv("MONGO_URI") or os.getenv("MONGODB_URI", "mongodb://localhost:27017")
client = MongoClient(mongo_uri)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))


def create_indexes() -> None:
    """Create required indexes idempotently at startup."""
    # Unique index on emp_code (also enforces concurrent duplicate creates → 409)
    db.employees.create_index([("emp_code", ASCENDING)], unique=True)
    # For list_employees sorted by emp_code, filtered by department
    db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)])
    # Compound unique index for attendance records; also handles concurrent punch-ins
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)], unique=True
    )
    # For list_attendance sort (date desc, emp_code asc) with optional filters
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)]
    )
    # For analytics queries filtered by date prefix (month-based)
    db.attendance_logs.create_index(
        [("date", ASCENDING), ("emp_code", ASCENDING)]
    )


create_indexes()

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
PRESENCE_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}
ALL_STATUSES = {"PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"}

# Epoch ms range that identifies a real instant (not seconds)
EPOCH_MS_MIN = 100_000_000_000
EPOCH_MS_MAX = 4_102_444_800_000


# ---------------------------------------------------------------------------
# Rounding helpers
# ---------------------------------------------------------------------------
def round_half_up(value: float, decimals: int) -> float:
    """Round a float using ROUND_HALF_UP to the given decimal places."""
    if value is None:
        return None
    q = Decimal(10) ** -decimals
    return float(Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP))


# ---------------------------------------------------------------------------
# Time / date helpers
# ---------------------------------------------------------------------------
def ms_to_dt(ms: int) -> datetime:
    """Convert epoch milliseconds to a UTC-aware datetime, truncated to seconds."""
    # Truncate to whole seconds first (R1)
    sec = ms // 1000
    return datetime.fromtimestamp(sec, tz=timezone.utc)


def dt_to_ms(dt: datetime) -> Optional[int]:
    """Convert a datetime (aware or naive UTC) to epoch milliseconds."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def dt_to_ist(dt: datetime) -> datetime:
    """Convert a UTC datetime to IST."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST)


def attendance_date(punch_in_utc: datetime, shift_start: str, shift_end: str) -> str:
    """
    Compute the attendance date string (YYYY-MM-DD) per R1.

    For overnight shifts (shift_end <= shift_start): if the IST time of punch_in
    is strictly earlier than shift_end, the punch-in belongs to the shift that
    started the previous day, so the attendance date is yesterday (IST).
    Otherwise, the attendance date is today (IST).
    """
    ist_dt = dt_to_ist(punch_in_utc)
    ist_time_str = ist_dt.strftime("%H:%M")
    shift_is_overnight = shift_end <= shift_start

    if shift_is_overnight and ist_time_str < shift_end:
        # This punch-in is in the tail of yesterday's overnight shift
        date_str = (ist_dt - timedelta(days=1)).strftime("%Y-%m-%d")
    else:
        date_str = ist_dt.strftime("%Y-%m-%d")
    return date_str


# ---------------------------------------------------------------------------
# Business-rule computations
# ---------------------------------------------------------------------------
def compute_late_minutes(punch_in: datetime, shift_start: str) -> int:
    """
    R2: Grace period is 10 minutes. Late only if strictly more than 10:00 after
    shift_start. late_minutes = floor minutes since shift_start (not since end of grace).
    Example (09:30 shift): 09:40:00 → 0; 09:40:01 → 10; 10:15:59 → 45.
    """
    ist = dt_to_ist(punch_in)
    h, m = map(int, shift_start.split(":"))
    shift_start_dt = ist.replace(hour=h, minute=m, second=0, microsecond=0)
    diff_seconds = (ist - shift_start_dt).total_seconds()
    if diff_seconds <= 600:  # 10 minutes = 600 seconds
        return 0
    return int(diff_seconds // 60)


def compute_overtime_minutes(punch_out: datetime, shift_end: str) -> int:
    """
    R3: overtime_minutes = floor minutes between shift_end and punch_out,
    counted only when >= 30; otherwise 0.
    """
    ist = dt_to_ist(punch_out)
    h, m = map(int, shift_end.split(":"))
    shift_end_dt = ist.replace(hour=h, minute=m, second=0, microsecond=0)
    # For overnight shifts, shift_end is on the next day relative to shift_start,
    # but punch_out IST date may already be that next day - just compare as-is.
    diff_seconds = (ist - shift_end_dt).total_seconds()
    if diff_seconds < 1800:  # less than 30 minutes
        return 0
    return int(diff_seconds // 60)


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up."""
    seconds = (punch_out - punch_in).total_seconds()
    return round_half_up(seconds / 3600, 2)


def compute_derived(punch_in: Optional[datetime], punch_out: Optional[datetime],
                    shift_start: str, shift_end: str) -> dict:
    """
    Compute all derived fields for a record.
    Returns a dict with late_minutes, work_hours, overtime_minutes, half_day.
    """
    if punch_in is None:
        return {
            "late_minutes": 0,
            "work_hours": None,
            "overtime_minutes": 0,
            "half_day": False,
        }
    late_minutes = compute_late_minutes(punch_in, shift_start)
    if punch_out is None:
        return {
            "late_minutes": late_minutes,
            "work_hours": None,
            "overtime_minutes": 0,
            "half_day": False,
        }
    work_hours = compute_work_hours(punch_in, punch_out)
    overtime_minutes = compute_overtime_minutes(punch_out, shift_end)
    half_day = work_hours < 4.50  # R5: rounded work_hours < 4.50
    return {
        "late_minutes": late_minutes,
        "work_hours": work_hours,
        "overtime_minutes": overtime_minutes,
        "half_day": half_day,
    }


# ---------------------------------------------------------------------------
# Response serialization helpers
# ---------------------------------------------------------------------------
def serialize_record(doc: dict) -> dict:
    """
    Convert a raw MongoDB attendance_log document to the API response shape.
    Timestamps stored as BSON datetimes are converted to epoch milliseconds.
    history entries' punch_in/punch_out from/to are also converted.
    """
    out = {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_ms(doc.get("punch_in")),
        "punch_out": dt_to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": [],
    }
    for entry in doc.get("history", []):
        changes = {}
        for field, change in entry.get("changes", {}).items():
            if field in ("punch_in", "punch_out"):
                changes[field] = {
                    "from": dt_to_ms(change.get("from")),
                    "to": dt_to_ms(change.get("to")),
                }
            else:
                changes[field] = {"from": change.get("from"), "to": change.get("to")}
        out["history"].append({
            "at": dt_to_ms(entry["at"]),
            "by": entry["by"],
            "reason": entry["reason"],
            "changes": changes,
        })
    return out


def serialize_employee(doc: dict) -> dict:
    """Convert a raw MongoDB employee document to the API response shape."""
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": dt_to_ms(doc["created_at"]),
    }


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------
class EpochMillisInt(int):
    """Integer epoch milliseconds. Rejects values below 100 billion (seconds-range)."""

    @classmethod
    def __get_validators__(cls):
        yield cls.validate

    @classmethod
    def validate(cls, v):
        if not isinstance(v, int) or isinstance(v, bool):
            raise ValueError("must be an integer")
        if v < EPOCH_MS_MIN or v > EPOCH_MS_MAX:
            raise ValueError(
                f"epoch ms must be between {EPOCH_MS_MIN} and {EPOCH_MS_MAX}"
            )
        return v


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str  # YYYY-MM-DD

    @model_validator(mode="after")
    def shift_start_ne_shift_end(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"

    @field_validator("punched_at", mode="before")
    @classmethod
    def validate_punched_at(cls, v):
        if v is None:
            return v
        if not isinstance(v, int) or isinstance(v, bool):
            raise ValueError("punched_at must be an integer epoch milliseconds")
        if v < EPOCH_MS_MIN or v > EPOCH_MS_MAX:
            raise ValueError(
                f"punched_at must be between {EPOCH_MS_MIN} and {EPOCH_MS_MAX}"
            )
        return v

    @field_validator("status", mode="before")
    @classmethod
    def validate_status(cls, v):
        if v not in PRESENCE_STATUSES:
            raise ValueError(f"status must be one of {sorted(PRESENCE_STATUSES)}")
        return v


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None

    @field_validator("punched_at", mode="before")
    @classmethod
    def validate_punched_at(cls, v):
        if v is None:
            return v
        if not isinstance(v, int) or isinstance(v, bool):
            raise ValueError("punched_at must be an integer epoch milliseconds")
        if v < EPOCH_MS_MIN or v > EPOCH_MS_MAX:
            raise ValueError(
                f"punched_at must be between {EPOCH_MS_MIN} and {EPOCH_MS_MAX}"
            )
        return v


class RegularizeIn(BaseModel):
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)
    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None

    @field_validator("status", mode="before")
    @classmethod
    def validate_status(cls, v):
        if v is None:
            return v
        if v not in ALL_STATUSES:
            raise ValueError(f"status must be one of {sorted(ALL_STATUSES)}")
        return v

    @field_validator("punch_in", "punch_out", mode="before")
    @classmethod
    def validate_epoch_ms(cls, v):
        if v is None:
            return v
        if not isinstance(v, int) or isinstance(v, bool):
            raise ValueError("must be an integer epoch milliseconds")
        if v < EPOCH_MS_MIN or v > EPOCH_MS_MAX:
            raise ValueError(
                f"epoch ms must be between {EPOCH_MS_MIN} and {EPOCH_MS_MAX}"
            )
        return v


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def index_ui():
    """Clean interactive landing portal for the Employee Attendance API."""
    return """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Employee Attendance & Analytics</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
    <style>
        :root {
            --primary: #4f46e5;
            --primary-hover: #4338ca;
            --bg: #090d16;
            --card: rgba(17, 24, 39, 0.7);
            --border: rgba(255, 255, 255, 0.08);
            --text-main: #f8fafc;
            --text-sub: #94a3b8;
            --accent: #10b981;
            --badge-bg: rgba(79, 70, 229, 0.15);
        }
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: 'Plus Jakarta Sans', -apple-system, sans-serif;
            background-color: var(--bg);
            background-image: 
                radial-gradient(at 0% 0%, rgba(79, 70, 229, 0.2) 0px, transparent 50%),
                radial-gradient(at 100% 100%, rgba(16, 185, 129, 0.12) 0px, transparent 50%);
            color: var(--text-main);
            min-height: 100vh;
            padding: 2.5rem 1.5rem;
            display: flex;
            justify-content: center;
        }
        .container {
            width: 100%;
            max-width: 1080px;
        }
        header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 2.5rem;
            flex-wrap: wrap;
            gap: 1rem;
        }
        .badge {
            background: var(--badge-bg);
            color: #818cf8;
            padding: 0.35rem 0.85rem;
            border-radius: 9999px;
            font-size: 0.75rem;
            font-weight: 700;
            letter-spacing: 0.05em;
            text-transform: uppercase;
            border: 1px solid rgba(129, 140, 248, 0.25);
            display: inline-flex;
            align-items: center;
            gap: 0.4rem;
        }
        .badge-dot {
            width: 6px;
            height: 6px;
            background: var(--accent);
            border-radius: 50%;
            box-shadow: 0 0 8px var(--accent);
        }
        h1 {
            font-size: 2.25rem;
            font-weight: 800;
            letter-spacing: -0.03em;
            margin-top: 0.5rem;
            background: linear-gradient(to right, #ffffff, #cbd5e1);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }
        p.subtitle {
            color: var(--text-sub);
            font-size: 0.95rem;
            margin-top: 0.35rem;
        }
        .nav-links {
            display: flex;
            gap: 0.75rem;
        }
        .btn {
            padding: 0.65rem 1.25rem;
            border-radius: 0.65rem;
            font-size: 0.875rem;
            font-weight: 600;
            text-decoration: none;
            transition: all 0.2s ease;
            display: inline-flex;
            align-items: center;
            gap: 0.5rem;
        }
        .btn-primary {
            background: linear-gradient(135deg, #6366f1, #4f46e5);
            color: #fff;
            box-shadow: 0 4px 14px rgba(79, 70, 229, 0.35);
        }
        .btn-primary:hover {
            transform: translateY(-1px);
            box-shadow: 0 6px 20px rgba(79, 70, 229, 0.45);
        }
        .btn-secondary {
            background: rgba(255, 255, 255, 0.05);
            color: var(--text-main);
            border: 1px solid var(--border);
        }
        .btn-secondary:hover {
            background: rgba(255, 255, 255, 0.1);
        }
        .grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
            gap: 1.25rem;
            margin-bottom: 2.5rem;
        }
        .card {
            background: var(--card);
            backdrop-filter: blur(16px);
            border: 1px solid var(--border);
            border-radius: 1rem;
            padding: 1.5rem;
            transition: transform 0.2s, border-color 0.2s;
        }
        .card:hover {
            border-color: rgba(99, 102, 241, 0.35);
            transform: translateY(-2px);
        }
        .card-header {
            display: flex;
            align-items: center;
            gap: 0.75rem;
            margin-bottom: 1rem;
        }
        .icon {
            width: 38px;
            height: 38px;
            background: rgba(255, 255, 255, 0.05);
            border-radius: 0.5rem;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 1.25rem;
            border: 1px solid var(--border);
        }
        .card-title {
            font-size: 1.1rem;
            font-weight: 700;
        }
        .endpoint-item {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 0.6rem 0.75rem;
            background: rgba(0, 0, 0, 0.25);
            border-radius: 0.5rem;
            margin-bottom: 0.5rem;
            font-family: 'JetBrains Mono', monospace;
            font-size: 0.8rem;
            border: 1px solid rgba(255, 255, 255, 0.03);
        }
        .method {
            padding: 0.2rem 0.5rem;
            border-radius: 0.35rem;
            font-size: 0.7rem;
            font-weight: 700;
        }
        .method-get { background: rgba(16, 185, 129, 0.15); color: #34d399; }
        .method-post { background: rgba(59, 130, 246, 0.15); color: #60a5fa; }
        .method-patch { background: rgba(245, 158, 11, 0.15); color: #fbbf24; }
        .path { color: #e2e8f0; text-decoration: none; }
        .path:hover { color: #818cf8; text-decoration: underline; }
        .metrics-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 1rem;
            margin-bottom: 2rem;
        }
        .metric-card {
            background: rgba(255, 255, 255, 0.02);
            border: 1px solid var(--border);
            border-radius: 0.75rem;
            padding: 1.25rem;
            text-align: center;
        }
        .metric-value {
            font-size: 1.75rem;
            font-weight: 800;
            color: #fff;
            margin-top: 0.25rem;
        }
        .metric-label {
            font-size: 0.75rem;
            text-transform: uppercase;
            letter-spacing: 0.05em;
            color: var(--text-sub);
        }
        footer {
            text-align: center;
            color: var(--text-sub);
            font-size: 0.85rem;
            padding-top: 1.5rem;
            border-top: 1px solid var(--border);
        }
    </style>
</head>
<body>
    <div class="container">
        <header>
            <div>
                <span class="badge"><span class="badge-dot"></span> System Live & Connected</span>
                <h1>Employee Attendance & Analytics</h1>
                <p class="subtitle">FastAPI attendance engine backed by MongoDB Atlas aggregation pipelines</p>
            </div>
            <div class="nav-links">
                <a href="/docs" class="btn btn-primary">⚡ Open Swagger Docs</a>
                <a href="/redoc" class="btn btn-secondary">📖 View ReDoc</a>
                <a href="/health" class="btn btn-secondary" target="_blank">💚 Health</a>
            </div>
        </header>

        <div class="metrics-grid">
            <div class="metric-card">
                <div class="metric-label">API Status</div>
                <div class="metric-value" style="color: #34d399;">Ready 200</div>
            </div>
            <div class="metric-card">
                <div class="metric-label">Database Engine</div>
                <div class="metric-value">MongoDB Atlas</div>
            </div>
            <div class="metric-card">
                <div class="metric-label">Timezone Calculation</div>
                <div class="metric-value">IST (UTC+5:30)</div>
            </div>
            <div class="metric-card">
                <div class="metric-label">Audit Regularization</div>
                <div class="metric-value">Active (R1-R10)</div>
            </div>
        </div>

        <div class="grid">
            <div class="card">
                <div class="card-header">
                    <div class="icon">👥</div>
                    <div>
                        <div class="card-title">Employee Management</div>
                        <div style="font-size: 0.8rem; color: var(--text-sub);">Staff registry & lookups</div>
                    </div>
                </div>
                <div class="endpoint-item">
                    <a class="path" href="/employees" target="_blank">/employees</a>
                    <span class="method method-get">GET</span>
                </div>
                <div class="endpoint-item">
                    <span class="path">/employees</span>
                    <span class="method method-post">POST</span>
                </div>
            </div>

            <div class="card">
                <div class="card-header">
                    <div class="icon">⏱️</div>
                    <div>
                        <div class="card-title">Punch & Regularization</div>
                        <div style="font-size: 0.8rem; color: var(--text-sub);">Check-ins, check-outs & audits</div>
                    </div>
                </div>
                <div class="endpoint-item">
                    <span class="path">/attendance/punch-in</span>
                    <span class="method method-post">POST</span>
                </div>
                <div class="endpoint-item">
                    <span class="path">/attendance/punch-out</span>
                    <span class="method method-post">POST</span>
                </div>
                <div class="endpoint-item">
                    <a class="path" href="/attendance" target="_blank">/attendance</a>
                    <span class="method method-get">GET</span>
                </div>
                <div class="endpoint-item">
                    <span class="path">/attendance/{emp_code}/{date}</span>
                    <span class="method method-patch">PATCH</span>
                </div>
            </div>

            <div class="card">
                <div class="card-header">
                    <div class="icon">📊</div>
                    <div>
                        <div class="card-title">Analytics & Aggregations</div>
                        <div style="font-size: 0.8rem; color: var(--text-sub);">MongoDB pipelines with zero-log handling</div>
                    </div>
                </div>
                <div class="endpoint-item">
                    <a class="path" href="/analytics/departments/summary?month=2026-07" target="_blank">/analytics/departments/summary</a>
                    <span class="method method-get">GET</span>
                </div>
                <div class="endpoint-item">
                    <a class="path" href="/analytics/leaderboard/late?month=2026-07" target="_blank">/analytics/leaderboard/late</a>
                    <span class="method method-get">GET</span>
                </div>
                <div class="endpoint-item">
                    <a class="path" href="/analytics/employees/EMP0001/monthly?month=2026-07" target="_blank">/analytics/employees/{code}/monthly</a>
                    <span class="method method-get">GET</span>
                </div>
                <div class="endpoint-item">
                    <a class="path" href="/analytics/departments/Engineering/trend?from=2026-07-06&to=2026-07-10" target="_blank">/analytics/departments/{dept}/trend</a>
                    <span class="method method-get">GET</span>
                </div>
            </div>
        </div>

        <footer>
            Crafted for Employee Attendance & Analytics Evaluation &bull; Powered by FastAPI & MongoDB
        </footer>
    </div>
</body>
</html>"""


@app.get("/health")
def health():
    """R: Returns 200 if MongoDB answers a ping, otherwise 503."""
    try:
        db.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="MongoDB unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    """Create a new employee. emp_code must be unique (enforced by DB unique index)."""
    doc = body.model_dump()
    doc["created_at"] = datetime.now(tz=timezone.utc)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")
    doc.pop("_id", None)
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """List employees sorted by emp_code ascending. total reflects the filtered set."""
    q: dict[str, Any] = {}
    if department:
        q["department"] = department
    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    items = list(
        db.employees.find(q, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip(skip)
        .limit(page_size)
    )
    return {
        "items": [serialize_employee(e) for e in items],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    """
    Record a punch-in. Uses insert with unique index to atomically prevent duplicates.
    """
    # Resolve employee
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    # Resolve punch-in time (truncated to seconds per R1)
    if body.punched_at is not None:
        punch_in_dt = ms_to_dt(body.punched_at)
    else:
        now_sec = int(datetime.now(tz=timezone.utc).timestamp())
        punch_in_dt = datetime.fromtimestamp(now_sec, tz=timezone.utc)

    date_str = attendance_date(punch_in_dt, emp["shift_start"], emp["shift_end"])
    derived = compute_derived(punch_in_dt, None, emp["shift_start"], emp["shift_end"])

    doc = {
        "emp_code": body.emp_code,
        "date": date_str,
        "status": body.status,
        "punch_in": punch_in_dt,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": derived["late_minutes"],
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409, detail="Already punched in for this date"
        )

    doc.pop("_id", None)
    return serialize_record(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    """
    Record a punch-out. Finds the most recent open record whose punch_in <= punched_at.
    Uses find_one_and_update with a version check to prevent duplicate punch-outs.
    """
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    # Resolve punch-out time (truncated to seconds per R1)
    if body.punched_at is not None:
        punch_out_dt = ms_to_dt(body.punched_at)
    else:
        now_sec = int(datetime.now(tz=timezone.utc).timestamp())
        punch_out_dt = datetime.fromtimestamp(now_sec, tz=timezone.utc)

    # Find the most recent open record with punch_in <= punch_out (for overnight shifts)
    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$ne": None, "$lte": punch_out_dt},
            "punch_out": None,
        },
        sort=[("punch_in", DESCENDING)],
    )
    if not record:
        # Check if there's a record that's already punched out (for 409)
        any_record = db.attendance_logs.find_one(
            {
                "emp_code": body.emp_code,
                "punch_in": {"$ne": None, "$lte": punch_out_dt},
            },
            sort=[("punch_in", DESCENDING)],
        )
        if any_record and any_record.get("punch_out") is not None:
            raise HTTPException(
                status_code=409, detail="Record is already punched out"
            )
        raise HTTPException(
            status_code=404, detail="No open punch-in found for this employee"
        )

    punch_in_dt = record["punch_in"]
    if punch_in_dt.tzinfo is None:
        punch_in_dt = punch_in_dt.replace(tzinfo=timezone.utc)

    # Validate: punch_out must be after punch_in and within 24 hours
    diff = (punch_out_dt - punch_in_dt).total_seconds()
    if diff <= 0:
        raise HTTPException(
            status_code=422, detail="punched_at must be after punch_in"
        )
    if diff > 86400:
        raise HTTPException(
            status_code=422, detail="punched_at must be within 24 hours of punch_in"
        )

    derived = compute_derived(
        punch_in_dt, punch_out_dt, emp["shift_start"], emp["shift_end"]
    )

    # Atomically update only if punch_out is still null (concurrent race → 409)
    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": punch_out_dt,
                "work_hours": derived["work_hours"],
                "overtime_minutes": derived["overtime_minutes"],
                "half_day": derived["half_day"],
            }
        },
        return_document=True,
    )
    if updated is None:
        raise HTTPException(
            status_code=409, detail="Record is already punched out"
        )

    updated.pop("_id", None)
    return serialize_record(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """List attendance records sorted by date desc then emp_code asc."""
    if date_from and date_to and date_from > date_to:
        raise HTTPException(
            status_code=422,
            detail=[{"msg": "date_from must not be greater than date_to"}],
        )
    if status and status not in ALL_STATUSES:
        raise HTTPException(
            status_code=422,
            detail=[{"msg": f"status must be one of {sorted(ALL_STATUSES)}"}],
        )

    q: dict[str, Any] = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    docs = list(
        db.attendance_logs.find(q)
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip(skip)
        .limit(page_size)
    )
    return {
        "items": [serialize_record(d) for d in docs],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    """
    Manually correct one day's attendance record.
    Appends one audit history entry; never overwrites existing entries.
    """
    # Validate employee exists
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    # Validate record exists
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(
            status_code=404, detail="No attendance record for this employee and date"
        )

    new_status = body.status if body.status is not None else record["status"]

    # Rule: ABSENT/LEAVE clears punch times; supplying punch times with them is 422
    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(
                status_code=422,
                detail="Cannot supply punch_in or punch_out with ABSENT or LEAVE status",
            )
        new_punch_in = None
        new_punch_out = None
    else:
        # Resolve punch times
        if body.punch_in is not None:
            new_punch_in = ms_to_dt(body.punch_in)
        else:
            raw = record.get("punch_in")
            if raw is not None and raw.tzinfo is None:
                raw = raw.replace(tzinfo=timezone.utc)
            new_punch_in = raw

        if body.punch_out is not None:
            new_punch_out = ms_to_dt(body.punch_out)
        else:
            raw = record.get("punch_out")
            if raw is not None and raw.tzinfo is None:
                raw = raw.replace(tzinfo=timezone.utc)
            new_punch_out = raw

        # Presence status requires a punch_in
        if new_punch_in is None:
            raise HTTPException(
                status_code=422,
                detail="Presence status requires a punch_in",
            )

        # punch_in must remain on the attendance date (R1)
        new_punch_in_date = attendance_date(
            new_punch_in, emp["shift_start"], emp["shift_end"]
        )
        if new_punch_in_date != date:
            raise HTTPException(
                status_code=422,
                detail="punch_in must remain on the record's attendance date",
            )

        # If punch_out is provided, validate it
        if new_punch_out is not None:
            diff = (new_punch_out - new_punch_in).total_seconds()
            if diff <= 0:
                raise HTTPException(
                    status_code=422, detail="punch_out must be after punch_in"
                )
            if diff > 86400:
                raise HTTPException(
                    status_code=422,
                    detail="punch_out must be within 24 hours of punch_in",
                )

    # Recompute derived fields
    derived = compute_derived(
        new_punch_in, new_punch_out, emp["shift_start"], emp["shift_end"]
    )

    # Build changes dict — only fields that actually changed
    old_punch_in = record.get("punch_in")
    if old_punch_in is not None and old_punch_in.tzinfo is None:
        old_punch_in = old_punch_in.replace(tzinfo=timezone.utc)
    old_punch_out = record.get("punch_out")
    if old_punch_out is not None and old_punch_out.tzinfo is None:
        old_punch_out = old_punch_out.replace(tzinfo=timezone.utc)

    changes: dict[str, Any] = {}

    if new_status != record["status"]:
        changes["status"] = {"from": record["status"], "to": new_status}

    if new_punch_in != old_punch_in:
        changes["punch_in"] = {"from": old_punch_in, "to": new_punch_in}

    if new_punch_out != old_punch_out:
        changes["punch_out"] = {"from": old_punch_out, "to": new_punch_out}

    if derived["work_hours"] != record.get("work_hours"):
        changes["work_hours"] = {
            "from": record.get("work_hours"),
            "to": derived["work_hours"],
        }

    if derived["late_minutes"] != record.get("late_minutes", 0):
        changes["late_minutes"] = {
            "from": record.get("late_minutes", 0),
            "to": derived["late_minutes"],
        }

    if derived["overtime_minutes"] != record.get("overtime_minutes", 0):
        changes["overtime_minutes"] = {
            "from": record.get("overtime_minutes", 0),
            "to": derived["overtime_minutes"],
        }

    if derived["half_day"] != record.get("half_day", False):
        changes["half_day"] = {
            "from": record.get("half_day", False),
            "to": derived["half_day"],
        }

    if not changes:
        raise HTTPException(
            status_code=422, detail="Request would not change anything"
        )

    # Build the history entry (store punch datetimes as BSON dates in MongoDB)
    history_changes_stored: dict[str, Any] = {}
    for field, change in changes.items():
        history_changes_stored[field] = {"from": change["from"], "to": change["to"]}

    history_entry = {
        "at": datetime.now(tz=timezone.utc),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": history_changes_stored,
    }

    # Atomically update with optimistic concurrency on history length
    # (Concurrent edits with same history length → one wins, one gets 409)
    current_history_len = len(record.get("history", []))
    result = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, current_history_len]},
        },
        {
            "$set": {
                "status": new_status,
                "punch_in": new_punch_in,
                "punch_out": new_punch_out,
                "work_hours": derived["work_hours"],
                "late_minutes": derived["late_minutes"],
                "overtime_minutes": derived["overtime_minutes"],
                "half_day": derived["half_day"],
            },
            "$push": {"history": history_entry},
        },
        return_document=True,
    )

    if result is None:
        raise HTTPException(
            status_code=409, detail="Concurrent edit detected; please retry"
        )

    result.pop("_id", None)
    return serialize_record(result)


# ---------------------------------------------------------------------------
# Analytics helpers
# ---------------------------------------------------------------------------
def month_date_range(month: str) -> tuple[str, str]:
    """Return (first_day, last_day) strings for a YYYY-MM month."""
    year, mon = map(int, month.split("-"))
    first = f"{year}-{mon:02d}-01"
    # last day: go to next month and subtract one day
    if mon == 12:
        last_dt = datetime(year + 1, 1, 1) - timedelta(days=1)
    else:
        last_dt = datetime(year, mon + 1, 1) - timedelta(days=1)
    last = last_dt.strftime("%Y-%m-%d")
    return first, last


def working_days_in_range(start: str, end: str) -> int:
    """Count Mon-Fri days between start and end inclusive (YYYY-MM-DD strings)."""
    from datetime import date
    s = date.fromisoformat(start)
    e = date.fromisoformat(end)
    count = 0
    current = s
    while current <= e:
        if current.weekday() < 5:  # 0=Mon, 4=Fri
            count += 1
        current += timedelta(days=1)
    return count


# ---------------------------------------------------------------------------
# Analytics endpoints
# ---------------------------------------------------------------------------

@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    """Monthly attendance summary for one employee using MongoDB aggregation."""
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    first_day, last_day = month_date_range(month)

    # R7: working days from the later of joined_on and month start, to month end
    joined_on = emp["joined_on"]
    count_from = max(first_day, joined_on)
    working_days = working_days_in_range(count_from, last_day)

    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": first_day, "$lte": last_day},
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", list(PRESENCE_STATUSES)]},
                            {
                                "$cond": [
                                    # half_day counts as 0.5 only on Mon-Fri (R7, R5)
                                    # present_days only counts Mon-Fri records
                                    {
                                        "$and": [
                                            {"$lt": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 7]},
                                            {"$gt": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 1]},
                                        ]
                                    },
                                    {
                                        "$cond": [
                                            {"$eq": [{"$ifNull": ["$half_day", False]}, True]},
                                            0.5,
                                            1,
                                        ]
                                    },
                                    0,
                                ]
                            },
                            0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
                "total_overtime_minutes": {
                    "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                },
            }
        },
    ]

    result = list(db.attendance_logs.aggregate(pipeline))

    if result:
        row = result[0]
        present_days = round_half_up(row["present_days"], 2)
        leave_days = int(row["leave_days"])
        late_count = int(row["late_count"])
        total_late_minutes = int(row["total_late_minutes"])
        total_overtime_minutes = int(row["total_overtime_minutes"])
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late_minutes = 0
        total_overtime_minutes = 0

    if working_days == 0:
        attendance_pct = None
    else:
        attendance_pct = round_half_up(present_days / working_days * 100, 2)

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    """
    Department attendance summary for a month.
    Headcount uses employees.joined_on <= last_day_of_month (R9).
    Employees with zero logs are still counted in headcount.
    """
    first_day, last_day = month_date_range(month)

    # Build headcount per department using employees collection
    emp_filter: dict[str, Any] = {"joined_on": {"$lte": last_day}}
    if department:
        emp_filter["department"] = department

    headcount_pipeline = [
        {"$match": emp_filter},
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "emp_codes": {"$push": "$emp_code"},
            }
        },
    ]
    dept_headcounts = {
        row["_id"]: {"headcount": row["headcount"], "emp_codes": row["emp_codes"]}
        for row in db.employees.aggregate(headcount_pipeline)
    }

    if not dept_headcounts:
        return {"month": month, "items": []}

    # Build attendance aggregation per department for this month
    log_filter: dict[str, Any] = {"date": {"$gte": first_day, "$lte": last_day}}
    if department:
        log_filter["emp_code"] = {"$in": dept_headcounts.get(department, {}).get("emp_codes", [])}

    log_pipeline = [
        {"$match": log_filter},
        # Join to get department
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]

    if department:
        log_pipeline.append({"$match": {"emp.department": department}})

    log_pipeline += [
        {
            "$group": {
                "_id": "$emp.department",
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", list(PRESENCE_STATUSES)]},
                            {
                                "$cond": [
                                    {
                                        "$and": [
                                            {"$lt": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 7]},
                                            {"$gt": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 1]},
                                        ]
                                    },
                                    {
                                        "$cond": [
                                            {"$eq": [{"$ifNull": ["$half_day", False]}, True]},
                                            0.5,
                                            1,
                                        ]
                                    },
                                    0,
                                ]
                            },
                            0,
                        ]
                    }
                },
                "avg_work_hours_sum": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", list(PRESENCE_STATUSES)]},
                                    {"$ne": [{"$ifNull": ["$work_hours", None]}, None]},
                                ]
                            },
                            "$work_hours",
                            0,
                        ]
                    }
                },
                "avg_work_hours_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", list(PRESENCE_STATUSES)]},
                                    {"$ne": [{"$ifNull": ["$work_hours", None]}, None]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]
                    }
                },
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "leave_count": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "on_duty_count": {
                    "$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}
                },
            }
        },
    ]

    log_results = {
        row["_id"]: row for row in db.attendance_logs.aggregate(log_pipeline)
    }

    items = []
    for dept, hc_info in sorted(dept_headcounts.items()):
        hc = hc_info["headcount"]
        if hc == 0:
            continue  # omit departments with headcount 0 (shouldn't happen by filter)
        log = log_results.get(dept, {})
        present_days = round_half_up(log.get("present_days", 0), 2)
        awh_sum = log.get("avg_work_hours_sum", 0)
        awh_count = log.get("avg_work_hours_count", 0)
        avg_work_hours = round_half_up(awh_sum / awh_count, 2) if awh_count > 0 else None
        items.append({
            "department": dept,
            "headcount": hc,
            "present_days": present_days,
            "avg_work_hours": avg_work_hours,
            "late_count": int(log.get("late_count", 0)),
            "total_late_minutes": int(log.get("total_late_minutes", 0)),
            "leave_count": int(log.get("leave_count", 0)),
            "on_duty_count": int(log.get("on_duty_count", 0)),
        })

    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(default=10, ge=1, le=50),
    department: Optional[str] = None,
):
    """
    Late-comers leaderboard for a month.
    Standard competition ranking (1, 2, 2, 4).
    Returns all employees whose rank <= limit (ties at the cutoff are all included).
    """
    first_day, last_day = month_date_range(month)

    match_stage: dict[str, Any] = {
        "date": {"$gte": first_day, "$lte": last_day},
        "late_minutes": {"$gt": 0},
    }

    pipeline = [
        {"$match": match_stage},
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        # Join employees to get name and department
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        # Ignore logs whose employee no longer exists
        {"$match": {"emp": {"$ne": []}}},
        {"$unwind": "$emp"},
    ]

    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline += [
        {
            "$sort": {"total_late_minutes": DESCENDING, "_id": ASCENDING}
        },
        {
            "$project": {
                "_id": 0,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ]

    rows = list(db.attendance_logs.aggregate(pipeline))

    # Apply standard competition ranking in Python (MongoDB $rank doesn't exist in 4.x)
    ranked = []
    rank = 1
    for i, row in enumerate(rows):
        if i > 0 and row["total_late_minutes"] < rows[i - 1]["total_late_minutes"]:
            rank = i + 1
        row["rank"] = rank
        if rank <= limit:
            ranked.append(row)
        else:
            break

    return {"month": month, "items": ranked}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(alias="from"),
    to_date: str = Query(alias="to"),
):
    """
    Daily attendance trend for a department.
    Uses $densify to fill gaps (days with no records).
    """
    if to_date < from_date:
        raise HTTPException(
            status_code=422,
            detail=[{"msg": "to must not be before from"}],
        )
    # Range must not exceed 92 days
    from datetime import date as date_type
    f_date = date_type.fromisoformat(from_date)
    t_date = date_type.fromisoformat(to_date)
    if (t_date - f_date).days > 92:
        raise HTTPException(
            status_code=422,
            detail=[{"msg": "Date range must not exceed 92 days"}],
        )

    # Check department exists
    if not db.employees.find_one({"department": department}):
        raise HTTPException(status_code=404, detail="Department not found")

    # Aggregate daily stats from logs
    pipeline = [
        {
            "$match": {
                "date": {"$gte": from_date, "$lte": to_date},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
        {"$match": {"emp.department": department}},
        {
            "$group": {
                "_id": "$date",
                "present_count_raw": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", list(PRESENCE_STATUSES)]},
                            {
                                "$cond": [
                                    {"$eq": [{"$ifNull": ["$half_day", False]}, True]},
                                    0.5,
                                    1,
                                ]
                            },
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}
                },
            }
        },
        {"$sort": {"_id": ASCENDING}},
    ]

    daily_logs = {row["_id"]: row for row in db.attendance_logs.aggregate(pipeline)}

    # Build every day in the range in Python (simpler than $densify for this case)
    from datetime import date as date_type

    items = []
    current = f_date
    # Keep a rolling window of the last 7 rows' attendance_rate values (including None).
    # The moving average is the mean of the non-null values in that window.
    rate_window: list[Optional[float]] = []

    while current <= t_date:
        date_str = current.strftime("%Y-%m-%d")
        is_working_day = current.weekday() < 5  # Mon=0, Fri=4

        # Headcount: employees in dept joined on or before this day (R9)
        headcount = db.employees.count_documents({
            "department": department,
            "joined_on": {"$lte": date_str},
        })

        log = daily_logs.get(date_str, {})
        present_count = round_half_up(log.get("present_count_raw", 0), 2)
        late_count = int(log.get("late_count", 0))

        if is_working_day and headcount > 0:
            attendance_rate = round_half_up(present_count / headcount, 4)
        else:
            attendance_rate = None

        # Update the rolling 7-row window (current + up to 6 preceding rows)
        rate_window.append(attendance_rate)
        if len(rate_window) > 7:
            rate_window.pop(0)

        # moving_avg_7d = mean of non-null rates in the window; null if none are non-null
        non_null_in_window = [r for r in rate_window if r is not None]
        if non_null_in_window:
            moving_avg_7d = round_half_up(sum(non_null_in_window) / len(non_null_in_window), 4)
        else:
            moving_avg_7d = None

        items.append({
            "date": date_str,
            "is_working_day": is_working_day,
            "headcount": headcount,
            "present_count": present_count,
            "late_count": late_count,
            "attendance_rate": attendance_rate,
            "moving_avg_7d": moving_avg_7d,
        })

        current += timedelta(days=1)

    return {"department": department, "items": items}


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------

@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(default=10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_date: Optional[str] = Query(default=None, alias="from"),
    to_date: Optional[str] = Query(default=None, alias="to"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """Run explain(executionStats) for the named endpoint's main query/aggregation."""
    valid_endpoints = {
        "attendance_list", "employee_monthly", "department_summary",
        "late_leaderboard", "department_trend",
    }
    if endpoint not in valid_endpoints:
        raise HTTPException(
            status_code=422,
            detail=[{"msg": f"endpoint must be one of {sorted(valid_endpoints)}"}],
        )

    if endpoint == "attendance_list":
        q: dict[str, Any] = {}
        if emp_code:
            q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        if status:
            q["status"] = status
        skip = (page - 1) * page_size
        explain_result = db.command(
            "explain",
            {
                "find": "attendance_logs",
                "filter": q,
                "sort": {"date": -1, "emp_code": 1},
                "skip": skip,
                "limit": page_size,
            },
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_result}

    elif endpoint == "employee_monthly":
        if not month:
            raise HTTPException(
                status_code=422, detail=[{"msg": "month is required for employee_monthly"}]
            )
        if not emp_code:
            raise HTTPException(
                status_code=422, detail=[{"msg": "emp_code is required for employee_monthly"}]
            )
        first_day, last_day = month_date_range(month)
        pipeline = [
            {"$match": {"emp_code": emp_code, "date": {"$gte": first_day, "$lte": last_day}}},
        ]
        explain_result = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_result}

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(
                status_code=422, detail=[{"msg": "month is required for department_summary"}]
            )
        first_day, last_day = month_date_range(month)
        pipeline = [
            {"$match": {"date": {"$gte": first_day, "$lte": last_day}}},
        ]
        explain_result = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_result}

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(
                status_code=422, detail=[{"msg": "month is required for late_leaderboard"}]
            )
        first_day, last_day = month_date_range(month)
        match: dict[str, Any] = {
            "date": {"$gte": first_day, "$lte": last_day},
            "late_minutes": {"$gt": 0},
        }
        pipeline = [{"$match": match}]
        explain_result = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_result}

    elif endpoint == "department_trend":
        if not from_date or not to_date:
            raise HTTPException(
                status_code=422, detail=[{"msg": "from and to are required for department_trend"}]
            )
        pipeline = [
            {"$match": {"date": {"$gte": from_date, "$lte": to_date}}},
        ]
        explain_result = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_result}
