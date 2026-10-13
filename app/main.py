"""
Employee Attendance & Analytics API - Production Implementation
Contract: OpenAPI 3.0.3 v2.0 (HROne Engineering Assignment)

All application code is contained in this single file as required by the specification.
Starts with: uvicorn app.main:app --port 8000
"""

import json
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Literal, Optional, Union

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, StrictInt, field_validator, model_validator
from bson import json_util
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError

# --------------------------------------------------------------------------- #
# Environment & Constants
# --------------------------------------------------------------------------- #
load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client: MongoClient = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))

# --------------------------------------------------------------------------- #
# Index Initialization (Lifecycle)
# --------------------------------------------------------------------------- #
def init_indexes():
    """
    Creates all required indexes idempotently at application startup.
    Ensures unique constraints against race conditions and guarantees IXSCAN
    for all query, aggregation, and explain endpoints.
    """
    try:
        # employees indexes
        db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="idx_employees_emp_code_unique")
        db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="idx_employees_dept_joined")
        db.employees.create_index([("joined_on", ASCENDING)], name="idx_employees_joined_on")

        # attendance_logs indexes
        db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="idx_attendance_emp_date_unique")
        db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="idx_attendance_date_desc_emp_asc")
        db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="idx_attendance_emp_punch_in_desc")
        db.attendance_logs.create_index([("date", ASCENDING), ("late_minutes", ASCENDING), ("emp_code", ASCENDING)], name="idx_attendance_date_late_emp")
        db.attendance_logs.create_index([("date", ASCENDING)], name="idx_attendance_date_asc")
    except Exception:
        # Do not crash the app (GET /health will report 503), but never hide the failure
        logging.getLogger("uvicorn.error").exception("Index initialization failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_indexes()
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    description="HROne Employee Attendance & Analytics API (Contract v2.0)",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# Helpers & Business Rules (R1 - R8)
# --------------------------------------------------------------------------- #
def round_half_up(val: Optional[Union[float, Decimal]], decimals: int = 2) -> Optional[float]:
    """R8: Decimal round half-up."""
    if val is None:
        return None
    d = Decimal(str(val))
    target = Decimal("1") if decimals == 0 else Decimal("0." + "0" * (decimals - 1) + "1")
    return float(d.quantize(target, rounding=ROUND_HALF_UP))


def truncate_to_seconds(dt: datetime) -> datetime:
    """R1: Instants are truncated to whole seconds before any calculation."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.replace(microsecond=0)


def ms_to_datetime_utc(ms: Optional[int]) -> Optional[datetime]:
    if ms is None:
        return None
    # Truncate to whole seconds
    truncated_ms = (ms // 1000) * 1000
    return datetime.fromtimestamp(truncated_ms / 1000.0, tz=timezone.utc)


def datetime_to_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return int(dt.timestamp()) * 1000


def is_overnight_shift(shift_start: str, shift_end: str) -> bool:
    """R1: Overnight shift when shift_end <= shift_start."""
    sh_h, sh_m = map(int, shift_start.split(":"))
    eh_h, eh_m = map(int, shift_end.split(":"))
    return (eh_h, eh_m) <= (sh_h, sh_m)


def compute_attendance_date(punch_in_utc: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1: A record's date is the IST calendar date of its punch_in.
    Overnight shifts: punch-in earlier than shift_end belongs to the shift starting previous day.
    """
    punch_in_ist = punch_in_utc.astimezone(IST)
    if is_overnight_shift(shift_start, shift_end):
        eh_h, eh_m = map(int, shift_end.split(":"))
        if punch_in_ist.time() < time(eh_h, eh_m):
            return (punch_in_ist.date() - timedelta(days=1)).isoformat()
    return punch_in_ist.date().isoformat()


def compute_late_minutes(punch_in_utc: datetime, shift_start: str, attendance_date: str) -> int:
    """
    R2: Grace period 10 minutes (600s).
    Late ONLY if punch-in is strictly more than 10:00 after shift_start.
    Then late_minutes = floor((punch_in - shift_start) in minutes).
    """
    sh_h, sh_m = map(int, shift_start.split(":"))
    att_date = date.fromisoformat(attendance_date)
    shift_start_ist = datetime.combine(att_date, time(sh_h, sh_m), tzinfo=IST)
    punch_in_ist = punch_in_utc.astimezone(IST)

    delta_sec = (punch_in_ist - shift_start_ist).total_seconds()
    if delta_sec > 600:
        return int(delta_sec // 60)
    return 0


def compute_overtime(punch_out_utc: datetime, shift_start: str, shift_end: str, attendance_date: str) -> int:
    """
    R3: Overtime = whole minutes (floor) between shift_end and punch_out,
    counted only when >= 30; otherwise 0.
    """
    eh_h, eh_m = map(int, shift_end.split(":"))
    att_date = date.fromisoformat(attendance_date)
    if is_overnight_shift(shift_start, shift_end):
        shift_end_ist = datetime.combine(att_date + timedelta(days=1), time(eh_h, eh_m), tzinfo=IST)
    else:
        shift_end_ist = datetime.combine(att_date, time(eh_h, eh_m), tzinfo=IST)

    punch_out_ist = punch_out_utc.astimezone(IST)
    delta_sec = (punch_out_ist - shift_end_ist).total_seconds()
    delta_min = int(delta_sec // 60)
    return delta_min if delta_min >= 30 else 0


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> Optional[float]:
    """R4: (punch_out - punch_in) in seconds / 3600, rounded 2 decimals half-up."""
    if punch_out_utc is None or punch_in_utc is None:
        return None
    delta_sec = Decimal(str((punch_out_utc - punch_in_utc).total_seconds()))
    hours = delta_sec / Decimal("3600")
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_half_day(work_hours: Optional[float]) -> bool:
    """R5: half_day is true when rounded work_hours < 4.50."""
    if work_hours is None:
        return False
    return work_hours < 4.50


def get_month_dates(month_str: str) -> tuple[date, date]:
    """Returns start and end calendar dates for a month 'YYYY-MM'."""
    y, m = map(int, month_str.split("-"))
    start_d = date(y, m, 1)
    if m == 12:
        end_d = date(y + 1, 1, 1) - timedelta(days=1)
    else:
        end_d = date(y, m + 1, 1) - timedelta(days=1)
    return start_d, end_d


def count_working_days(joined_on_str: str, month_str: str) -> int:
    """R7: Mon-Fri working days, counted from joined_on for mid-month joiners."""
    month_start, month_end = get_month_dates(month_str)
    joined_d = date.fromisoformat(joined_on_str)
    if joined_d > month_end:
        return 0
    cur = max(month_start, joined_d)
    count = 0
    while cur <= month_end:
        if cur.weekday() < 5:  # Mon-Fri
            count += 1
        cur += timedelta(days=1)
    return count


def serialize_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Transforms a MongoDB attendance record document into OpenAPI response format."""
    out = dict(doc)
    out.pop("_id", None)
    out.pop("id", None)

    out["punch_in"] = datetime_to_ms(out.get("punch_in"))
    out["punch_out"] = datetime_to_ms(out.get("punch_out"))
    out["late_minutes"] = out.get("late_minutes", 0)
    out["overtime_minutes"] = out.get("overtime_minutes", 0)
    out["half_day"] = out.get("half_day", False)

    history = []
    for h in out.get("history", []):
        h_entry = {
            "at": datetime_to_ms(h.get("at")),
            "by": h.get("by"),
            "reason": h.get("reason"),
            "changes": {},
        }
        for field_name, ch in h.get("changes", {}).items():
            f_from = ch.get("from")
            f_to = ch.get("to")
            if field_name in ("punch_in", "punch_out"):
                if isinstance(f_from, datetime):
                    f_from = datetime_to_ms(f_from)
                if isinstance(f_to, datetime):
                    f_to = datetime_to_ms(f_to)
            h_entry["changes"][field_name] = {"from": f_from, "to": f_to}
        history.append(h_entry)
    out["history"] = history
    return out


# --------------------------------------------------------------------------- #
# Pydantic Schemas
# --------------------------------------------------------------------------- #
TIME_REGEX = r"^([01]\d|2[0-3]):[0-5]\d$"
DATE_REGEX = r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"
MONTH_REGEX = r"^\d{4}-(0[1-9]|1[0-2])$"
EMP_CODE_REGEX = r"^EMP\d{4,6}$"
EMAIL_REGEX = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"


StatusLiteral = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]


class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., pattern=EMP_CODE_REGEX)
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=EMAIL_REGEX)
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=TIME_REGEX)
    shift_end: str = Field("18:30", pattern=TIME_REGEX)
    joined_on: str = Field(..., pattern=DATE_REGEX)

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, v: str) -> str:
        try:
            date.fromisoformat(v)  # rejects e.g. 2026-02-31, which the regex alone accepts
        except ValueError:
            raise ValueError("joined_on must be a real calendar date")
        return v

    @model_validator(mode="after")
    def validate_shifts(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = Field(None, ge=100000000000, le=4102444800000)
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = Field(None, ge=100000000000, le=4102444800000)


class RegularizeRequest(BaseModel):
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[StrictInt] = Field(None, ge=100000000000, le=4102444800000)
    punch_out: Optional[StrictInt] = Field(None, ge=100000000000, le=4102444800000)
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health", tags=["system"])
def health():
    """Returns 200 only if MongoDB answers a ping, otherwise 503."""
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(status_code=503, detail="Database unavailable")


@app.post("/employees", status_code=status.HTTP_201_CREATED, tags=["employees"])
def create_employee(body: EmployeeCreate):
    """
    Creates an employee. Unique emp_code is enforced via database unique index.
    Server sets created_at as epoch milliseconds.
    """
    created_dt = truncate_to_seconds(datetime.now(timezone.utc))
    doc = body.model_dump()
    doc["created_at"] = created_dt

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")

    doc.pop("_id", None)
    doc["created_at"] = datetime_to_ms(created_dt)
    return doc


@app.get("/employees", tags=["employees"])
def list_employees(
    department: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """List employees sorted by emp_code ascending with accurate filtered total."""
    q = {}
    if department is not None:
        q["department"] = department

    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip(skip).limit(page_size)

    items = []
    for emp in cursor:
        emp["created_at"] = datetime_to_ms(emp.get("created_at"))
        items.append(emp)

    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=status.HTTP_201_CREATED, tags=["attendance"])
def punch_in(body: PunchInRequest):
    """
    Records a punch-in. Computes attendance date (R1) and late minutes (R2).
    Race-safe against concurrent punch-ins via compound unique index (emp_code, date).
    """
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        punch_in_utc = ms_to_datetime_utc(body.punched_at)
    else:
        punch_in_utc = truncate_to_seconds(datetime.now(timezone.utc))

    att_date = compute_attendance_date(punch_in_utc, emp["shift_start"], emp["shift_end"])
    late_min = compute_late_minutes(punch_in_utc, emp["shift_start"], att_date)

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": punch_in_utc,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_min,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="already punched in for this date")

    return serialize_doc(doc)


@app.post("/attendance/punch-out", status_code=status.HTTP_200_OK, tags=["attendance"])
def punch_out(body: PunchOutRequest):
    """
    Closes the most recent record whose punch_in <= punched_at.
    Computes work hours (R4), overtime (R3), and half day (R5).
    Atomic update ensures concurrent punch-outs yield exactly one 200 and one 409.
    """
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        punch_out_utc = ms_to_datetime_utc(body.punched_at)
    else:
        punch_out_utc = truncate_to_seconds(datetime.now(timezone.utc))

    # Find the most recent record with punch_in <= punched_at
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punch_out_utc}},
        sort=[("punch_in", DESCENDING)],
    )

    if not record:
        raise HTTPException(status_code=404, detail="No punch-in found")

    punch_in_utc = record.get("punch_in")
    if punch_out_utc <= punch_in_utc or (punch_out_utc - punch_in_utc).total_seconds() > 86400:
        raise HTTPException(
            status_code=422,
            detail="punched_at must be strictly greater than punch_in and within 24 hours",
        )

    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="record is already punched out")

    # Compute derived metrics
    work_hrs = compute_work_hours(punch_in_utc, punch_out_utc)
    half_d = compute_half_day(work_hrs)
    overtime_min = compute_overtime(punch_out_utc, emp["shift_start"], emp["shift_end"], record["date"])

    # Atomic update on punch_out is None
    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": punch_out_utc,
                "work_hours": work_hrs,
                "overtime_minutes": overtime_min,
                "half_day": half_d,
            }
        },
        return_document=True,
    )

    if not updated:
        raise HTTPException(status_code=409, detail="record is already punched out")

    return serialize_doc(updated)


@app.get("/attendance", tags=["attendance"])
def list_attendance(
    emp_code: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None, pattern=DATE_REGEX),
    date_to: Optional[str] = Query(None, pattern=DATE_REGEX),
    status: Optional[StatusLiteral] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """
    List attendance records sorted by date descending, then emp_code ascending.
    Evaluated directly in MongoDB with indexes for high throughput on 100k+ records.
    """
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from cannot be greater than date_to")

    q = build_attendance_filter(emp_code, date_from, date_to, status)

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.attendance_logs.find(q).sort(ATTENDANCE_SORT).skip(skip).limit(page_size)

    items = [serialize_doc(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}", tags=["attendance"])
def regularize_attendance(
    emp_code: str = Path(...),
    date_param: str = Path(..., alias="date", pattern=DATE_REGEX),
    body: RegularizeRequest = ...,
):
    """
    Regularizes one day's record, recomputes derived metrics, and appends an audit trail entry.
    Optimistic concurrency prevents race conditions.
    """
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date_param})
    if not record:
        raise HTTPException(status_code=404, detail="Attendance record not found for that date")

    # Current values
    cur_status = record.get("status")
    cur_punch_in = record.get("punch_in")
    cur_punch_out = record.get("punch_out")

    # Target values
    target_status = body.status if body.status is not None else cur_status

    if target_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(status_code=422, detail="punch_in and punch_out cannot be supplied when setting ABSENT or LEAVE")
        target_punch_in = None
        target_punch_out = None
    else:
        target_punch_in = ms_to_datetime_utc(body.punch_in) if body.punch_in is not None else cur_punch_in
        target_punch_out = ms_to_datetime_utc(body.punch_out) if body.punch_out is not None else cur_punch_out

        if target_punch_in is None:
            raise HTTPException(status_code=422, detail="Presence status requires a punch_in")

        # Validate punch_in stays on record attendance date
        r1_date = compute_attendance_date(target_punch_in, emp["shift_start"], emp["shift_end"])
        if r1_date != date_param:
            raise HTTPException(status_code=422, detail="punch_in must stay on the record's attendance date")

        if target_punch_out is not None:
            if target_punch_out <= target_punch_in or (target_punch_out - target_punch_in).total_seconds() > 86400:
                raise HTTPException(status_code=422, detail="punch_out must be after punch_in and within 24 hours")

    # Recompute derived fields
    if target_status in ("ABSENT", "LEAVE"):
        target_work_hours = None
        target_late_minutes = 0
        target_overtime_minutes = 0
        target_half_day = False
    else:
        target_late_minutes = compute_late_minutes(target_punch_in, emp["shift_start"], date_param)
        if target_punch_out is not None:
            target_work_hours = compute_work_hours(target_punch_in, target_punch_out)
            target_half_day = compute_half_day(target_work_hours)
            target_overtime_minutes = compute_overtime(target_punch_out, emp["shift_start"], emp["shift_end"], date_param)
        else:
            target_work_hours = None
            target_half_day = False
            target_overtime_minutes = 0

    # Build changes dictionary
    changes_db = {}
    diff_checks = [
        ("status", cur_status, target_status),
        ("punch_in", cur_punch_in, target_punch_in),
        ("punch_out", cur_punch_out, target_punch_out),
        ("work_hours", record.get("work_hours"), target_work_hours),
        ("late_minutes", record.get("late_minutes", 0), target_late_minutes),
        ("overtime_minutes", record.get("overtime_minutes", 0), target_overtime_minutes),
        ("half_day", record.get("half_day", False), target_half_day),
    ]

    for field_name, old_val, new_val in diff_checks:
        if old_val != new_val:
            changes_db[field_name] = {"from": old_val, "to": new_val}

    if not changes_db:
        raise HTTPException(status_code=422, detail="Request changes nothing")

    history_entry = {
        "at": truncate_to_seconds(datetime.now(timezone.utc)),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes_db,
    }

    # Optimistic lock on the history array. Legacy records have NO `history` field at all,
    # and {"history": {"$size": 0}} does not match a missing field, so handle that case.
    if "history" in record:
        history_guard: Dict[str, Any] = {"$size": len(record["history"])}
    else:
        history_guard = {"$exists": False}
    update_doc = {
        "$set": {
            "status": target_status,
            "punch_in": target_punch_in,
            "punch_out": target_punch_out,
            "work_hours": target_work_hours,
            "late_minutes": target_late_minutes,
            "overtime_minutes": target_overtime_minutes,
            "half_day": target_half_day,
        },
        "$push": {"history": history_entry},
    }

    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "history": history_guard},
        update_doc,
        return_document=True,
    )

    if not updated:
        raise HTTPException(status_code=409, detail="Concurrent regularize modification detected")

    return serialize_doc(updated)


# --------------------------------------------------------------------------- #
# Query / pipeline builders
# Each endpoint AND its /admin/explain counterpart call the same builder, so the
# explained plan is exactly what the endpoint runs.
# --------------------------------------------------------------------------- #
PRESENT_STATUSES = ["PRESENT", "WFH", "ON_DUTY"]
WEEKDAY_NUMBERS = [2, 3, 4, 5, 6]  # Mongo $dayOfWeek: 1=Sunday ... 7=Saturday


def build_attendance_filter(
    emp_code: Optional[str],
    date_from: Optional[str],
    date_to: Optional[str],
    status_filter: Optional[str],
) -> Dict[str, Any]:
    q: Dict[str, Any] = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status_filter:
        q["status"] = status_filter
    return q


ATTENDANCE_SORT = [("date", DESCENDING), ("emp_code", ASCENDING)]


def pipeline_employee_monthly(emp_code: str, month: str) -> List[Dict[str, Any]]:
    start_d, end_d = get_month_dates(month)
    return [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": start_d.isoformat(), "$lte": end_d.isoformat()},
            }
        },
        {
            "$project": {
                "status": 1,
                "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
                "half_day": {"$ifNull": ["$half_day", False]},
                "day_of_week": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$day_of_week", WEEKDAY_NUMBERS]},
                                    {"$in": ["$status", PRESENT_STATUSES]},
                                ]
                            },
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"},
            }
        },
    ]


def pipeline_department_summary(month: str, department: Optional[str]) -> List[Dict[str, Any]]:
    """
    Starts from `employees` so people with zero logs still count (R9).
    Logs are joined per employee and reduced to a handful of numbers per employee
    BEFORE grouping, so no document ever holds a department's whole month of logs
    (avoids the 16 MB BSON limit at scale).
    """
    start_d, end_d = get_month_dates(month)
    match_emp: Dict[str, Any] = {"joined_on": {"$lte": end_d.isoformat()}}
    if department:
        match_emp["department"] = department

    is_present = {"$in": ["$$this.status", PRESENT_STATUSES]}
    is_weekday = {
        "$in": [
            {"$dayOfWeek": {"$dateFromString": {"dateString": "$$this.date"}}},
            WEEKDAY_NUMBERS,
        ]
    }
    late_of = {"$ifNull": ["$$this.late_minutes", 0]}
    has_hours = {"$and": [is_present, {"$isNumber": "$$this.work_hours"}]}

    return [
        {"$match": match_emp},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$emp"]},
                                    {"$gte": ["$date", start_d.isoformat()]},
                                    {"$lte": ["$date", end_d.isoformat()]},
                                ]
                            }
                        }
                    },
                    {"$project": {"_id": 0, "date": 1, "status": 1, "work_hours": 1,
                                  "late_minutes": 1, "half_day": 1}},
                ],
                "as": "logs",
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": 1,
                "present_days": {
                    "$reduce": {
                        "input": "$logs",
                        "initialValue": 0.0,
                        "in": {
                            "$add": [
                                "$$value",
                                {
                                    "$cond": [
                                        {"$and": [is_present, is_weekday]},
                                        {"$cond": [{"$ifNull": ["$$this.half_day", False]}, 0.5, 1.0]},
                                        0.0,
                                    ]
                                },
                            ]
                        },
                    }
                },
                "wh_sum": {
                    "$reduce": {
                        "input": "$logs",
                        "initialValue": 0.0,
                        "in": {"$add": ["$$value", {"$cond": [has_hours, "$$this.work_hours", 0.0]}]},
                    }
                },
                "wh_count": {
                    "$reduce": {
                        "input": "$logs",
                        "initialValue": 0,
                        "in": {"$add": ["$$value", {"$cond": [has_hours, 1, 0]}]},
                    }
                },
                "late_count": {
                    "$reduce": {
                        "input": "$logs",
                        "initialValue": 0,
                        "in": {"$add": ["$$value", {"$cond": [{"$gt": [late_of, 0]}, 1, 0]}]},
                    }
                },
                "total_late_minutes": {
                    "$reduce": {
                        "input": "$logs",
                        "initialValue": 0,
                        "in": {"$add": ["$$value", late_of]},
                    }
                },
                "leave_count": {
                    "$reduce": {
                        "input": "$logs",
                        "initialValue": 0,
                        "in": {"$add": ["$$value", {"$cond": [{"$eq": ["$$this.status", "LEAVE"]}, 1, 0]}]},
                    }
                },
                "on_duty_count": {
                    "$reduce": {
                        "input": "$logs",
                        "initialValue": 0,
                        "in": {"$add": ["$$value", {"$cond": [{"$eq": ["$$this.status", "ON_DUTY"]}, 1, 0]}]},
                    }
                },
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": "$present_days"},
                "wh_sum": {"$sum": "$wh_sum"},
                "wh_count": {"$sum": "$wh_count"},
                "late_count": {"$sum": "$late_count"},
                "total_late_minutes": {"$sum": "$total_late_minutes"},
                "leave_count": {"$sum": "$leave_count"},
                "on_duty_count": {"$sum": "$on_duty_count"},
            }
        },
        {"$sort": {"_id": ASCENDING}},
    ]


def pipeline_late_leaderboard(month: str, limit: int, department: Optional[str]) -> List[Dict[str, Any]]:
    start_d, end_d = get_month_dates(month)
    pipeline: List[Dict[str, Any]] = [
        {
            "$match": {
                "date": {"$gte": start_d.isoformat(), "$lte": end_d.isoformat()},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},  # logs of unknown employees are dropped
    ]
    if department:
        # rank inside the department only: filter BEFORE ranking
        pipeline.append({"$match": {"emp.department": department}})
    pipeline.extend(
        [
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
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": DESCENDING},
                    "output": {"rank": {"$rank": {}}},
                }
            },
            {"$match": {"rank": {"$lte": limit}}},  # limit applies AFTER ranking
            {"$sort": {"total_late_minutes": DESCENDING, "emp_code": ASCENDING}},
        ]
    )
    return pipeline


def pipeline_department_trend(department: str, from_date: str, to_date: str) -> List[Dict[str, Any]]:
    d_from = date.fromisoformat(from_date)
    d_to = date.fromisoformat(to_date)
    dt_from_bound = datetime.combine(d_from, time(0, 0), tzinfo=timezone.utc)
    dt_to_bound = datetime.combine(d_to + timedelta(days=1), time(0, 0), tzinfo=timezone.utc)

    return [
        {"$match": {"date": {"$gte": from_date, "$lte": to_date}}},
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
            "$project": {
                "date": 1,
                "status": 1,
                "half_day": {"$ifNull": ["$half_day", False]},
                "late_minutes": {"$ifNull": ["$late_minutes", 0]},
            }
        },
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", PRESENT_STATUSES]},
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
            }
        },
        # Anchor row so $densify has something to fill from even when the range has no logs
        {
            "$unionWith": {
                "coll": "employees",
                "pipeline": [
                    {"$match": {"department": department}},
                    {"$limit": 1},
                    {
                        "$project": {
                            "_id": {"$literal": from_date},
                            "present_count": {"$literal": 0.0},
                            "late_count": {"$literal": 0},
                        }
                    },
                ],
            }
        },
        {
            "$group": {
                "_id": "$_id",
                "present_count": {"$sum": "$present_count"},
                "late_count": {"$sum": "$late_count"},
            }
        },
        {
            "$project": {
                "date_obj": {"$dateFromString": {"dateString": "$_id", "timezone": "UTC"}},
                "present_count": 1,
                "late_count": 1,
            }
        },
        {
            "$densify": {
                "field": "date_obj",
                "range": {"step": 1, "unit": "day", "bounds": [dt_from_bound, dt_to_bound]},
            }
        },
        {
            "$project": {
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date_obj", "timezone": "UTC"}},
                "date_obj": 1,
                "present_count": {"$ifNull": ["$present_count", 0.0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"day_str": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$day_str"]},
                                ]
                            }
                        }
                    },
                    {"$count": "hc"},
                ],
                "as": "hc_docs",
            }
        },
        {
            "$project": {
                "date": 1,
                "present_count": 1,
                "late_count": 1,
                "headcount": {"$ifNull": [{"$arrayElemAt": ["$hc_docs.hc", 0]}, 0]},
                "is_working_day": {
                    "$in": [{"$dayOfWeek": {"date": "$date_obj", "timezone": "UTC"}}, WEEKDAY_NUMBERS]
                },
            }
        },
        {
            "$project": {
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                # R8: 4 decimals HALF-UP. Mongo's $round is half-to-even, so round by hand:
                # floor(present * 10000 / headcount + 0.5) / 10000
                "attendance_rate": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        {
                            "$divide": [
                                {
                                    "$floor": {
                                        "$add": [
                                            {"$divide": [{"$multiply": ["$present_count", 10000]}, "$headcount"]},
                                            0.5,
                                        ]
                                    }
                                },
                                10000,
                            ]
                        },
                        None,
                    ]
                },
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": ASCENDING},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, "current"]},
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1,  # rounded half-up in Python (see department_trend)
            }
        },
        {"$sort": {"date": ASCENDING}},
    ]


# --------------------------------------------------------------------------- #
# Analytics endpoints
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly", tags=["analytics"])
def employee_monthly(
    emp_code: str = Path(...),
    month: str = Query(..., pattern=MONTH_REGEX),
):
    """Monthly attendance summary for one employee via a MongoDB aggregation."""
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    working_days = count_working_days(emp["joined_on"], month)
    res = list(db.attendance_logs.aggregate(pipeline_employee_monthly(emp_code, month)))
    agg = res[0] if res else {}

    present_days = float(agg.get("present_days", 0.0))
    att_pct = round_half_up((present_days / working_days) * 100.0, 2) if working_days > 0 else None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": round_half_up(present_days, 2),
        "leave_days": int(agg.get("leave_days", 0)),
        "late_count": int(agg.get("late_count", 0)),
        "total_late_minutes": int(agg.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(agg.get("total_overtime_minutes", 0)),
        "attendance_pct": att_pct,
    }


@app.get("/analytics/departments/summary", tags=["analytics"])
def department_summary(
    month: str = Query(..., pattern=MONTH_REGEX),
    department: Optional[str] = Query(None),
):
    """Department summary for a month (cross-collection aggregation, R9 headcount)."""
    items = []
    for d in db.employees.aggregate(pipeline_department_summary(month, department)):
        wh_count = d.get("wh_count", 0)
        # average over RECORDS (sum of hours / number of records), not average of averages
        avg_wh = round_half_up(d["wh_sum"] / wh_count, 2) if wh_count else None
        items.append(
            {
                "department": d["_id"],
                "headcount": d["headcount"],
                "present_days": round_half_up(d.get("present_days", 0.0), 2),
                "avg_work_hours": avg_wh,
                "late_count": d.get("late_count", 0),
                "total_late_minutes": d.get("total_late_minutes", 0),
                "leave_count": d.get("leave_count", 0),
                "on_duty_count": d.get("on_duty_count", 0),
            }
        )
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late", tags=["analytics"])
def late_leaderboard(
    month: str = Query(..., pattern=MONTH_REGEX),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = Query(None),
):
    """Late-comers with standard competition ranking (1, 2, 2, 4); limit applies after ranking."""
    items = list(db.attendance_logs.aggregate(pipeline_late_leaderboard(month, limit, department)))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend", tags=["analytics"])
def department_trend(
    department: str = Path(...),
    from_date: str = Query(..., alias="from", pattern=DATE_REGEX),
    to_date: str = Query(..., alias="to", pattern=DATE_REGEX),
):
    """One row per calendar day (gap-filled by $densify) with a 7-day moving average."""
    try:
        d_from = date.fromisoformat(from_date)
        d_to = date.fromisoformat(to_date)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid calendar date")
    if d_to < d_from:
        raise HTTPException(status_code=422, detail="'to' date cannot be earlier than 'from' date")
    if (d_to - d_from).days + 1 > 92:
        raise HTTPException(status_code=422, detail="Date range cannot exceed 92 days")

    if db.employees.count_documents({"department": department}, limit=1) == 0:
        raise HTTPException(status_code=404, detail="Department not found")

    items = list(db.attendance_logs.aggregate(pipeline_department_trend(department, from_date, to_date)))
    for row in items:
        if row.get("moving_avg_7d") is not None:
            row["moving_avg_7d"] = round_half_up(row["moving_avg_7d"], 4)
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Explain
# --------------------------------------------------------------------------- #
def _json_safe(doc: Any) -> Any:
    """Explain output can hold BSON types (Timestamp, Binary, ObjectId) that FastAPI cannot encode."""
    return json.loads(json_util.dumps(doc, json_options=json_util.RELAXED_JSON_OPTIONS))


def _explain_command(cmd: Dict[str, Any]) -> Any:
    return _json_safe(db.command("explain", cmd, verbosity="executionStats"))


@app.get("/admin/explain/{endpoint}", tags=["admin"])
def explain_endpoint(
    endpoint: Literal[
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    ] = Path(...),
    emp_code: Optional[str] = Query(None),
    month: Optional[str] = Query(None, pattern=MONTH_REGEX),
    department: Optional[str] = Query(None),
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None, pattern=DATE_REGEX),
    date_to: Optional[str] = Query(None, pattern=DATE_REGEX),
    status_filter: Optional[StatusLiteral] = Query(None, alias="status"),
    from_date: Optional[str] = Query(None, alias="from", pattern=DATE_REGEX),
    to_date: Optional[str] = Query(None, alias="to", pattern=DATE_REGEX),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """executionStats explain of the endpoint's real query/pipeline (same builders as the endpoint)."""

    def agg(collection: str, pipeline: List[Dict[str, Any]]):
        explain_res = _explain_command({"aggregate": collection, "pipeline": pipeline, "cursor": {}})
        return {"endpoint": endpoint, "collection": collection, "explain": explain_res}

    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(status_code=422, detail="date_from cannot be greater than date_to")
        find_cmd = {
            "find": "attendance_logs",
            "filter": build_attendance_filter(emp_code, date_from, date_to, status_filter),
            "sort": dict(ATTENDANCE_SORT),
            "skip": (page - 1) * page_size,
            "limit": page_size,
        }
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": _explain_command(find_cmd)}

    if endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(status_code=422, detail="emp_code and month are required")
        return agg("attendance_logs", pipeline_employee_monthly(emp_code, month))

    if endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        return agg("employees", pipeline_department_summary(month, department))

    if endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        return agg("attendance_logs", pipeline_late_leaderboard(month, limit, department))

    # department_trend
    if not department or not from_date or not to_date:
        raise HTTPException(status_code=422, detail="department, from and to are required")
    if to_date < from_date:
        raise HTTPException(status_code=422, detail="'to' cannot be earlier than 'from'")
    return agg("attendance_logs", pipeline_department_trend(department, from_date, to_date))
