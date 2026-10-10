"""
Comprehensive tests for the Employee Attendance & Analytics API.

Run:
    pytest tests/ -v

Uses an isolated test database (attendance_test_db) that is created/torn down
per test session. Never touches production databases.
"""
import math
import threading
import time
from datetime import datetime, timedelta, timezone, date as date_type

import pytest
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Test DB setup — override env vars BEFORE importing app.main
# ---------------------------------------------------------------------------
import os
from dotenv import load_dotenv
load_dotenv()

os.environ.setdefault("MONGO_URI", "mongodb://localhost:27017")
os.environ["MONGO_DB"] = "attendance_test_db"

from app.main import app, db, client as mongo_client  # noqa: E402  (after env override)

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))

# Epoch-millisecond helpers
def to_ms(dt: datetime) -> int:
    return int(dt.astimezone(UTC).timestamp()) * 1000


def ist(year, month, day, hour, minute, second=0) -> datetime:
    return datetime(year, month, day, hour, minute, second, tzinfo=IST)


# Sample epoch-ms values
TS_PUNCH_IN  = to_ms(ist(2026, 7, 6, 9, 28, 0))   # 09:28 IST — on time
TS_PUNCH_OUT = to_ms(ist(2026, 7, 6, 18, 35, 0))  # 18:35 IST — 5 min OT (below 30)
TS_LATE      = to_ms(ist(2026, 7, 6, 10, 5, 0))   # 10:05 IST — 35 min late

EMPLOYEE_BASE = {
    "emp_code":    "EMP9001",
    "name":        "Test User",
    "email":       "test@example.com",
    "department":  "Engineering",
    "shift_start": "09:30",
    "shift_end":   "18:30",
    "joined_on":   "2026-01-01",
}


# ---------------------------------------------------------------------------
# Session-scoped fixture: fresh test DB
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session", autouse=True)
def clean_db():
    """Drop the test database at the start and end of the test session."""
    mongo_client.drop_database("attendance_test_db")
    yield
    mongo_client.drop_database("attendance_test_db")


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def wipe_collections():
    """Clean both collections before every test."""
    db.employees.delete_many({})
    db.attendance_logs.delete_many({})
    yield
    db.employees.delete_many({})
    db.attendance_logs.delete_many({})


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def create_emp(client, **kwargs):
    data = {**EMPLOYEE_BASE, **kwargs}
    r = client.post("/employees", json=data)
    assert r.status_code == 201, r.text
    return r.json()


def punch_in_emp(client, emp_code=EMPLOYEE_BASE["emp_code"], punched_at=TS_PUNCH_IN, status="PRESENT"):
    r = client.post("/attendance/punch-in", json={"emp_code": emp_code, "punched_at": punched_at, "status": status})
    return r


# ===========================================================================
# 1. Health check
# ===========================================================================

class TestHealth:
    def test_health_ok(self, client):
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}


# ===========================================================================
# 2. Employee CRUD
# ===========================================================================

class TestEmployees:
    def test_create_employee(self, client):
        r = client.post("/employees", json=EMPLOYEE_BASE)
        assert r.status_code == 201
        body = r.json()
        assert body["emp_code"] == "EMP9001"
        assert "created_at" in body
        assert isinstance(body["created_at"], int)
        assert body["created_at"] >= 100_000_000_000

    def test_created_at_is_epoch_ms(self, client):
        r = client.post("/employees", json=EMPLOYEE_BASE)
        body = r.json()
        # created_at must be epoch-ms (>= 100_000_000_000)
        assert body["created_at"] >= 100_000_000_000

    def test_duplicate_emp_code_409(self, client):
        client.post("/employees", json=EMPLOYEE_BASE)
        r = client.post("/employees", json=EMPLOYEE_BASE)
        assert r.status_code == 409

    def test_shift_start_eq_end_422(self, client):
        data = {**EMPLOYEE_BASE, "shift_start": "09:30", "shift_end": "09:30"}
        r = client.post("/employees", json=data)
        assert r.status_code == 422

    def test_list_employees_sorted(self, client):
        create_emp(client, emp_code="EMP9003", name="C")
        create_emp(client, emp_code="EMP9001", name="A")
        create_emp(client, emp_code="EMP9002", name="B")
        r = client.get("/employees")
        assert r.status_code == 200
        codes = [e["emp_code"] for e in r.json()["items"]]
        assert codes == sorted(codes)

    def test_list_employees_dept_filter(self, client):
        create_emp(client, emp_code="EMP9001", department="Eng")
        create_emp(client, emp_code="EMP9002", department="Sales")
        r = client.get("/employees", params={"department": "Eng"})
        body = r.json()
        assert body["total"] == 1
        assert body["items"][0]["department"] == "Eng"

    def test_list_employees_pagination(self, client):
        for i in range(5):
            create_emp(client, emp_code=f"EMP900{i}", name=f"User {i}", email=f"u{i}@x.com")
        r = client.get("/employees", params={"page": 2, "page_size": 2})
        body = r.json()
        assert body["total"] == 5
        assert len(body["items"]) == 2
        assert body["page"] == 2

    def test_list_total_reflects_filter(self, client):
        create_emp(client, emp_code="EMP9001", department="Eng")
        create_emp(client, emp_code="EMP9002", department="Sales")
        r = client.get("/employees", params={"department": "Eng"})
        assert r.json()["total"] == 1   # not 2

    def test_page_size_max_100(self, client):
        r = client.get("/employees", params={"page_size": 101})
        assert r.status_code == 422

    def test_emp_code_not_in_response_as_id(self, client):
        r = client.post("/employees", json=EMPLOYEE_BASE)
        body = r.json()
        assert "_id" not in body


