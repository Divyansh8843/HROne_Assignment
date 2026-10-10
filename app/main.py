"""
Employee Attendance & Analytics API  — full implementation
Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB  (a local .env is loaded for convenience)
"""
import os
import math
import calendar
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone, date as date_type
from typing import Optional, Any

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, field_validator, model_validator
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError

load_dotenv()

# ---------------------------------------------------------------------------
# Database connection & constants
# ---------------------------------------------------------------------------
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB  = os.getenv("MONGO_DB",  "attendance_db")

client = MongoClient(MONGO_URI, tz_aware=True)
db     = client[MONGO_DB]

IST    = timezone(timedelta(hours=5, minutes=30))
UTC    = timezone.utc

# Epoch-milliseconds bounds (from openapi.yaml EpochMillis schema)
EPOCH_MS_MIN = 100_000_000_000   # smallest valid epoch-ms (prevents accidental seconds)
EPOCH_MS_MAX = 4_102_444_800_000

# ---------------------------------------------------------------------------
# Index creation (idempotent, called at startup)
# ---------------------------------------------------------------------------
def create_indexes() -> None:
    # employees: unique emp_code
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="idx_employees_emp_code")

    # attendance_logs: unique (emp_code, date) — the natural key
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
        name="idx_attendance_emp_date",
    )
    # date descending + emp_code ascending — primary list sort
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)],
        name="idx_attendance_date_emp",
    )
    # status filter support
    db.attendance_logs.create_index([("status", ASCENDING)], name="idx_attendance_status")
    # per-employee date range queries (analytics)
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        name="idx_attendance_emp_date_range",
    )
    # department lookup on employees for analytics cross-join
    db.employees.create_index([("department", ASCENDING)], name="idx_employees_department")
    db.employees.create_index([("joined_on", ASCENDING)], name="idx_employees_joined_on")


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app_: FastAPI):
    create_indexes()
    yield

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Helper utilities
# ---------------------------------------------------------------------------

def ms_to_utc(ms: int) -> datetime:
    """Convert epoch-milliseconds → UTC datetime (truncated to whole seconds)."""
    return datetime.fromtimestamp(ms // 1000, tz=UTC)


def utc_to_ms(dt: datetime) -> int:
    """Convert a datetime → epoch-milliseconds (whole-second precision)."""
    if dt is None:
        return None
    # Ensure UTC
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    else:
        dt = dt.astimezone(UTC)
    return int(dt.timestamp()) * 1000


def dt_to_ist(dt: datetime) -> datetime:
    """Convert a UTC-aware datetime to IST."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(IST)


def attendance_date_for_punch(punch_utc: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1: The attendance date is the IST calendar date of the punch-in.
    Exception for overnight shifts (shift_end <= shift_start):
        if the IST time of punch-in is earlier than shift_end, it belongs to
        the previous calendar day (the shift that started that day).
    """
    punch_ist = dt_to_ist(punch_utc)
    date_ist  = punch_ist.date()

    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))

    # Overnight shift
    if (eh * 60 + em) <= (sh * 60 + sm):
        punch_time_min = punch_ist.hour * 60 + punch_ist.minute
        end_min        = eh * 60 + em
        if punch_time_min < end_min:
            # belongs to the previous day's shift
            date_ist = (punch_ist - timedelta(days=1)).date()

    return date_ist.isoformat()


def half_up_round(value: float, decimals: int) -> float:
    """Half-up rounding (not Python's default banker's rounding)."""
    factor = 10 ** decimals
    return math.floor(value * factor + 0.5) / factor


# ---------------------------------------------------------------------------
# Business-rule helpers
# ---------------------------------------------------------------------------

