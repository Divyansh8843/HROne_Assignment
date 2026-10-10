# Employee Attendance & Analytics API

> **HROne Software Engineer Trainee — Technical Assignment**  
> Python · FastAPI · PyMongo · MongoDB 6.0+  
> **All 92 tests pass · 0 known gaps**

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [Request / Response Workflow](#2-request--response-workflow)
3. [Data Model](#3-data-model)
4. [Business Rules Implemented](#4-business-rules-implemented)
5. [MongoDB Index Strategy](#5-mongodb-index-strategy)
6. [Aggregation Pipelines](#6-aggregation-pipelines)
7. [Prerequisites](#7-prerequisites)
8. [Installation & Setup](#8-installation--setup)
9. [Environment Variables](#9-environment-variables)
10. [Running the API](#10-running-the-api)
11. [Running Tests](#11-running-tests)
12. [API Reference](#12-api-reference)
13. [Project Structure](#13-project-structure)
14. [Design Decisions Summary](#14-design-decisions-summary)
15. [Known Limitations & Future Work](#15-known-limitations--future-work)

---

## 1. Architecture Overview

```
┌─────────────────────────────────────────────────────────────┐
│                        Client (HTTP)                         │
└────────────────────────────┬────────────────────────────────┘
                             │  REST / JSON
┌────────────────────────────▼────────────────────────────────┐
│                  FastAPI Application                         │
│                                                             │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────┐  │
│  │  Pydantic    │  │   Route      │  │  Business Logic  │  │
│  │  Validation  │→ │  Handlers    │→ │  (rules R1-R9)   │  │
│  └──────────────┘  └──────┬───────┘  └──────────────────┘  │
│                           │                                  │
│                    ┌──────▼───────┐                         │
│                    │  PyMongo     │                         │
│                    │  Driver      │                         │
│                    └──────┬───────┘                         │
└───────────────────────────┼─────────────────────────────────┘
                            │  Wire Protocol
┌───────────────────────────▼─────────────────────────────────┐
│                   MongoDB 6.0+                               │
│                                                             │
│   ┌──────────────────┐      ┌──────────────────────────┐   │
│   │   employees      │      │    attendance_logs        │   │
│   │   collection     │      │    collection             │   │
│   │                  │      │                           │   │
│   │  idx_emp_code    │      │  idx_emp_date (unique)    │   │
│   │  (unique)        │      │  idx_date_emp (sort)      │   │
│   │  idx_department  │      │  idx_status               │   │
│   │  idx_joined_on   │      │                           │   │
│   └──────────────────┘      └──────────────────────────┘   │
└─────────────────────────────────────────────────────────────┘
```

**Key design choices:**

| Layer | Technology | Reason |
|---|---|---|
| Web framework | FastAPI 0.110+ | Automatic OpenAPI docs, Pydantic v2 validation, async-capable |
| Database driver | PyMongo 4.7 (sync) | Straightforward for CRUD + aggregation; avoids Motor complexity for CPU-bound analytics |
| Schema validation | Pydantic v2 `field_validator` / `model_validator` | Strict epoch-ms range checks, status enum guards, cross-field rules |
| BSON serialization | `bson.json_util` | Handles ObjectId + datetime without a manual recursion walk |
| Timezone handling | `datetime.timezone` (stdlib) | No external dependency; IST = UTC+05:30 fixed offset |

---

## 2. Request / Response Workflow

### Punch-In Flow

```
POST /attendance/punch-in
        │
        ▼
 Pydantic validates body
 (emp_code format, epoch-ms bounds, status enum)
        │
        ▼
 Lookup employee in employees collection
 (raises 404 if not found)
        │
        ▼
 Convert epoch-ms → UTC datetime (tz-aware)
 Derive attendance date in IST (handles midnight/overnight)
        │
        ▼
 Compute late_minutes
 (punch_in vs shift_start in IST, 10-min grace period — R2)
        │
        ▼
 insert_one({ emp_code, date, status, punch_in, …, history:[] })
 Unique index (emp_code, date) → DuplicateKeyError → 409
        │
        ▼
 Return 201 + AttendanceRecord (epoch-ms punch_in, computed fields)
```

### Punch-Out Flow

```
POST /attendance/punch-out
        │
        ▼
 Pydantic validates body
        │
        ▼
 Lookup employee (404 if absent)
        │
        ▼
 find_one({ emp_code, punch_in: {$ne: null}, punch_out: null })
 sorted date DESC → most-recent open record
 (404 if none, 409 if already closed)
        │
        ▼
 Business-rule checks (R4):
   diff ≤ 0    → 422 "punch_out must be after punch_in"
   diff > 24 h → 422 "punch_out must be within 24 hours"
        │
        ▼
 Compute work_hours (R4), overtime_minutes (R3), half_day (R5)
        │
        ▼
 Atomic find_one_and_update({ _id, punch_out: null }, $set{…})
 Returns None → 409 (race-condition double punch-out)
        │
        ▼
 Return 200 + updated AttendanceRecord
```

### Regularize (PATCH) Flow

```
PATCH /attendance/{emp_code}/{date}
        │
        ▼
 Validate date format (YYYY-MM-DD)
 Lookup employee → 404 if absent
 Lookup attendance record → 404 if absent
        │
        ▼
 Apply business-rule guards:
   ABSENT/LEAVE + punch times → 422
   No effective field change   → 422
        │
        ▼
 Build $set payload (only changed fields)
 Recompute derived fields (late_minutes, work_hours, overtime, half_day)
        │
        ▼
 Build history entry { at, by, reason, changes: {field: {from, to}} }
 Atomic find_one_and_update with $set + $push history
        │
        ▼
 Return 200 + updated AttendanceRecord (history appended)
```

### Analytics Flow (example: Late Leaderboard)

```
GET /analytics/leaderboard/late?month=2026-07&limit=10
        │
        ▼
 Validate month parameter (regex ^\\d{4}-(0[1-9]|1[0-2])$)
 Derive first_day / last_day strings (YYYY-MM-DD)
        │
        ▼
 MongoDB Aggregation Pipeline:
   $match employees (optional dept filter)
   $lookup attendance_logs (date range + late_minutes > 0)
   $unwind late_logs (preserveNullAndEmptyArrays: false)
   $group by emp_code → sum late_minutes, name, department
   $match total_late_minutes > 0
   $setWindowFields sortBy total_late_minutes DESC → $rank
   $match rank <= limit
   $sort total_late_minutes DESC, emp_code ASC
        │
        ▼
 Return 200 + [ { emp_code, name, department,
                   total_late_minutes, rank } ]
```

---

## 3. Data Model

### `employees` collection

| Field | Type | Notes |
|---|---|---|
| `emp_code` | String | Unique, format `EMP\d{4,6}` |
| `name` | String | 1–100 chars |
| `email` | String | Unique, validated pattern |
| `department` | String | 1–50 chars |
| `joined_on` | String | `YYYY-MM-DD`, used as date boundary in aggregations |
| `shift_start` | String | `HH:MM` in IST |
| `shift_end` | String | `HH:MM` in IST (end < start → overnight shift) |
| `created_at` | DateTime | UTC, set at creation |

### `attendance_logs` collection

| Field | Type | Notes |
|---|---|---|
| `emp_code` | String | FK → employees |
| `date` | String | `YYYY-MM-DD` attendance day in IST |
| `status` | String | `PRESENT / ABSENT / LEAVE / WFH / ON_DUTY` |
| `punch_in` | DateTime (UTC) | Null for ABSENT/LEAVE |
| `punch_out` | DateTime (UTC) | Null until punched out |
| `work_hours` | Float | Null until punch-out; rounded half-up to 2 dp |
| `late_minutes` | Int | 0 if on time; floor whole minutes (R2) |
| `overtime_minutes` | Int | 0 if < 30 min past shift end (R3) |
| `half_day` | Bool | True when work_hours < 4.50 (R5) |
| `history` | Array | Audit trail of regularizations |

---

## 4. Business Rules Implemented

| Rule | Spec | Implementation |
|---|---|---|
| **R1** | Attendance date = IST calendar day of punch-in (overnight shifts use next day) | `dt_to_ist(punch_utc).date()` |
| **R2** | `late_minutes` = floor((punch_in − shift_start) / 60); ≤ 10 min grace → 0 | `compute_late_minutes()` — IST-aware shift-start construction |
| **R3** | `overtime_minutes` = floor((punch_out − shift_end) / 60); < 30 min → 0; overnight shift_end placed on next day | `compute_overtime_minutes_with_start()` |
| **R4** | `work_hours` = elapsed seconds / 3600, half-up to 2 dp; punch_out must be after punch_in and within 24 h | `compute_work_hours()` + validation in punch-out handler |
| **R5** | `half_day = work_hours < 4.50` | `compute_half_day()` |
| **R6** | One record per employee per calendar day; concurrent duplicates → exactly one 201, one 409 | Unique index + `DuplicateKeyError` catch |
| **R7** | Regularize: full audit trail; derived fields recomputed | `$push history` in atomic update |
| **R8** | Leaderboard: standard competition ranking; ties at limit boundary include all tied rows | `$setWindowFields $rank`, then `$match rank <= limit` |
| **R9** | Department trend headcount = employees with `joined_on <= day` | `$lookup` with `joined_on` filter in trend pipeline |

---

## 5. MongoDB Index Strategy

```python
# employees
employees.create_index([("emp_code", ASC)], unique=True)       # PK, all lookups
employees.create_index([("department", ASC)])                   # dept filter in analytics
employees.create_index([("joined_on", ASC)])                    # headcount boundary in trend

# attendance_logs
attendance_logs.create_index([("emp_code", ASC), ("date", ASC)], unique=True)   # natural key + race guard
attendance_logs.create_index([("date", DESC), ("emp_code", ASC)])                # list sort, no in-memory sort
attendance_logs.create_index([("status", ASC)])                                  # status filter
```

All indexes are created idempotently at startup via the `lifespan` context manager.

---

## 6. Aggregation Pipelines

| Endpoint | Stages | Special operators |
|---|---|---|
| `GET /analytics/employees/{emp_code}/monthly` | `$match` → `$group` | Conditional `$sum` for half-day weighting |
| `GET /analytics/departments/summary` | `$match` → `$lookup` → `$unwind` (preserve nulls) → `$group` → `$sort` | `$addToSet` for headcount deduplication |
| `GET /analytics/leaderboard/late` | `$match` → `$lookup` → `$unwind` → `$group` → `$match` → `$setWindowFields` → `$match` → `$sort` | `$rank` window function |
| `GET /analytics/departments/{dept}/trend` | `$match` → `$lookup` → `$unwind` → `$group` → `$densify` → `$lookup` (headcount) → `$sort` → `$setWindowFields` (7d MA) | `$densify` for gap-filling, `$avg` window |
| `GET /admin/explain/{endpoint}` | same pipeline per endpoint, run with `explain=True` | `bson.json_util` serialization |

Zero Python-side aggregation loops: all arithmetic and ranking runs inside MongoDB.

---

## 7. Prerequisites

- Python **3.11+**
- MongoDB **6.0+** — local instance, Docker, or [Atlas free M0](https://www.mongodb.com/atlas)
- Git

---

## 8. Installation & Setup

```bash
git clone <your-repo-url>
cd candidate_kit

# Create virtual environment
python -m venv .venv

# Activate (Windows)
.venv\Scripts\activate
# Activate (Linux / macOS)
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

### Seed sample data (optional)

```bash
python sample_seed.py
```

Loads the nine sample documents from `sample_data/` into MongoDB.  
The grader uses a **larger hidden dataset** — the sample is for shape verification only.

---

## 9. Environment Variables

Create a `.env` file in the project root (never commit it — it is in `.gitignore`):

```dotenv
# MongoDB connection string
MONGO_URI=mongodb://localhost:27017
# OR for Atlas:
# MONGO_URI=mongodb+srv://<user>:<password>@<cluster>.mongodb.net/?retryWrites=true&w=majority

# Database name (the app uses "attendance_db" by default)
MONGO_DB=attendance_db
```

| Variable | Default | Description |
|---|---|---|
| `MONGO_URI` | `mongodb://localhost:27017` | MongoDB connection string |
| `MONGO_DB` | `attendance_db` | Production database name |

> **Tests** use a separate `attendance_test_db` database (hard-coded in `tests/test_api.py`) and never touch the production database.

---

## 10. Running the API

```bash
uvicorn app.main:app --port 8000 --reload
```

| URL | Description |
|---|---|
| `http://localhost:8000/docs` | Swagger UI (interactive) |
| `http://localhost:8000/redoc` | ReDoc documentation |
| `http://localhost:8000/health` | Readiness probe |

---

## 11. Running Tests

```bash
pytest tests/ -v
```

Expected output:

```
92 passed in ~110s
```

Tests connect to `attendance_test_db` and drop it before and after the full session.  
Each individual test wipes both collections before and after execution (`wipe_collections` autouse fixture).

**Test categories covered:**

| Class | # Tests | What it covers |
|---|---|---|
| `TestHealth` | 1 | MongoDB ping, 503 on failure |
| `TestEmployees` | 7 | Create, list, pagination, filters, validation |
| `TestPunchIn` | 9 | IST date, late minutes, grace, status, race/409 |
| `TestPunchOut` | 13 | work_hours, overtime, half_day, 422, 409, overnight |
| `TestAttendanceList` | 7 | Sort, filter, pagination, date range |
| `TestRegularize` | 11 | Audit trail, derived recompute, 404, 422 |
| `TestEmployeeMonthly` | 8 | Aggregation correctness |
| `TestDepartmentSummary` | 7 | Cross-collection aggregation |
| `TestLateLeaderboard` | 11 | Ranking, ties, limit boundary, dept filter |
| `TestDepartmentTrend` | 10 | `$densify`, headcount, 7-day MA, weekend rows |
| `TestExplain` | 5 | Explain plans returned, correct collection names |
| `TestConcurrency` | 1 | Concurrent punch-in: exactly one 201, rest 409 |
| `TestHelpers` | 2 | Epoch-ms conversion, rounding |

---

## 12. API Reference

| Method | Path | Status codes | Description |
|---|---|---|---|
| `GET` | `/health` | 200, 503 | MongoDB ping |
| `POST` | `/employees` | 201, 409, 422 | Create employee |
| `GET` | `/employees` | 200, 422 | List employees (paginated, sorted by emp_code) |
| `POST` | `/attendance/punch-in` | 201, 404, 409, 422 | Punch in |
| `POST` | `/attendance/punch-out` | 200, 404, 409, 422 | Punch out |
| `GET` | `/attendance` | 200, 422 | List attendance records |
| `PATCH` | `/attendance/{emp_code}/{date}` | 200, 404, 422 | Regularize record + audit trail |
| `GET` | `/analytics/employees/{emp_code}/monthly` | 200, 404, 422 | Employee monthly summary |
| `GET` | `/analytics/departments/summary` | 200, 422 | Department summary |
| `GET` | `/analytics/leaderboard/late` | 200, 422 | Late-comers leaderboard |
| `GET` | `/analytics/departments/{department}/trend` | 200, 404, 422 | Daily trend + 7-day moving average |
| `GET` | `/admin/explain/{endpoint}` | 200, 422 | MongoDB query explain plan |

---

## 13. Project Structure

```
candidate_kit/
├── app/
│   ├── __init__.py
│   └── main.py              ← All API code (1 500+ lines, fully documented)
├── sample_data/
│   ├── employees.json        ← 4 sample employees
│   └── attendance_logs.json  ← 5 sample log entries
├── tests/
│   ├── __init__.py
│   └── test_api.py           ← 92 tests, session-scoped DB isolation
├── sample_seed.py            ← Loads sample_data/ into MongoDB
├── openapi.yaml              ← Official contract (provided, not modified)
├── DATA_MODEL.md             ← Field-level data model (provided)
├── REVIEW.md                 ← 11 defects found & fixed in starter code
├── DECISIONS.md              ← 5 design decisions with rationale
├── requirements.txt
└── README.md                 ← This file
```

> `.env` is intentionally excluded from the repository.

---

## 14. Design Decisions Summary

Full rationale in [DECISIONS.md](DECISIONS.md).

| # | Decision | Why |
|---|---|---|
| 1 | **Compound unique index `(emp_code, date)`** | Enforces one-record-per-employee-per-day atomically at DB layer; also the primary read index |
| 2 | **Race-free punch-in via `insert_one` + `DuplicateKeyError`** | No check-then-insert window; the DB constraint is the race guard |
| 3 | **Standard competition ranking with `$setWindowFields $rank`** | Correct tie semantics (1,2,2,4); `$match rank <= limit` returns all tied rows at the boundary |
| 4 | **`$lookup` with `preserveNullAndEmptyArrays: True` for headcount** | Employees with no logs still count toward department headcount |
| 5 | **Push headcount fully into MongoDB for 100× scale** | Python loop over employees per trend-day would be a bottleneck at 10M records |

---

## 15. Known Limitations & Future Work

- **`$densify` requires MongoDB 5.1+** and **`$setWindowFields $rank` requires MongoDB 5.0+**. The assignment specifies MongoDB 6.0+, so both are safe.
- **Sync PyMongo** is used throughout. For a production service under heavy concurrent load, migrating to **Motor** (async driver) would allow FastAPI to handle I/O concurrently.
- **No authentication / authorization** — the assignment does not require it, but any real deployment would add JWT or API-key middleware.
- **Partial submissions**: None — all endpoints, business rules, and tests are fully implemented.