# ===========================================================================
# 3. Punch-in
# ===========================================================================

class TestPunchIn:
    def test_punch_in_creates_record(self, client):
        create_emp(client)
        r = punch_in_emp(client)
        assert r.status_code == 201
        body = r.json()
        assert body["emp_code"] == EMPLOYEE_BASE["emp_code"]
        assert body["punch_in"] == TS_PUNCH_IN
        assert body["punch_out"] is None
        assert body["work_hours"] is None
        assert body["late_minutes"] == 0
        assert body["overtime_minutes"] == 0
        assert body["half_day"] is False
        assert body["history"] == []

    def test_punch_in_unknown_employee_404(self, client):
        r = client.post("/attendance/punch-in", json={"emp_code": "EMP0000", "punched_at": TS_PUNCH_IN})
        assert r.status_code == 404

    def test_punch_in_duplicate_409(self, client):
        create_emp(client)
        punch_in_emp(client)
        r = punch_in_emp(client)
        assert r.status_code == 409

    def test_punch_in_wrong_status_422(self, client):
        create_emp(client)
        r = client.post("/attendance/punch-in", json={
            "emp_code": EMPLOYEE_BASE["emp_code"],
            "punched_at": TS_PUNCH_IN,
            "status": "ABSENT"
        })
        assert r.status_code == 422

    def test_punch_in_timestamp_seconds_rejected(self, client):
        """Value < 100_000_000_000 looks like seconds, must be 422."""
        create_emp(client)
        r = client.post("/attendance/punch-in", json={
            "emp_code": EMPLOYEE_BASE["emp_code"],
            "punched_at": 1_700_000_000,   # seconds-era value
        })
        assert r.status_code == 422

    def test_punch_in_float_rejected(self, client):
        create_emp(client)
        r = client.post("/attendance/punch-in", json={
            "emp_code": EMPLOYEE_BASE["emp_code"],
            "punched_at": 1783312500000.5,
        })
        assert r.status_code == 422

    def test_punch_in_string_rejected(self, client):
        create_emp(client)
        r = client.post("/attendance/punch-in", json={
            "emp_code": EMPLOYEE_BASE["emp_code"],
            "punched_at": "2026-07-06T09:28:00Z",
        })
        assert r.status_code == 422

    # R2 — Late boundary tests
    def test_late_boundary_exactly_10min_not_late(self, client):
        """09:40:00 → late_minutes = 0 (exactly 10 min grace)."""
        create_emp(client)
        ts = to_ms(ist(2026, 7, 6, 9, 40, 0))
        r = punch_in_emp(client, punched_at=ts)
        assert r.status_code == 201
        assert r.json()["late_minutes"] == 0

    def test_late_boundary_10min_1sec_is_late(self, client):
        """09:40:01 → late_minutes = 10 (floor of 10m1s)."""
        create_emp(client)
        ts = to_ms(ist(2026, 7, 6, 9, 40, 1))
        r = punch_in_emp(client, punched_at=ts)
        assert r.status_code == 201
        assert r.json()["late_minutes"] == 10

    def test_late_boundary_45min(self, client):
        """10:15:59 → late_minutes = 45 (floor of 45m59s)."""
        create_emp(client)
        ts = to_ms(ist(2026, 7, 6, 10, 15, 59))
        r = punch_in_emp(client, punched_at=ts)
        assert r.status_code == 201
        assert r.json()["late_minutes"] == 45

    def test_punch_in_late(self, client):
        """10:05:00 → 35 min late."""
        create_emp(client)
        r = punch_in_emp(client, punched_at=TS_LATE)
        assert r.status_code == 201
        assert r.json()["late_minutes"] == 35

    def test_ist_date_derived_correctly(self, client):
        """The date field uses IST calendar date."""
        create_emp(client)
        ts = to_ms(ist(2026, 7, 6, 9, 28, 0))
        r = punch_in_emp(client, punched_at=ts)
        assert r.json()["date"] == "2026-07-06"

    # Overnight shift
    def test_overnight_shift_date(self, client):
        """EMP with 22:00-06:00 shift. Punch-in at 21:55 IST → date is same day."""
        create_emp(client, emp_code="EMP9010", shift_start="22:00", shift_end="06:00")
        ts = to_ms(ist(2026, 7, 6, 21, 55, 0))
        r = client.post("/attendance/punch-in", json={"emp_code": "EMP9010", "punched_at": ts})
        assert r.status_code == 201
        assert r.json()["date"] == "2026-07-06"

    def test_overnight_punch_in_before_shift_end_belongs_to_previous_day(self, client):
        """Punch-in at 02:00 IST on 7th with 22:00-06:00 shift → date is 6th."""
        create_emp(client, emp_code="EMP9011", shift_start="22:00", shift_end="06:00")
        ts = to_ms(ist(2026, 7, 7, 2, 0, 0))
        # First we need to create a punch-in for the 6th
        ts_prev = to_ms(ist(2026, 7, 6, 22, 30, 0))
        r1 = client.post("/attendance/punch-in", json={"emp_code": "EMP9011", "punched_at": ts_prev})
        assert r1.status_code == 201
        assert r1.json()["date"] == "2026-07-06"

    def test_wfh_status_accepted(self, client):
        create_emp(client)
        ts = to_ms(ist(2026, 7, 6, 9, 28, 0))
        r = client.post("/attendance/punch-in", json={
            "emp_code": EMPLOYEE_BASE["emp_code"],
            "punched_at": ts,
            "status": "WFH",
        })
        assert r.status_code == 201
        assert r.json()["status"] == "WFH"

    def test_on_duty_status_accepted(self, client):
        create_emp(client)
        ts = to_ms(ist(2026, 7, 6, 9, 28, 0))
        r = client.post("/attendance/punch-in", json={
            "emp_code": EMPLOYEE_BASE["emp_code"],
            "punched_at": ts,
            "status": "ON_DUTY",
        })
        assert r.status_code == 201
        assert r.json()["status"] == "ON_DUTY"