def compute_late_minutes(punch_in_utc: datetime, shift_start: str, attendance_date: str) -> int:
    """
    R2: Grace period is 10 minutes (strict: > 10:00 after shift_start).
    late_minutes = floor((punch_in - shift_start) / 60) if > 10 min late, else 0.
    """
    sh, sm = map(int, shift_start.split(":"))
    # Build the shift-start instant in IST on the attendance date
    att_date    = date_type.fromisoformat(attendance_date)
    shift_start_ist = datetime(att_date.year, att_date.month, att_date.day, sh, sm, 0, tzinfo=IST)
    shift_start_utc = shift_start_ist.astimezone(UTC)

    diff_sec = (punch_in_utc - shift_start_utc).total_seconds()
    if diff_sec <= 600:           # <= 10 minutes → on time (includes exact 10:00)
        return 0
    return int(diff_sec // 60)   # floor whole minutes since shift_start (R2)


def compute_overtime_minutes(punch_out_utc: datetime, shift_end: str, attendance_date: str) -> int:
    """
    R3: overtime_minutes = floor((punch_out - shift_end) / 60), only when >= 30 min; else 0.
    For overnight shifts the shift_end falls on attendance_date + 1.
    """
    sh, sm = map(int, shift_end.split(":"))
    att_date = date_type.fromisoformat(attendance_date)
    # Normal shift
    shift_end_ist = datetime(att_date.year, att_date.month, att_date.day, sh, sm, 0, tzinfo=IST)
    # If the shift_end time <= shift_start time it's overnight → shift_end is next day
    # We detect overnight by checking if punch_out is on the next day relative to attendance_date
    # More robustly: pass shift_start too
    shift_end_utc = shift_end_ist.astimezone(UTC)

    diff_sec = (punch_out_utc - shift_end_utc).total_seconds()
    if diff_sec < 1800:           # < 30 minutes
        return 0
    return int(diff_sec // 60)


def compute_overtime_minutes_with_start(
    punch_out_utc: datetime, shift_start: str, shift_end: str, attendance_date: str
) -> int:
    """Like compute_overtime_minutes but handles overnight shifts correctly."""
    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))
    att_date = date_type.fromisoformat(attendance_date)

    shift_end_ist = datetime(att_date.year, att_date.month, att_date.day, eh, em, 0, tzinfo=IST)
    # Overnight: end is on the next day
    if (eh * 60 + em) <= (sh * 60 + sm):
        shift_end_ist += timedelta(days=1)
    shift_end_utc = shift_end_ist.astimezone(UTC)

    diff_sec = (punch_out_utc - shift_end_utc).total_seconds()
    if diff_sec < 1800:
        return 0
    return int(diff_sec // 60)


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    """R4: elapsed seconds / 3600, half-up to 2 decimals."""
    elapsed = (punch_out_utc - punch_in_utc).total_seconds()
    return half_up_round(elapsed / 3600, 2)


def compute_half_day(work_hours: float) -> bool:
    """R5: half_day is True when rounded work_hours < 4.50."""
    return work_hours < 4.50


def recompute_derived(doc: dict, emp: dict) -> dict:
    """
    After a regularization that changes punch_in / punch_out / status,
    recompute late_minutes, work_hours, overtime_minutes, half_day.
    Returns a dict of only the derived fields.
    """
    status    = doc["status"]
    punch_in  = doc.get("punch_in")
    punch_out = doc.get("punch_out")
    att_date  = doc["date"]
    shift_start = emp["shift_start"]
    shift_end   = emp["shift_end"]

    if status in ("ABSENT", "LEAVE") or punch_in is None:
        return {
            "late_minutes":     0,
            "work_hours":       None,
            "overtime_minutes": 0,
            "half_day":         False,
        }

    late = compute_late_minutes(punch_in, shift_start, att_date)
    if punch_out is None:
        return {
            "late_minutes":     late,
            "work_hours":       None,
            "overtime_minutes": 0,
            "half_day":         False,
        }

    wh  = compute_work_hours(punch_in, punch_out)
    ot  = compute_overtime_minutes_with_start(punch_out, shift_start, shift_end, att_date)
    hd  = compute_half_day(wh)
    return {
        "late_minutes":     late,
        "work_hours":       wh,
        "overtime_minutes": ot,
        "half_day":         hd,
    }


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------

def _ser_datetime(dt) -> Optional[int]:
    """BSON datetime → epoch-ms (or None)."""
    if dt is None:
        return None
    return utc_to_ms(dt)


def _ser_history(history: list) -> list:
    """Convert history array: BSON dates in `at` and `changes.*` → epoch-ms."""
    out = []
    for entry in history:
        ch = {}
        for field, fv in entry.get("changes", {}).items():
            ch[field] = {
                "from": _ser_datetime(fv["from"]) if isinstance(fv.get("from"), datetime) else fv.get("from"),
                "to":   _ser_datetime(fv["to"])   if isinstance(fv.get("to"),   datetime) else fv.get("to"),
            }
        out.append({
            "at":      utc_to_ms(entry["at"]) if isinstance(entry.get("at"), datetime) else entry.get("at"),
            "by":      entry.get("by"),
            "reason":  entry.get("reason"),
            "changes": ch,
        })
    return out


def format_attendance_doc(doc: dict) -> dict:
    """Convert a MongoDB attendance document to the API response shape."""
    doc.pop("_id", None)
    return {
        "emp_code":          doc["emp_code"],
        "date":              doc["date"],
        "status":            doc["status"],
        "punch_in":          _ser_datetime(doc.get("punch_in")),
        "punch_out":         _ser_datetime(doc.get("punch_out")),
        "work_hours":        doc.get("work_hours"),
        "late_minutes":      doc.get("late_minutes", 0),
        "overtime_minutes":  doc.get("overtime_minutes", 0),
        "half_day":          doc.get("half_day", False),
        "history":           _ser_history(doc.get("history") or []),
    }


def format_employee_doc(doc: dict) -> dict:
    """Convert a MongoDB employee document to the API response shape."""
    doc.pop("_id", None)
    return {
        "emp_code":    doc["emp_code"],
        "name":        doc["name"],
        "email":       doc["email"],
        "department":  doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end":   doc["shift_end"],
        "joined_on":   doc["joined_on"],
        "created_at":  utc_to_ms(doc["created_at"]) if isinstance(doc.get("created_at"), datetime) else doc.get("created_at"),
    }


# ---------------------------------------------------------------------------
# Pydantic Models
# ---------------------------------------------------------------------------

class EmployeeIn(BaseModel):
    emp_code:    str
    name:        str
    email:       str
    department:  str
    shift_start: str = "09:30"
    shift_end:   str = "18:30"
    joined_on:   str

    @model_validator(mode="after")
    def shift_start_ne_end(self) -> "EmployeeIn":
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code:   str
    punched_at: Optional[int] = None
    status:     str = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def validate_epoch_ms(cls, v):
        if v is not None:
            if not isinstance(v, int) or isinstance(v, bool):
                raise ValueError("punched_at must be an integer epoch-milliseconds value")
            if v < EPOCH_MS_MIN or v > EPOCH_MS_MAX:
                raise ValueError(
                    f"punched_at must be epoch milliseconds in [{EPOCH_MS_MIN}, {EPOCH_MS_MAX}]"
                )
        return v

    @field_validator("status")
    @classmethod
    def status_presence_only(cls, v):
        if v not in ("PRESENT", "WFH", "ON_DUTY"):
            raise ValueError("status for punch-in must be PRESENT, WFH, or ON_DUTY")
        return v


class PunchOutIn(BaseModel):
    emp_code:   str
    punched_at: Optional[int] = None

    @field_validator("punched_at")
    @classmethod
    def validate_epoch_ms(cls, v):
        if v is not None:
            if not isinstance(v, int) or isinstance(v, bool):
                raise ValueError("punched_at must be an integer epoch-milliseconds value")
            if v < EPOCH_MS_MIN or v > EPOCH_MS_MAX:
                raise ValueError(
                    f"punched_at must be epoch milliseconds in [{EPOCH_MS_MIN}, {EPOCH_MS_MAX}]"
                )
        return v


class RegularizeIn(BaseModel):
    reason:          str
    regularized_by:  str
    status:          Optional[str] = None
    punch_in:        Optional[int] = None
    punch_out:       Optional[int] = None

    @field_validator("punch_in", "punch_out")
    @classmethod
    def validate_epoch_ms(cls, v):
        if v is not None:
            if not isinstance(v, int) or isinstance(v, bool):
                raise ValueError("punch times must be integer epoch-milliseconds")
            if v < EPOCH_MS_MIN or v > EPOCH_MS_MAX:
                raise ValueError(
                    f"punch time must be epoch milliseconds in [{EPOCH_MS_MIN}, {EPOCH_MS_MAX}]"
                )
        return v

    @field_validator("status")
    @classmethod
    def validate_status(cls, v):
        if v is not None and v not in ("PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"):
            raise ValueError("invalid status")
        return v

    @field_validator("reason")
    @classmethod
    def reason_length(cls, v):
        if len(v) < 2:
            raise ValueError("reason must be at least 2 characters")
        return v

    @field_validator("regularized_by")
    @classmethod
    def by_length(cls, v):
        if len(v) < 1:
            raise ValueError("regularized_by must not be empty")
        return v


# ---------------------------------------------------------------------------
# Endpoints — System
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    """R: 200 if MongoDB is reachable, 503 otherwise."""
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(503, "MongoDB unavailable")


# ---------------------------------------------------------------------------
# Endpoints — Employees
# ---------------------------------------------------------------------------

@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    """Create an employee; enforced by unique index → 409 on duplicate."""
    doc = body.model_dump()
    doc["created_at"] = datetime.now(UTC)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    doc.pop("_id", None)
    return format_employee_doc(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page:       int = Query(default=1,  ge=1),
    page_size:  int = Query(default=20, ge=1, le=100),
):
    """List employees sorted by emp_code asc. total reflects the filtered count."""
    q: dict = {}
    if department:
        q["department"] = department
    total = db.employees.count_documents(q)
    skip  = (page - 1) * page_size
    items = list(
        db.employees.find(q, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip(skip)
        .limit(page_size)
    )
    return {
        "items":     [format_employee_doc(d) for d in items],
        "total":     total,
        "page":      page,
        "page_size": page_size,
    }


# ---------------------------------------------------------------------------
# Endpoints — Attendance
# ---------------------------------------------------------------------------

@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    """
    Record a punch-in. Uses atomic insert to handle concurrent duplicate
    punch-ins (unique index on emp_code+date → 409 for the loser).
    """
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, f"Employee '{body.emp_code}' not found")

    now_utc = datetime.now(UTC)
    if body.punched_at is not None:
        punch_utc = ms_to_utc(body.punched_at)
    else:
        # Truncate to whole seconds
        punch_utc = now_utc.replace(microsecond=0)

    att_date   = attendance_date_for_punch(punch_utc, emp["shift_start"], emp["shift_end"])
    late_min   = compute_late_minutes(punch_utc, emp["shift_start"], att_date)

    doc = {
        "emp_code":         body.emp_code,
        "date":             att_date,
        "status":           body.status,
        "punch_in":         punch_utc,
        "punch_out":        None,
        "work_hours":       None,
        "late_minutes":     late_min,
        "overtime_minutes": 0,
        "half_day":         False,
        "history":          [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, f"Employee '{body.emp_code}' is already punched in for {att_date}")

    return format_attendance_doc(doc)


@app.post("/attendance/punch-out", status_code=200)
def punch_out(body: PunchOutIn):
    """
    Close the employee's most recent open punch-in record.
    Atomic conditional update prevents double punch-out.
    """
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, f"Employee '{body.emp_code}' not found")

    now_utc = datetime.now(UTC)
    if body.punched_at is not None:
        punch_out_utc = ms_to_utc(body.punched_at)
    else:
        punch_out_utc = now_utc.replace(microsecond=0)

    # Find the most-recent open record
    record = db.attendance_logs.find_one(
        {
            "emp_code":  body.emp_code,
            "punch_in":  {"$ne": None},
            "punch_out": None,
        },
        sort=[("date", DESCENDING)],
    )
    if not record:
        # Check if there's a record that's already punched out
        any_record = db.attendance_logs.find_one(
            {"emp_code": body.emp_code, "punch_in": {"$ne": None}},
            sort=[("date", DESCENDING)],
        )
        if any_record and any_record.get("punch_out") is not None:
            raise HTTPException(409, "Record is already punched out")
        raise HTTPException(404, f"No open punch-in found for '{body.emp_code}'")

    punch_in_utc = record["punch_in"]

    # R4 validation: punch_out must be after punch_in and within 24 hours
    diff_sec = (punch_out_utc - punch_in_utc).total_seconds()
    if diff_sec <= 0:
        raise HTTPException(422, "punched_at must be after punch_in")
    if diff_sec > 86400:
        raise HTTPException(422, "punched_at must be within 24 hours of punch_in")

    wh = compute_work_hours(punch_in_utc, punch_out_utc)
    ot = compute_overtime_minutes_with_start(
        punch_out_utc, emp["shift_start"], emp["shift_end"], record["date"]
    )
    hd = compute_half_day(wh)

    # Atomic update: only update if punch_out is still None (prevents double punch-out race)
    result = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out":        punch_out_utc,
                "work_hours":       wh,
                "overtime_minutes": ot,
                "half_day":         hd,
            }
        },
        return_document=True,
    )
    if result is None:
        raise HTTPException(409, "Record is already punched out")

    return format_attendance_doc(result)


