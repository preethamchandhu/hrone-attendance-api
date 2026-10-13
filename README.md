# Employee Attendance & Analytics API

FastAPI and MongoDB backend service for employee attendance tracking, regularization audit trails, and aggregation-powered analytics.

Built to conform strictly to **Contract v2.0** (`openapi.yaml` and `DATA_MODEL.md`).

---

## Getting Started

### 1. Requirements
- Python 3.11+
- MongoDB 6.0+ (Local Community Server or free MongoDB Atlas cluster)

### 2. Installation
```bash
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 3. Configuration
Set the following environment variables (or place them in a local `.env` file during local testing):
```env
MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance_db
```
*Note: Real environment variables take priority over local `.env`.*

### 4. Running the Application
```bash
uvicorn app.main:app --port 8000
```
The application will start, connect to MongoDB, and automatically create all required indexes idempotently at startup. The readiness probe is available at `GET /health`.

---

## Architectural & Design Highlights

1. **Race-Safe Concurrency:**
   - Unique compound indexes on `employees(emp_code)` and `attendance_logs(emp_code, date)` guarantee that concurrent creation or punch-in attempts yield exactly one `201 Created` and the remainder receive `409 Conflict`.
   - Punch-out and regularization use atomic and optimistic conditional updates to eliminate race conditions and avoid silent history loss.

2. **Accurate Date & Time Handling (R1 - R5):**
   - All external timestamps are validated as integer epoch milliseconds (`EpochMillis`) and converted to UTC BSON datetimes at boundaries.
   - Truncates instants to whole seconds before computation.
   - Accurately tracks overnight shifts across midnight (e.g., 22:00 to 06:00).
   - Strict adherence to business rules: 10-minute grace period (R2), 30-minute minimum overtime threshold (R3), and Decimal round-half-up for work hours (R4) and attendance rates (R8).

3. **High-Performance MongoDB Aggregations:**
   - **Monthly Employee Summary:** Aggregates working days, presence, half days, and leave.
   - **Department Summary:** Cross-collection aggregation starting from `employees` ensuring accurate headcount including zero-log employees (R9).
   - **Late Leaderboard:** Native window function standard competition ranking (`$setWindowFields` with `$rank: {}`) correctly managing ties (1, 2, 2, 4) with post-ranking limits.
   - **Department Trend:** Database-level calendar gap-filling via `$densify` combined with a rolling 7-day moving average (`$setWindowFields`).

4. **Explain Endpoint:**
   - `GET /admin/explain/{endpoint}` runs `explain` with `executionStats` on the same query or pipeline the endpoint runs (both call the same builder function) and returns it as JSON-safe output.

---

## Tests
`test_logic.py` checks the business rules and builds every pipeline without needing MongoDB:
```bash
pip install pytest httpx
python -m pytest -q test_logic.py
```
The endpoints themselves should be exercised against a real MongoDB (Atlas or local) with data that includes ties, weekend records, a mid-month joiner, an overnight shift and an employee with no logs.

---

## Submission Deliverables
- [`app/main.py`](app/main.py): Complete API and business logic.
- [`requirements.txt`](requirements.txt): Required application dependencies.
- [`REVIEW.md`](REVIEW.md): Code review and catalog of starter bugs and resolutions.
- [`DECISIONS.md`](DECISIONS.md): Architectural decisions and index strategy.
- [`README.md`](README.md): Setup and documentation.