# ===========================================================================
# 4. Punch-out
# ===========================================================================

class TestPunchOut:
    def _do_punch_in(self, client, ts_in=None):
        create_emp(client)
        ts_in = ts_in or TS_PUNCH_IN
        r = punch_in_emp(client, punched_at=ts_in)
        assert r.status_code == 201
        return r.json()

    def test_punch_out_success(self, client):
        self._do_punch_in(client)
        r = client.post("/attendance/punch-out", json={
            "emp_code": EMPLOYEE_BASE["emp_code"],
            "punched_at": TS_PUNCH_OUT,
        })
        assert r.status_code == 200
        body = r.json()
        assert body["punch_out"] == TS_PUNCH_OUT
        assert body["work_hours"] is not None
        assert body["half_day"] is False

    def test_work_hours_computed_r4(self, client):
        """(18:35 - 09:28) = 9h7m = 9.1166... → rounded half-up to 9.12."""
        self._do_punch_in(client)
        r = client.post("/attendance/punch-out", json={
            "emp_code": EMPLOYEE_BASE["emp_code"],
            "punched_at": TS_PUNCH_OUT,
        })
        assert r.status_code == 200
        assert r.json()["work_hours"] == 9.12

    def test_overtime_below_30_min_is_zero(self, client):
        """18:35 is only 5 min past shift end — below 30 min threshold."""
        self._do_punch_in(client)
        r = client.post("/attendance/punch-out", json={
            "emp_code": EMPLOYEE_BASE["emp_code"],
            "punched_at": TS_PUNCH_OUT,
        })
        assert r.json()["overtime_minutes"] == 0

    def test_overtime_at_30_min(self, client):
        """Exactly 30 min after shift end → counts as overtime."""
        self._do_punch_in(client)
        ts_out = to_ms(ist(2026, 7, 6, 19, 0, 0))  # 18:30 + 30 min
        r = client.post("/attendance/punch-out", json={
            "emp_code": EMPLOYEE_BASE["emp_code"],
            "punched_at": ts_out,
        })
        assert r.json()["overtime_minutes"] == 30

    def test_overtime_above_30_min(self, client):
        """40 min past shift end."""
        self._do_punch_in(client)
        ts_out = to_ms(ist(2026, 7, 6, 19, 10, 0))
        r = client.post("/attendance/punch-out", json={
            "emp_code": EMPLOYEE_BASE["emp_code"],
            "punched_at": ts_out,
        })
        assert r.json()["overtime_minutes"] == 40

    def test_half_day_below_4_5_hours(self, client):
        """3.5 hours → half_day = True."""
        ts_in  = to_ms(ist(2026, 7, 6, 9, 30, 0))
        ts_out = to_ms(ist(2026, 7, 6, 13, 0, 0))
        create_emp(client)
        punch_in_emp(client, punched_at=ts_in)
        r = client.post("/attendance/punch-out", json={
            "emp_code": EMPLOYEE_BASE["emp_code"],
            "punched_at": ts_out,
        })
        assert r.json()["half_day"] is True
        assert r.json()["work_hours"] == 3.5

    def test_half_day_exactly_4_5_is_false(self, client):
        """Exactly 4.50 hours → half_day = False (not strictly below)."""
        ts_in  = to_ms(ist(2026, 7, 6, 9, 30, 0))
        ts_out = to_ms(ist(2026, 7, 6, 14, 0, 0))  # exactly 4.5 hours
        create_emp(client)
        punch_in_emp(client, punched_at=ts_in)
        r = client.post("/attendance/punch-out", json={
            "emp_code": EMPLOYEE_BASE["emp_code"],
            "punched_at": ts_out,
        })
        assert r.json()["half_day"] is False
        assert r.json()["work_hours"] == 4.5

    def test_punch_out_before_punch_in_422(self, client):
        self._do_punch_in(client)
        ts_bad = to_ms(ist(2026, 7, 6, 8, 0, 0))
        r = client.post("/attendance/punch-out", json={
            "emp_code": EMPLOYEE_BASE["emp_code"],
            "punched_at": ts_bad,
        })
        assert r.status_code == 422

    def test_punch_out_more_than_24h_422(self, client):
        self._do_punch_in(client)
        ts_far = to_ms(ist(2026, 7, 8, 9, 28, 0))  # 2 days later
        r = client.post("/attendance/punch-out", json={
            "emp_code": EMPLOYEE_BASE["emp_code"],
            "punched_at": ts_far,
        })
        assert r.status_code == 422

    def test_punch_out_unknown_employee_404(self, client):
        r = client.post("/attendance/punch-out", json={"emp_code": "EMP0000", "punched_at": TS_PUNCH_OUT})
        assert r.status_code == 404

    def test_punch_out_no_open_record_404(self, client):
        create_emp(client)
        r = client.post("/attendance/punch-out", json={
            "emp_code": EMPLOYEE_BASE["emp_code"],
            "punched_at": TS_PUNCH_OUT,
        })
        assert r.status_code == 404

    def test_punch_out_already_closed_409(self, client):
        self._do_punch_in(client)
        client.post("/attendance/punch-out", json={
            "emp_code": EMPLOYEE_BASE["emp_code"],
            "punched_at": TS_PUNCH_OUT,
        })
        r = client.post("/attendance/punch-out", json={
            "emp_code": EMPLOYEE_BASE["emp_code"],
            "punched_at": TS_PUNCH_OUT,
        })
        assert r.status_code == 409

    def test_overnight_shift_overtime(self, client):
        """Overnight 22:00-06:00 shift. Punch-out at 06:40 next day → 40 min OT."""
        create_emp(client, emp_code="EMP9020", shift_start="22:00", shift_end="06:00")
        ts_in  = to_ms(ist(2026, 7, 6, 21, 55, 0))
        ts_out = to_ms(ist(2026, 7, 7, 6, 40, 0))
        client.post("/attendance/punch-in", json={"emp_code": "EMP9020", "punched_at": ts_in})
        r = client.post("/attendance/punch-out", json={"emp_code": "EMP9020", "punched_at": ts_out})
        assert r.status_code == 200
        assert r.json()["overtime_minutes"] == 40