@app.get("/attendance")
def list_attendance(
    emp_code:  Optional[str] = None,
    date_from: Optional[str] = None,
    date_to:   Optional[str] = None,
    status:    Optional[str] = None,
    page:      int = Query(default=1,  ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """
    List attendance records, sorted by date desc then emp_code asc.
    Uses indexes; does not load entire collection into memory.
    """
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must not be after date_to")

    q: dict = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        valid_statuses = {"PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"}
        if status not in valid_statuses:
            raise HTTPException(422, f"status must be one of {sorted(valid_statuses)}")
        q["status"] = status

    total = db.attendance_logs.count_documents(q)
    skip  = (page - 1) * page_size
    docs  = list(
        db.attendance_logs.find(q)
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip(skip)
        .limit(page_size)
    )
    return {
        "items":     [format_attendance_doc(d) for d in docs],
        "total":     total,
        "page":      page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    """
    Manually correct a record. Appends one history entry atomically.
    Recomputes derived fields after applying changes.
    """
    # Validate date format
    try:
        date_type.fromisoformat(date)
    except ValueError:
        raise HTTPException(422, "date must be YYYY-MM-DD")

    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, f"Employee '{emp_code}' not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(404, f"No attendance record for '{emp_code}' on {date}")

    # Business rule: ABSENT/LEAVE cannot have punch times
    new_status = body.status if body.status is not None else record["status"]
    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(
                422, "Cannot set punch times for ABSENT or LEAVE status"
            )

    # Presence status requires punch_in
    punch_in_ms = body.punch_in
    punch_out_ms = body.punch_out

    # Convert epoch-ms to UTC datetimes (whole-second truncated)
    new_punch_in  = ms_to_utc(punch_in_ms)  if punch_in_ms  is not None else record.get("punch_in")
    new_punch_out = ms_to_utc(punch_out_ms) if punch_out_ms is not None else record.get("punch_out")

    # If status becomes ABSENT/LEAVE, clear punch times
    if new_status in ("ABSENT", "LEAVE"):
        new_punch_in  = None
        new_punch_out = None

    # Presence status requires a punch_in
    if new_status in ("PRESENT", "WFH", "ON_DUTY") and new_punch_in is None:
        raise HTTPException(422, "Presence status requires punch_in")

    # punch_in must remain on the record's attendance date (R1)
    if new_punch_in is not None:
        # Validate that the IST date of the new punch_in matches the record date
        new_att_date = attendance_date_for_punch(new_punch_in, emp["shift_start"], emp["shift_end"])
        if new_att_date != date:
            raise HTTPException(
                422, f"punch_in must remain on attendance date {date} (resolved to {new_att_date})"
            )

    # punch_out must be after punch_in and within 24h
    if new_punch_out is not None and new_punch_in is not None:
        diff = (new_punch_out - new_punch_in).total_seconds()
        if diff <= 0:
            raise HTTPException(422, "punch_out must be after punch_in")
        if diff > 86400:
            raise HTTPException(422, "punch_out must be within 24 hours of punch_in")

    # Build the projected new state for change detection
    new_doc = dict(record)
    new_doc["status"]    = new_status
    new_doc["punch_in"]  = new_punch_in
    new_doc["punch_out"] = new_punch_out

    # Recompute derived fields
    derived = recompute_derived(new_doc, emp)
    new_doc.update(derived)

    # Detect changes (only fields that actually changed)
    TRACKABLE = ["status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day"]
    changes: dict = {}
    for field in TRACKABLE:
        old_val = record.get(field)
        new_val = new_doc.get(field)
        # Normalise: None == None is fine; datetime comparison is direct
        if old_val != new_val:
            if field in ("punch_in", "punch_out"):
                changes[field] = {
                    "from": _ser_datetime(old_val) if isinstance(old_val, datetime) else old_val,
                    "to":   _ser_datetime(new_val) if isinstance(new_val, datetime) else new_val,
                }
            else:
                changes[field] = {"from": old_val, "to": new_val}

    if not changes:
        raise HTTPException(422, "No fields actually changed")

    # Build history entry
    history_entry = {
        "at":     datetime.now(UTC),
        "by":     body.regularized_by,
        "reason": body.reason,
        "changes": {
            k: {
                "from": ms_to_utc(v["from"]) if k in ("punch_in", "punch_out") and isinstance(v["from"], int) else v["from"],
                "to":   ms_to_utc(v["to"])   if k in ("punch_in", "punch_out") and isinstance(v["to"],   int) else v["to"],
            }
            for k, v in changes.items()
        },
    }

    # Atomic update: set all changed fields AND push history entry together.
    # Use optimistic concurrency via version check or simply $push + $set atomically.
    set_payload: dict = {
        "status":           new_status,
        "punch_in":         new_punch_in,
        "punch_out":        new_punch_out,
        "work_hours":       derived["work_hours"],
        "late_minutes":     derived["late_minutes"],
        "overtime_minutes": derived["overtime_minutes"],
        "half_day":         derived["half_day"],
    }

    result = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"]},
        {
            "$set":  set_payload,
            "$push": {"history": history_entry},
        },
        return_document=True,
    )
    if result is None:
        raise HTTPException(409, "Record was modified concurrently; please retry")

    return format_attendance_doc(result)


# ---------------------------------------------------------------------------
# Analytics helpers
# ---------------------------------------------------------------------------

def _month_range(month: str):
    """Return (first_day_str, last_day_str) for a YYYY-MM string."""
    year, mon = int(month[:4]), int(month[5:])
    first = date_type(year, mon, 1)
    last  = date_type(year, mon, calendar.monthrange(year, mon)[1])
    return first.isoformat(), last.isoformat()


def _working_days_in_month(month: str, joined_on: str) -> int:
    """
    R7: Count Mon-Fri days in the month, starting from max(1st, joined_on).
    """
    year, mon = int(month[:4]), int(month[5:])
    first = date_type(year, mon, 1)
    last  = date_type(year, mon, calendar.monthrange(year, mon)[1])

    join  = date_type.fromisoformat(joined_on)
    start = max(first, join)
    if start > last:
        return 0

    count = 0
    d = start
    while d <= last:
        if d.weekday() < 5:   # 0=Mon … 4=Fri
            count += 1
        d += timedelta(days=1)
    return count


# ---------------------------------------------------------------------------
# Analytics Endpoints
# ---------------------------------------------------------------------------

@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    """Monthly attendance summary for a single employee via aggregation."""
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, f"Employee '{emp_code}' not found")

    first_day, last_day = _month_range(month)
    working_days = _working_days_in_month(month, emp["joined_on"])

    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first_day, "$lte": last_day}}},
        {
            "$group": {
                "_id": None,
                # present_days: R5+R6+R7
                # Only weekday records with presence status count
                # half_day = 0.5, full day = 1.0
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    # weekday: dayOfWeek 2=Mon … 6=Fri (1=Sun, 7=Sat in MongoDB)
                                    {
                                        "$in": [
                                            {"$dayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}}},
                                            [2, 3, 4, 5, 6],
                                        ]
                                    },
                                ]
                            },
                            {
                                "$cond": [
                                    {"$eq": [{"$ifNull": ["$half_day", False]}, True]},
                                    0.5,
                                    1.0,
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

    results = list(db.attendance_logs.aggregate(pipeline))
    if results:
        r = results[0]
    else:
        r = {
            "present_days": 0,
            "leave_days": 0,
            "late_count": 0,
            "total_late_minutes": 0,
            "total_overtime_minutes": 0,
        }

    present_days = half_up_round(r["present_days"], 2)
    att_pct = (
        half_up_round(present_days / working_days * 100, 2)
        if working_days > 0
        else None
    )

    return {
        "emp_code":               emp_code,
        "month":                  month,
        "working_days":           working_days,
        "present_days":           present_days,
        "leave_days":             int(r["leave_days"]),
        "late_count":             int(r["late_count"]),
        "total_late_minutes":     int(r["total_late_minutes"]),
        "total_overtime_minutes": int(r["total_overtime_minutes"]),
        "attendance_pct":         att_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month:      str           = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    """
    Department summary via cross-collection aggregation.
    Employees with zero logs are included (R9).
    Averages are over individual records, not per-employee averages.
    """
    first_day, last_day = _month_range(month)

    emp_match: dict = {"joined_on": {"$lte": last_day}}
    if department:
        emp_match["department"] = department

    pipeline = [
        # Start from employees, filter by joined_on and optional department
        {"$match": emp_match},
        # Left join attendance_logs for this month only
        {
            "$lookup": {
                "from": "attendance_logs",
                "let":  {"ec": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq":  ["$emp_code", "$$ec"]},
                                    {"$gte": ["$date", first_day]},
                                    {"$lte": ["$date", last_day]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        # Unwind logs (preserveNullAndEmptyArrays so employees with no logs remain)
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        # Group by department
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$addToSet": "$emp_code"},
                # present_days: weekday presence records; half_day = 0.5
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {
                                        "$in": [
                                            {
                                                "$dayOfWeek": {
                                                    "$dateFromString": {
                                                        "dateString": "$logs.date",
                                                        "format": "%Y-%m-%d",
                                                    }
                                                }
                                            },
                                            [2, 3, 4, 5, 6],
                                        ]
                                    },
                                ]
                            },
                            {
                                "$cond": [
                                    {"$eq": [{"$ifNull": ["$logs.half_day", False]}, True]},
                                    0.5,
                                    1.0,
                                ]
                            },
                            0,
                        ]
                    }
                },
                # avg_work_hours: average over presence records with a work_hours value
                "work_hours_sum":   {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$logs.work_hours", None]},
                                ]
                            },
                            "$logs.work_hours",
                            0,
                        ]
                    }
                },
                "work_hours_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$logs.work_hours", None]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$logs.late_minutes", 0]}
                },
                "leave_count": {
                    "$sum": {
                        "$cond": [
                            {"$and": [{"$ne": ["$logs", None]}, {"$eq": ["$logs.status", "LEAVE"]}]},
                            1,
                            0,
                        ]
                    }
                },
                "on_duty_count": {
                    "$sum": {
                        "$cond": [
                            {"$and": [{"$ne": ["$logs", None]}, {"$eq": ["$logs.status", "ON_DUTY"]}]},
                            1,
                            0,
                        ]
                    }
                },
            }
        },
        {"$sort": {"_id": 1}},
    ]

    items = []

    for r in db.employees.aggregate(pipeline):
        hc = len(r["headcount"])
        if hc == 0:
            continue
        avg_wh = (
            half_up_round(r["work_hours_sum"] / r["work_hours_count"], 2)
            if r["work_hours_count"] > 0
            else None
        )
        items.append({
            "department":        r["_id"],
            "headcount":         hc,
            "present_days":      half_up_round(r["present_days"], 2),
            "avg_work_hours":    avg_wh,
            "late_count":        int(r["late_count"]),
            "total_late_minutes": int(r["total_late_minutes"]),
            "leave_count":       int(r["leave_count"]),
            "on_duty_count":     int(r["on_duty_count"]),
        })

    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month:      str           = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit:      int           = Query(default=10, ge=1, le=50),
    department: Optional[str] = None,
):
    """
    Late-comers leaderboard using $setWindowFields for standard competition ranking.
    Ties share a rank; next rank has a gap (1, 2, 2, 4).
    Limit applied after ranking (may return >limit rows on tie at boundary).
    """
    first_day, last_day = _month_range(month)

    emp_match: dict = {}
    if department:
        emp_match["department"] = department

    pipeline = [
        # Start from employees for name/department lookup
        {"$match": emp_match} if emp_match else {"$match": {}},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let":  {"ec": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq":  ["$emp_code", "$$ec"]},
                                    {"$gte": ["$date", first_day]},
                                    {"$lte": ["$date", last_day]},
                                    {"$gt":  [{"$ifNull": ["$late_minutes", 0]}, 0]},
                                ]
                            }
                        }
                    }
                ],
                "as": "late_logs",
            }
        },
        # Keep only employees with late records
        {"$match": {"late_logs.0": {"$exists": True}}},
        # Unwind to aggregate
        {"$unwind": "$late_logs"},
        {
            "$group": {
                "_id":               "$emp_code",
                "name":              {"$first": "$name"},
                "department":        {"$first": "$department"},
                "total_late_minutes": {"$sum": "$late_logs.late_minutes"},
                "late_count":        {"$sum": 1},
            }
        },
        {
            "$sort": {"total_late_minutes": -1, "_id": 1}
        },
        # Standard competition ranking
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {
                        "$rank": {}
                    }
                }
            }
        },
        # Apply limit: keep rows where rank <= limit
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {
            "$project": {
                "_id":               0,
                "rank":              "$rank",
                "emp_code":          "$_id",
                "name":              1,
                "department":        1,
                "total_late_minutes": 1,
                "late_count":        1,
            }
        },
    ]

    items = list(db.employees.aggregate(pipeline))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date:  str = Query(..., alias="from"),
    to_date:    str = Query(..., alias="to"),
):
    """
    Daily attendance trend for a department.
    Uses $densify for gap-filling and $setWindowFields for 7-day moving average.
    Returns exactly one row per calendar day in [from, to].
    """
    # Validate dates
    try:
        fd = date_type.fromisoformat(from_date)
        td = date_type.fromisoformat(to_date)
    except ValueError:
        raise HTTPException(422, "from and to must be YYYY-MM-DD dates")

    if td < fd:
        raise HTTPException(422, "to must not be before from")

    delta_days = (td - fd).days + 1
    if delta_days > 92:
        raise HTTPException(422, "Date range must not exceed 92 days")

    # Validate department exists
    dept_exists = db.employees.find_one({"department": department})
    if not dept_exists:
        raise HTTPException(404, f"Department '{department}' not found")

    # For headcount per day: employees where joined_on <= that day
    # We'll calculate this via MongoDB; for $densify we need a date field.
    # Strategy:
    # 1. Aggregate attendance_logs for this department + date range
    # 2. $densify to fill gaps
    # 3. $lookup employee headcount per day
    # 4. $setWindowFields for 7-day moving avg

    pipeline = [
        # Step 1: aggregate logs for this dept in date range
        {
            "$match": {
                "date": {"$gte": from_date, "$lte": to_date},
            }
        },
        # Join to verify employee belongs to the department
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "emp_doc",
            }
        },
        {"$unwind": "$emp_doc"},
        {"$match": {"emp_doc.department": department}},
        # Compute is_working_day and presence contribution
        {
            "$addFields": {
                "date_obj": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}},
                "dow": {
                    "$dayOfWeek": {
                        "$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}
                    }
                },
                "is_present": {
                    "$cond": [{"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]}, 1, 0]
                },
                "present_contribution": {
                    "$cond": [
                        {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                        {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1.0]},
                        0,
                    ]
                },
                "is_late": {
                    "$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]
                },
            }
        },
        # Group by date
        {
            "$group": {
                "_id":           "$date",
                "date_obj":      {"$first": "$date_obj"},
                "present_count": {"$sum": "$present_contribution"},
                "late_count":    {"$sum": "$is_late"},
            }
        },
        # Convert date string to Date for $densify
        {
            "$addFields": {
                "date_dt": "$date_obj"
            }
        },
        # Densify to fill missing dates
        {
            "$densify": {
                "field": "date_dt",
                "range": {
                    "step":     1,
                    "unit":     "day",
                    "bounds":   [
                        datetime(fd.year, fd.month, fd.day, tzinfo=UTC),
                        datetime(td.year, td.month, td.day, tzinfo=UTC) + timedelta(days=1),
                    ],
                }
            }
        },
        # Restore date string from date_dt for densified rows
        {
            "$addFields": {
                "_id":           {"$dateToString": {"format": "%Y-%m-%d", "date": "$date_dt"}},
                "present_count": {"$ifNull": ["$present_count", 0]},
                "late_count":    {"$ifNull": ["$late_count", 0]},
            }
        },
        # is_working_day: Mon-Fri (dayOfWeek: 1=Sun, 2=Mon … 7=Sat)
        {
            "$addFields": {
                "dow_val": {"$dayOfWeek": "$date_dt"},
                "is_working_day": {
                    "$in": [{"$dayOfWeek": "$date_dt"}, [2, 3, 4, 5, 6]]
                },
            }
        },
        # Sort by date for window function
        {"$sort": {"_id": 1}},
    ]

    # We'll compute headcount and attendance_rate in Python after the pipeline
    # because $densify doesn't support correlated sub-queries for headcount.
    raw_rows = list(db.attendance_logs.aggregate(pipeline))

    # Build lookup: date_str → headcount (employees joined_on <= date)
    # Fetch all dept employees with their joined_on
    dept_employees = list(db.employees.find({"department": department}, {"emp_code": 1, "joined_on": 1, "_id": 0}))

    result_rows = []
    for row in raw_rows:
        date_str      = row["_id"]
        present_count = row.get("present_count", 0)
        late_count    = row.get("late_count", 0)
        is_working_day = row.get("is_working_day", False)

        # Headcount: employees joined on or before this day
        headcount = sum(1 for e in dept_employees if e["joined_on"] <= date_str)

        # attendance_rate: only on working days with headcount > 0
        if is_working_day and headcount > 0:
            att_rate = half_up_round(present_count / headcount, 4)
        else:
            att_rate = None

        result_rows.append({
            "date":            date_str,
            "is_working_day":  is_working_day,
            "headcount":       headcount,
            "present_count":   present_count,
            "late_count":      int(late_count),
            "attendance_rate": att_rate,
            "moving_avg_7d":   None,  # computed below
        })

    # Compute 7-day moving average in pure Python
    # The window is the current row + up to 6 preceding rows within the requested range.
    # Only non-null attendance_rate values participate.
    for i, row in enumerate(result_rows):
        window_start = max(0, i - 6)
        window_vals  = [
            result_rows[j]["attendance_rate"]
            for j in range(window_start, i + 1)
            if result_rows[j]["attendance_rate"] is not None
        ]
        if window_vals:
            row["moving_avg_7d"] = half_up_round(sum(window_vals) / len(window_vals), 4)
        else:
            row["moving_avg_7d"] = None

    return {"department": department, "items": result_rows}