# ===========================================================================
# 5. Attendance list
# ===========================================================================

class TestAttendanceList:
    def _seed(self, client):
        create_emp(client, emp_code="EMP9001")
        create_emp(client, emp_code="EMP9002", email="b@x.com")
        # day 1
        punch_in_emp(client, emp_code="EMP9001", punched_at=to_ms(ist(2026, 7, 6, 9, 28, 0)))
        # day 2
        punch_in_emp(client, emp_code="EMP9001", punched_at=to_ms(ist(2026, 7, 7, 9, 28, 0)))
        punch_in_emp(client, emp_code="EMP9002", punched_at=to_ms(ist(2026, 7, 7, 9, 28, 0)))

    def test_list_sorted_date_desc_emp_asc(self, client):
        self._seed(client)
        r = client.get("/attendance")
        items = r.json()["items"]
        dates = [x["date"] for x in items]
        assert dates == sorted(dates, reverse=True)

    def test_list_total_reflects_filters(self, client):
        self._seed(client)
        r = client.get("/attendance", params={"emp_code": "EMP9001"})
        assert r.json()["total"] == 2

    def test_list_date_range(self, client):
        self._seed(client)
        r = client.get("/attendance", params={"date_from": "2026-07-07", "date_to": "2026-07-07"})
        assert r.json()["total"] == 2

    def test_date_from_after_date_to_422(self, client):
        r = client.get("/attendance", params={"date_from": "2026-07-08", "date_to": "2026-07-06"})
        assert r.status_code == 422

    def test_list_pagination(self, client):
        self._seed(client)
        r = client.get("/attendance", params={"page": 1, "page_size": 2})
        body = r.json()
        assert len(body["items"]) == 2
        assert body["total"] == 3

    def test_list_no_collscan_possible(self, client):
        """Smoke test: query executes fast (index exists)."""
        self._seed(client)
        r = client.get("/attendance")
        assert r.status_code == 200

    def test_missing_history_field_returned_as_empty(self, client):
        """Legacy docs without history must return history: []"""
        # Insert a legacy doc directly
        from app.main import db as test_db, IST, UTC
        ts = datetime(2026, 7, 14, 4, 2, 0, tzinfo=UTC)
        test_db.attendance_logs.insert_one({
            "emp_code": "EMP9001",
            "date": "2026-07-14",
            "status": "PRESENT",
            "punch_in": ts,
            "punch_out": datetime(2026, 7, 14, 13, 1, 0, tzinfo=UTC),
            "work_hours": 8.98,
            "late_minutes": 0,
            "overtime_minutes": 0,
            # no half_day, no history fields
        })
        create_emp(client)
        r = client.get("/attendance", params={"emp_code": "EMP9001"})
        item = r.json()["items"][0]
        assert item["history"] == []
        assert item["half_day"] is False


# ===========================================================================
# 6. Regularize attendance (PATCH)
# ===========================================================================

class TestRegularize:
    def _setup(self, client):
        create_emp(client)
        punch_in_emp(client, punched_at=TS_LATE)  # 10:05 IST, late
        return {"emp_code": EMPLOYEE_BASE["emp_code"], "date": "2026-07-06"}

    def test_regularize_changes_punch_in(self, client):
        rec = self._setup(client)
        new_ts = to_ms(ist(2026, 7, 6, 9, 28, 0))
        r = client.patch(f"/attendance/{rec['emp_code']}/{rec['date']}", json={
            "punch_in": new_ts,
            "reason": "biometric glitch",
            "regularized_by": "hr.admin",
        })
        assert r.status_code == 200
        body = r.json()
        assert body["punch_in"] == new_ts
        assert body["late_minutes"] == 0  # recomputed
        assert len(body["history"]) == 1
        h = body["history"][0]
        assert h["by"] == "hr.admin"
        assert h["reason"] == "biometric glitch"
        assert "punch_in" in h["changes"]
        assert "late_minutes" in h["changes"]

    def test_regularize_no_change_422(self, client):
        rec = self._setup(client)
        r = client.patch(f"/attendance/{rec['emp_code']}/{rec['date']}", json={
            "reason": "no change",
            "regularized_by": "hr.admin",
            # no actual field change
        })
        assert r.status_code == 422

    def test_regularize_absent_with_punch_in_422(self, client):
        rec = self._setup(client)
        r = client.patch(f"/attendance/{rec['emp_code']}/{rec['date']}", json={
            "status": "ABSENT",
            "punch_in": TS_PUNCH_IN,
            "reason": "test",
            "regularized_by": "admin",
        })
        assert r.status_code == 422

    def test_regularize_unknown_employee_404(self, client):
        r = client.patch("/attendance/EMP9999/2026-07-06", json={
            "reason": "test",
            "regularized_by": "admin",
        })
        assert r.status_code == 404

    def test_regularize_unknown_record_404(self, client):
        create_emp(client)
        r = client.patch(f"/attendance/{EMPLOYEE_BASE['emp_code']}/2026-07-06", json={
            "reason": "test",
            "regularized_by": "admin",
        })
        assert r.status_code == 404

    def test_regularize_short_reason_422(self, client):
        rec = self._setup(client)
        r = client.patch(f"/attendance/{rec['emp_code']}/{rec['date']}", json={
            "punch_in": to_ms(ist(2026, 7, 6, 9, 28, 0)),
            "reason": "x",  # too short
            "regularized_by": "admin",
        })
        assert r.status_code == 422

    def test_regularize_appends_history_entries(self, client):
        rec = self._setup(client)
        # First correction
        client.patch(f"/attendance/{rec['emp_code']}/{rec['date']}", json={
            "punch_in": to_ms(ist(2026, 7, 6, 9, 28, 0)),
            "reason": "biometric glitch",
            "regularized_by": "hr.admin",
        })
        # Second correction
        r = client.patch(f"/attendance/{rec['emp_code']}/{rec['date']}", json={
            "punch_in": to_ms(ist(2026, 7, 6, 9, 35, 0)),
            "reason": "correction again",
            "regularized_by": "hr.admin",
        })
        body = r.json()
        assert len(body["history"]) == 2

    def test_regularize_to_absent_clears_punch_times(self, client):
        rec = self._setup(client)
        r = client.patch(f"/attendance/{rec['emp_code']}/{rec['date']}", json={
            "status": "ABSENT",
            "reason": "approved absence",
            "regularized_by": "hr.admin",
        })
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ABSENT"
        assert body["punch_in"] is None
        assert body["punch_out"] is None

    def test_punch_out_wrong_date_422(self, client):
        """punch_out before punch_in → 422."""
        rec = self._setup(client)
        ts_bad = to_ms(ist(2026, 7, 6, 8, 0, 0))  # before punch_in
        ts_in  = to_ms(ist(2026, 7, 6, 9, 28, 0))
        r = client.patch(f"/attendance/{rec['emp_code']}/{rec['date']}", json={
            "punch_in":  ts_in,
            "punch_out": ts_bad,
            "reason": "testing invalid order",
            "regularized_by": "admin",
        })
        assert r.status_code == 422


# ===========================================================================
# 7. Analytics — Employee Monthly
# ===========================================================================