# ---------------------------------------------------------------------------
# Admin — Explain
# ---------------------------------------------------------------------------

VALID_ENDPOINTS = {"attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"}


def _bson_to_json(obj: Any) -> Any:
    """Recursively convert BSON-unfriendly types for JSON serialisation."""
    import json
    return json.loads(json_util.dumps(obj))


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint:   str,
    emp_code:   Optional[str] = None,
    month:      Optional[str] = Query(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit:      int           = Query(default=10, ge=1, le=50),
    date_from:  Optional[str] = None,
    date_to:    Optional[str] = None,
    status:     Optional[str] = None,
    from_date:  Optional[str] = Query(default=None, alias="from"),
    to_date:    Optional[str] = Query(default=None, alias="to"),
    page:       int           = Query(default=1,  ge=1),
    page_size:  int           = Query(default=20, ge=1, le=100),
):
    if endpoint not in VALID_ENDPOINTS:
        raise HTTPException(422, f"endpoint must be one of: {sorted(VALID_ENDPOINTS)}")

    explain_result = None
    collection_name = None

    if endpoint == "attendance_list":
        q: dict = {}
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
        cursor = db.attendance_logs.find(q).sort(
            [("date", DESCENDING), ("emp_code", ASCENDING)]
        ).skip(skip).limit(page_size)
        explain_result = cursor.explain()
        collection_name = "attendance_logs"

    elif endpoint == "employee_monthly":
        if not emp_code:
            raise HTTPException(422, "emp_code is required for employee_monthly explain")
        if not month:
            raise HTTPException(422, "month is required for employee_monthly explain")
        first_day, last_day = _month_range(month)
        pipeline = [
            {"$match": {"emp_code": emp_code, "date": {"$gte": first_day, "$lte": last_day}}},
        ]
        explain_result = db.command(
            "aggregate", "attendance_logs",
            pipeline=pipeline,
            explain=True,
        )
        collection_name = "attendance_logs"

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month is required for department_summary explain")
        first_day, last_day = _month_range(month)
        emp_match: dict = {"joined_on": {"$lte": last_day}}
        if department:
            emp_match["department"] = department
        pipeline = [
            {"$match": emp_match},
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "let":  {"ec": "$emp_code"},
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {
                                    "$and": [
                                        {"$eq":  ["$emp_code", "$$ec"]},
                                        {"$gte": ["$date", first_day]},
                                        {"$lte": ["$date", last_day]},
                                    ]
                                }
                            }
                        }
                    ],
                    "as": "logs",
                }
            },
        ]
        explain_result = db.command(
            "aggregate", "employees",
            pipeline=pipeline,
            explain=True,
        )
        collection_name = "employees"

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month is required for late_leaderboard explain")
        first_day, last_day = _month_range(month)
        emp_match_: dict = {}
        if department:
            emp_match_["department"] = department
        pipeline = [
            {"$match": emp_match_} if emp_match_ else {"$match": {}},
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "let":  {"ec": "$emp_code"},
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {
                                    "$and": [
                                        {"$eq":  ["$emp_code", "$$ec"]},
                                        {"$gte": ["$date", first_day]},
                                        {"$lte": ["$date", last_day]},
                                        {"$gt":  [{"$ifNull": ["$late_minutes", 0]}, 0]},
                                    ]
                                }
                            }
                        }
                    ],
                    "as": "late_logs",
                }
            },
        ]
        explain_result = db.command(
            "aggregate", "employees",
            pipeline=pipeline,
            explain=True,
        )
        collection_name = "employees"

    elif endpoint == "department_trend":
        if not department:
            raise HTTPException(422, "department is required for department_trend explain")
        if not from_date or not to_date:
            raise HTTPException(422, "from and to are required for department_trend explain")
        try:
            fd = date_type.fromisoformat(from_date)
            td = date_type.fromisoformat(to_date)
        except ValueError:
            raise HTTPException(422, "from and to must be YYYY-MM-DD")
        if td < fd:
            raise HTTPException(422, "to must not be before from")
        pipeline = [
            {"$match": {"date": {"$gte": from_date, "$lte": to_date}}},
            {
                "$lookup": {
                    "from": "employees",
                    "localField": "emp_code",
                    "foreignField": "emp_code",
                    "as": "emp_doc",
                }
            },
            {"$unwind": "$emp_doc"},
            {"$match": {"emp_doc.department": department}},
        ]
        explain_result = db.command(
            "aggregate", "attendance_logs",
            pipeline=pipeline,
            explain=True,
        )
        collection_name = "attendance_logs"

    return {
        "endpoint":   endpoint,
        "collection": collection_name,
        "explain":    _bson_to_json(explain_result),
    }