class TestEmployeeMonthly:
    def test_unknown_employee_404(self, client):
        r = client.get("/analytics/employees/EMP9999/monthly", params={"month": "2026-07"})
        assert r.status_code == 404

    def test_empty_month(self, client):
        create_emp(client)
        r = client.get(f"/analytics/employees/{EMPLOYEE_BASE['emp_code']}/monthly",
                       params={"month": "2026-07"})
        body = r.json()
        assert body["present_days"] == 0
        assert body["leave_days"] == 0
        assert body["late_count"] == 0
        assert body["total_late_minutes"] == 0

    def test_working_days_from_joined_on(self, client):
        """Employee who joined 2026-07-13 (Monday) — only 15 working days in July."""
        create_emp(client, joined_on="2026-07-13")
        r = client.get(f"/analytics/employees/{EMPLOYEE_BASE['emp_code']}/monthly",
                       params={"month": "2026-07"})
        body = r.json()
        # July 2026 weekdays from 13th to 31st
        from app.main import _working_days_in_month
        expected = _working_days_in_month("2026-07", "2026-07-13")
        assert body["working_days"] == expected

    def test_present_days_half_day(self, client):
        """One half-day (3.5 hours) should contribute 0.5 to present_days."""
        create_emp(client, joined_on="2026-07-01")
        ts_in  = to_ms(ist(2026, 7, 7, 9, 30, 0))  # Tuesday
        ts_out = to_ms(ist(2026, 7, 7, 13, 0, 0))   # 3.5 hours
        punch_in_emp(client, punched_at=ts_in)
        client.post("/attendance/punch-out", json={
            "emp_code": EMPLOYEE_BASE["emp_code"],
            "punched_at": ts_out,
        })
        r = client.get(f"/analytics/employees/{EMPLOYEE_BASE['emp_code']}/monthly",
                       params={"month": "2026-07"})
        assert r.json()["present_days"] == 0.5

    def test_attendance_pct_null_when_working_days_zero(self, client):
        """If no working days in the period, attendance_pct must be null."""
        # Join after end of month
        create_emp(client, joined_on="2026-08-01")
        r = client.get(f"/analytics/employees/{EMPLOYEE_BASE['emp_code']}/monthly",
                       params={"month": "2026-07"})
        body = r.json()
        assert body["working_days"] == 0
        assert body["attendance_pct"] is None

    def test_leave_days_counted(self, client):
        from app.main import db as test_db, UTC
        create_emp(client)
        test_db.attendance_logs.insert_one({
            "emp_code": EMPLOYEE_BASE["emp_code"],
            "date": "2026-07-08",
            "status": "LEAVE",
            "punch_in": None, "punch_out": None,
            "work_hours": None, "late_minutes": 0,
            "overtime_minutes": 0, "half_day": False, "history": [],
        })
        r = client.get(f"/analytics/employees/{EMPLOYEE_BASE['emp_code']}/monthly",
                       params={"month": "2026-07"})
        assert r.json()["leave_days"] == 1

    def test_invalid_month_format_422(self, client):
        r = client.get("/analytics/employees/EMP9001/monthly", params={"month": "2026-13"})
        assert r.status_code == 422


# ===========================================================================
# 8. Analytics — Department Summary
# ===========================================================================

class TestDepartmentSummary:
    def test_employees_with_no_logs_counted(self, client):
        """Employee with zero logs still counted in headcount (R9)."""
        create_emp(client, emp_code="EMP9001", department="Eng", joined_on="2026-07-01")
        create_emp(client, emp_code="EMP9002", department="Eng", email="b@x.com", joined_on="2026-07-01")
        # EMP9001 punches in, EMP9002 does not
        punch_in_emp(client, emp_code="EMP9001", punched_at=to_ms(ist(2026, 7, 7, 9, 28, 0)))
        r = client.get("/analytics/departments/summary", params={"month": "2026-07"})
        body = r.json()
        eng = next(i for i in body["items"] if i["department"] == "Eng")
        assert eng["headcount"] == 2

    def test_employee_joined_after_month_excluded(self, client):
        """Employee who joined 2026-08-01 must not count in July headcount."""
        create_emp(client, emp_code="EMP9001", department="Eng", joined_on="2026-08-01")
        r = client.get("/analytics/departments/summary", params={"month": "2026-07"})
        body = r.json()
        assert body["items"] == []

    def test_avg_work_hours_over_records(self, client):
        """avg_work_hours is average of work_hours values, not average of per-employee avg."""
        create_emp(client, emp_code="EMP9001", department="Eng", joined_on="2026-07-01")
        create_emp(client, emp_code="EMP9002", department="Eng", email="b@x.com", joined_on="2026-07-01")
        from app.main import db as test_db, UTC
        test_db.attendance_logs.insert_many([
            {
                "emp_code": "EMP9001", "date": "2026-07-07", "status": "PRESENT",
                "punch_in": datetime(2026, 7, 7, 4, 0, tzinfo=UTC),
                "punch_out": datetime(2026, 7, 7, 12, 0, tzinfo=UTC),
                "work_hours": 8.0, "late_minutes": 0, "overtime_minutes": 0,
                "half_day": False, "history": [],
            },
            {
                "emp_code": "EMP9002", "date": "2026-07-07", "status": "PRESENT",
                "punch_in": datetime(2026, 7, 7, 4, 0, tzinfo=UTC),
                "punch_out": datetime(2026, 7, 7, 10, 0, tzinfo=UTC),
                "work_hours": 6.0, "late_minutes": 0, "overtime_minutes": 0,
                "half_day": False, "history": [],
            },
        ])
        r = client.get("/analytics/departments/summary", params={"month": "2026-07"})
        eng = next(i for i in r.json()["items"] if i["department"] == "Eng")
        # avg of 8.0 and 6.0 = 7.0
        assert eng["avg_work_hours"] == 7.0

    def test_dept_filter(self, client):
        create_emp(client, emp_code="EMP9001", department="Eng", joined_on="2026-07-01")
        create_emp(client, emp_code="EMP9002", department="Sales", email="b@x.com", joined_on="2026-07-01")
        r = client.get("/analytics/departments/summary", params={"month": "2026-07", "department": "Sales"})
        body = r.json()
        assert len(body["items"]) == 1
        assert body["items"][0]["department"] == "Sales"


# ===========================================================================
# 9. Analytics — Late Leaderboard
# ===========================================================================

class TestLateLeaderboard:
    def _seed_late_employees(self, client):
        from app.main import db as test_db, UTC
        create_emp(client, emp_code="EMP9001", department="Eng", joined_on="2026-07-01")
        create_emp(client, emp_code="EMP9002", department="Eng", email="b@x.com", joined_on="2026-07-01")
        create_emp(client, emp_code="EMP9003", department="Eng", email="c@x.com", joined_on="2026-07-01")
        test_db.attendance_logs.insert_many([
            {"emp_code": "EMP9001", "date": "2026-07-07", "status": "PRESENT",
             "punch_in": datetime(2026, 7, 7, 5, 5, tzinfo=UTC),  # 10:35 IST → 65 min late
             "punch_out": None, "work_hours": None, "late_minutes": 65,
             "overtime_minutes": 0, "half_day": False, "history": []},
            {"emp_code": "EMP9002", "date": "2026-07-07", "status": "PRESENT",
             "punch_in": datetime(2026, 7, 7, 4, 35, tzinfo=UTC),  # 10:05 IST → 35 min late
             "punch_out": None, "work_hours": None, "late_minutes": 35,
             "overtime_minutes": 0, "half_day": False, "history": []},
            {"emp_code": "EMP9003", "date": "2026-07-07", "status": "PRESENT",
             "punch_in": datetime(2026, 7, 7, 4, 35, tzinfo=UTC),  # 35 min late — tied with EMP9002
             "punch_out": None, "work_hours": None, "late_minutes": 35,
             "overtime_minutes": 0, "half_day": False, "history": []},
        ])

    def test_leaderboard_order(self, client):
        self._seed_late_employees(client)
        r = client.get("/analytics/leaderboard/late", params={"month": "2026-07"})
        items = r.json()["items"]
        assert items[0]["emp_code"] == "EMP9001"

    def test_leaderboard_ranks(self, client):
        self._seed_late_employees(client)
        r = client.get("/analytics/leaderboard/late", params={"month": "2026-07"})
        items = r.json()["items"]
        # EMP9001 → rank 1; EMP9002 and EMP9003 tied → rank 2
        ranks = [i["rank"] for i in items]
        assert ranks[0] == 1
        # Both tied at rank 2 (no rank 3 — gap)
        assert ranks[1] == ranks[2] == 2

    def test_leaderboard_tie_at_cutoff_includes_all_tied(self, client):
        """If limit=1 and 2 people share rank 2, but none share rank 1, only 1 row returned."""
        self._seed_late_employees(client)
        r = client.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": 1})
        items = r.json()["items"]
        assert len(items) == 1
        assert items[0]["rank"] == 1

    def test_leaderboard_limit_at_tie_boundary(self, client):
        """If limit=2 and tied employees share rank 2, all tied at rank 2 are returned."""
        self._seed_late_employees(client)
        r = client.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": 2})
        items = r.json()["items"]
        # rank 1 (1 employee) + rank 2 (2 employees) = 3 rows
        assert len(items) == 3

    def test_only_employees_with_late_records_appear(self, client):
        from app.main import db as test_db, UTC
        create_emp(client, emp_code="EMP9001", joined_on="2026-07-01")
        test_db.attendance_logs.insert_one({
            "emp_code": "EMP9001", "date": "2026-07-07", "status": "PRESENT",
            "punch_in": datetime(2026, 7, 7, 4, 0, tzinfo=UTC),
            "punch_out": None, "work_hours": None, "late_minutes": 0,
            "overtime_minutes": 0, "half_day": False, "history": [],
        })
        r = client.get("/analytics/leaderboard/late", params={"month": "2026-07"})
        assert r.json()["items"] == []

    def test_dept_filter_leaderboard(self, client):
        self._seed_late_employees(client)
        r = client.get("/analytics/leaderboard/late", params={"month": "2026-07", "department": "Eng"})
        for item in r.json()["items"]:
            assert item["department"] == "Eng"


# ===========================================================================
# 10. Analytics — Department Trend
# ===========================================================================

class TestDepartmentTrend:
    def test_unknown_department_404(self, client):
        r = client.get("/analytics/departments/Nope/trend",
                       params={"from": "2026-07-01", "to": "2026-07-07"})
        assert r.status_code == 404

    def test_to_before_from_422(self, client):
        create_emp(client, department="Eng", joined_on="2026-07-01")
        r = client.get("/analytics/departments/Eng/trend",
                       params={"from": "2026-07-07", "to": "2026-07-01"})
        assert r.status_code == 422

    def test_range_over_92_days_422(self, client):
        create_emp(client, department="Eng", joined_on="2026-07-01")
        r = client.get("/analytics/departments/Eng/trend",
                       params={"from": "2026-07-01", "to": "2026-10-02"})  # 93 days
        assert r.status_code == 422

    def test_one_row_per_day(self, client):
        create_emp(client, department="Eng", joined_on="2026-07-01")
        r = client.get("/analytics/departments/Eng/trend",
                       params={"from": "2026-07-06", "to": "2026-07-10"})
        body = r.json()
        assert len(body["items"]) == 5

    def test_weekend_included(self, client):
        create_emp(client, department="Eng", joined_on="2026-07-01")
        r = client.get("/analytics/departments/Eng/trend",
                       params={"from": "2026-07-04", "to": "2026-07-05"})
        body = r.json()
        assert len(body["items"]) == 2
        # July 4 2026 is Saturday, July 5 is Sunday
        for item in body["items"]:
            assert item["is_working_day"] is False

    def test_attendance_rate_null_on_weekend(self, client):
        create_emp(client, department="Eng", joined_on="2026-07-01")
        r = client.get("/analytics/departments/Eng/trend",
                       params={"from": "2026-07-04", "to": "2026-07-05"})
        for item in r.json()["items"]:
            assert item["attendance_rate"] is None

    def test_headcount_respects_joined_on(self, client):
        """Employee who joins on 2026-07-06 has headcount=0 on the 5th."""
        create_emp(client, department="Eng", joined_on="2026-07-06")
        r = client.get("/analytics/departments/Eng/trend",
                       params={"from": "2026-07-05", "to": "2026-07-07"})
        items = r.json()["items"]
        by_date = {i["date"]: i for i in items}
        assert by_date["2026-07-05"]["headcount"] == 0
        assert by_date["2026-07-06"]["headcount"] == 1

    def test_moving_avg_7d_first_rows_use_shorter_window(self, client):
        """First row has moving_avg_7d from window of 1 (itself if non-null)."""
        create_emp(client, department="Eng", joined_on="2026-07-01")
        r = client.get("/analytics/departments/Eng/trend",
                       params={"from": "2026-07-07", "to": "2026-07-10"})
        # All are weekdays with headcount=1, no records → rate=0.0 on working days
        items = r.json()["items"]
        # First working day row should have moving_avg_7d = 0.0 (window of 1)
        first_wd = next(i for i in items if i["is_working_day"])
        assert first_wd["moving_avg_7d"] is not None


# ===========================================================================
# 11. Explain endpoint
# ===========================================================================

class TestExplain:
    def test_attendance_list_explain(self, client):
        r = client.get("/admin/explain/attendance_list",
                       params={"date_from": "2026-07-01", "date_to": "2026-07-31"})
        assert r.status_code == 200
        body = r.json()
        assert body["endpoint"] == "attendance_list"
        assert body["collection"] == "attendance_logs"
        assert "explain" in body

    def test_employee_monthly_requires_params(self, client):
        r = client.get("/admin/explain/employee_monthly")
        assert r.status_code == 422

    def test_invalid_endpoint_422(self, client):
        r = client.get("/admin/explain/unknown_endpoint")
        assert r.status_code == 422

    def test_department_summary_explain(self, client):
        r = client.get("/admin/explain/department_summary",
                       params={"month": "2026-07"})
        assert r.status_code == 200
        assert r.json()["collection"] == "employees"

    def test_late_leaderboard_explain(self, client):
        r = client.get("/admin/explain/late_leaderboard",
                       params={"month": "2026-07"})
        assert r.status_code == 200

    def test_department_trend_explain(self, client):
        create_emp(client, department="Eng", joined_on="2026-07-01")
        r = client.get("/admin/explain/department_trend",
                       params={"department": "Eng", "from": "2026-07-01", "to": "2026-07-07"})
        assert r.status_code == 200


# ===========================================================================
# 12. Concurrency tests (requires actual MongoDB connection)
# ===========================================================================

class TestConcurrency:
    def test_concurrent_punch_in_one_wins(self, client):
        """Two identical concurrent punch-ins: exactly one 201 and one 409."""
        create_emp(client)
        results = []
        errors  = []

        def do_punch():
            try:
                with TestClient(app) as c:
                    r = c.post("/attendance/punch-in", json={
                        "emp_code": EMPLOYEE_BASE["emp_code"],
                        "punched_at": TS_PUNCH_IN,
                    })
                    results.append(r.status_code)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=do_punch) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, errors
        assert results.count(201) == 1
        assert all(s in (201, 409) for s in results)

    def test_concurrent_punch_out_one_wins(self, client):
        """Two concurrent punch-outs: exactly one 200 and one 409."""
        create_emp(client)
        punch_in_emp(client)
        results = []

        def do_punch_out():
            with TestClient(app) as c:
                r = c.post("/attendance/punch-out", json={
                    "emp_code": EMPLOYEE_BASE["emp_code"],
                    "punched_at": TS_PUNCH_OUT,
                })
                results.append(r.status_code)

        threads = [threading.Thread(target=do_punch_out) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert results.count(200) == 1
        assert all(s in (200, 409) for s in results)


# ===========================================================================
# 13. Business rule helpers — unit tests
# ===========================================================================

class TestHelpers:
    def test_half_up_round(self):
        from app.main import half_up_round
        assert half_up_round(2.345, 2) == 2.35   # half-up
        assert half_up_round(2.355, 2) == 2.36
        assert half_up_round(2.5, 0) == 3.0
        assert half_up_round(0.125, 2) == 0.13

    def test_ms_to_utc_truncates_ms(self):
        from app.main import ms_to_utc
        ms = 1783312500999  # fractional ms
        dt = ms_to_utc(ms)
        assert dt.microsecond == 0   # truncated to whole second

    def test_compute_work_hours(self):
        from app.main import compute_work_hours, UTC
        t1 = datetime(2026, 7, 6, 4, 0, 0, tzinfo=UTC)
        t2 = datetime(2026, 7, 6, 13, 7, 12, tzinfo=UTC)  # 9h7m12s = 9.12 hours exactly
        assert compute_work_hours(t1, t2) == 9.12
