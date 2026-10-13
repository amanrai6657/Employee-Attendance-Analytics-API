"""
Employee Attendance & Analytics API

Run: uvicorn app.main:app --port 8000
Env: MONGO_URI, MONGO_DB (a local .env is loaded for convenience)
"""
import os
import re
from calendar import monthrange
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, OperationFailure, PyMongoError

load_dotenv(override=False)

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc

client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=3000, connectTimeoutMS=3000, tz_aware=True, tzinfo=UTC)
db = client[MONGO_DB]
employees = db.employees
attendance = db.attendance_logs

PRESENCE_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}
ALL_STATUSES = PRESENCE_STATUSES | {"ABSENT", "LEAVE"}
EMP_RE = re.compile(r"^EMP\d{4,6}$")
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
MIN_EPOCH_MS = 100000000000
MAX_EPOCH_MS = 4102444800000


def create_indexes() -> None:
    employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_employees_emp_code")
    employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING), ("emp_code", ASCENDING)], name="idx_employees_department_joined_emp")
    employees.create_index([("joined_on", ASCENDING), ("department", ASCENDING)], name="idx_employees_joined_department")
    attendance.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_attendance_emp_date")
    attendance.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="idx_attendance_date_desc_emp")
    attendance.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="idx_attendance_emp_punch_in_desc")
    attendance.create_index([("date", ASCENDING), ("status", ASCENDING), ("emp_code", ASCENDING)], name="idx_attendance_date_status_emp")
    attendance.create_index([("emp_code", ASCENDING), ("date", ASCENDING), ("late_minutes", ASCENDING)], name="idx_attendance_emp_date_late")


@asynccontextmanager
async def lifespan(_: FastAPI):
    create_indexes()
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, value: str) -> str:
        parse_date(value)
        return value

    @model_validator(mode="after")
    def validate_shift(self) -> "EmployeeIn":
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[StrictInt] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def validate_punched_at(cls, value: Optional[int]) -> Optional[int]:
        return validate_epoch_ms(value)


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[StrictInt] = None

    @field_validator("punched_at")
    @classmethod
    def validate_punched_at(cls, value: Optional[int]) -> Optional[int]:
        return validate_epoch_ms(value)


class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[StrictInt] = None
    punch_out: Optional[StrictInt] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("punch_in", "punch_out")
    @classmethod
    def validate_instants(cls, value: Optional[int]) -> Optional[int]:
        return validate_epoch_ms(value)


def validate_epoch_ms(value: Optional[int]) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("timestamp must be an integer epoch millisecond value")
    if value < MIN_EPOCH_MS or value > MAX_EPOCH_MS:
        raise ValueError("timestamp out of range or seconds-looking")
    return value


def parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValueError("date must be YYYY-MM-DD")


def parse_month(value: str) -> tuple[date, date]:
    if not MONTH_RE.match(value or ""):
        raise HTTPException(422, "month must be YYYY-MM")
    year, month = map(int, value.split("-"))
    last = monthrange(year, month)[1]
    return date(year, month, 1), date(year, month, last)


def parse_query_date(value: Optional[str], name: str) -> Optional[date]:
    if value is None:
        return None
    try:
        return parse_date(value)
    except ValueError:
        raise HTTPException(422, f"{name} must be YYYY-MM-DD")


def parse_shift(value: str) -> time:
    if not TIME_RE.match(value or ""):
        raise HTTPException(422, "shift time must be HH:MM")
    h, m = map(int, value.split(":"))
    return time(h, m)


def is_overnight(emp: dict[str, Any]) -> bool:
    return emp["shift_end"] <= emp["shift_start"]


def truncate_to_second(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).replace(microsecond=0)


def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def ms_to_dt(value: Optional[int]) -> datetime:
    if value is None:
        return now_utc()
    return datetime.fromtimestamp(value / 1000, UTC).replace(microsecond=0)


def dt_to_ms(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return int(value.astimezone(UTC).timestamp() * 1000)


def dt_for_history(value: Any) -> Any:
    if isinstance(value, datetime):
        return dt_to_ms(value)
    return value


def half_up(value: Decimal, places: str) -> float:
    return float(value.quantize(Decimal(places), rounding=ROUND_HALF_UP))


def round2(value: Any) -> float:
    return half_up(Decimal(str(value)), "0.01")


def round4(value: Any) -> float:
    return half_up(Decimal(str(value)), "0.0001")


def attendance_date_for_punch(punch_in: datetime, emp: dict[str, Any]) -> str:
    local = punch_in.astimezone(IST)
    d = local.date()
    if is_overnight(emp) and local.time() < parse_shift(emp["shift_end"]):
        d -= timedelta(days=1)
    return d.isoformat()


def local_shift_start(att_date: str, emp: dict[str, Any]) -> datetime:
    d = parse_date(att_date)
    t = parse_shift(emp["shift_start"])
    return datetime.combine(d, t, IST)


def local_shift_end(att_date: str, emp: dict[str, Any]) -> datetime:
    d = parse_date(att_date)
    if is_overnight(emp):
        d += timedelta(days=1)
    t = parse_shift(emp["shift_end"])
    return datetime.combine(d, t, IST)


def compute_late_minutes(punch_in: Optional[datetime], emp: dict[str, Any], att_date: str) -> int:
    if punch_in is None:
        return 0
    start = local_shift_start(att_date, emp).astimezone(UTC)
    seconds = (truncate_to_second(punch_in) - start).total_seconds()
    return int(seconds // 60) if seconds > 10 * 60 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = Decimal(str((truncate_to_second(punch_out) - truncate_to_second(punch_in)).total_seconds()))
    return half_up(seconds / Decimal("3600"), "0.01")


def compute_overtime_minutes(punch_out: Optional[datetime], emp: dict[str, Any], att_date: str) -> int:
    if punch_out is None:
        return 0
    end = local_shift_end(att_date, emp).astimezone(UTC)
    minutes = int((truncate_to_second(punch_out) - end).total_seconds() // 60)
    return minutes if minutes >= 30 else 0


def recompute_derived(doc: dict[str, Any], emp: dict[str, Any]) -> dict[str, Any]:
    status = doc["status"]
    if status in {"ABSENT", "LEAVE"}:
        return {"punch_in": None, "punch_out": None, "work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    punch_in = doc.get("punch_in")
    punch_out = doc.get("punch_out")
    late = compute_late_minutes(punch_in, emp, doc["date"])
    if punch_out is None:
        return {"work_hours": None, "late_minutes": late, "overtime_minutes": 0, "half_day": False}
    work = compute_work_hours(punch_in, punch_out)
    return {"work_hours": work, "late_minutes": late, "overtime_minutes": compute_overtime_minutes(punch_out, emp, doc["date"]), "half_day": work < 4.5}


def current_value(doc: dict[str, Any], key: str) -> Any:
    if key in {"late_minutes", "overtime_minutes"}:
        return int(doc.get(key, 0) or 0)
    if key == "half_day":
        return bool(doc.get(key, False))
    return doc.get(key)


def validate_punch_interval(punch_in: datetime, punch_out: datetime) -> None:
    if punch_out <= punch_in:
        raise HTTPException(422, "punch_out must be after punch_in")
    if punch_out - punch_in > timedelta(hours=24):
        raise HTTPException(422, "punch_out must be within 24 hours of punch_in")


def ensure_regularized_date(punch_in: datetime, emp: dict[str, Any], att_date: str) -> None:
    if attendance_date_for_punch(punch_in, emp) != att_date:
        raise HTTPException(422, "punch_in does not belong to the record attendance date")


def serialize_employee(doc: dict[str, Any]) -> dict[str, Any]:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc.get("shift_start", "09:30"),
        "shift_end": doc.get("shift_end", "18:30"),
        "joined_on": doc["joined_on"],
        "created_at": dt_to_ms(doc["created_at"]),
    }


def serialize_history_entry(entry: dict[str, Any]) -> dict[str, Any]:
    changes = {}
    for key, pair in (entry.get("changes") or {}).items():
        changes[key] = {"from": dt_for_history(pair.get("from")), "to": dt_for_history(pair.get("to"))}
    return {"at": dt_to_ms(entry["at"]), "by": entry["by"], "reason": entry["reason"], "changes": changes}


def serialize_attendance(doc: dict[str, Any]) -> dict[str, Any]:
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_ms(doc.get("punch_in")),
        "punch_out": dt_to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": int(doc.get("late_minutes", 0) or 0),
        "overtime_minutes": int(doc.get("overtime_minutes", 0) or 0),
        "half_day": bool(doc.get("half_day", False)),
        "history": [serialize_history_entry(h) for h in doc.get("history", [])],
    }


def working_days_between(start: date, end: date) -> int:
    if start > end:
        return 0
    days = 0
    cur = start
    while cur <= end:
        if cur.weekday() < 5:
            days += 1
        cur += timedelta(days=1)
    return days


def attendance_filter(emp_code: Optional[str], date_from: Optional[str], date_to: Optional[str], status: Optional[str]) -> dict[str, Any]:
    df = parse_query_date(date_from, "date_from")
    dt = parse_query_date(date_to, "date_to")
    if df and dt and df > dt:
        raise HTTPException(422, "date_from must be on or before date_to")
    if status and status not in ALL_STATUSES:
        raise HTTPException(422, "invalid status")
    q: dict[str, Any] = {}
    if emp_code:
        q["emp_code"] = emp_code
    if df or dt:
        q["date"] = {}
        if df:
            q["date"]["$gte"] = df.isoformat()
        if dt:
            q["date"]["$lte"] = dt.isoformat()
    if status:
        q["status"] = status
    return q


def month_match(month: str) -> tuple[str, str, date, date]:
    start, end = parse_month(month)
    return start.isoformat(), end.isoformat(), start, end


def employee_monthly_pipeline(emp_code: str, start_s: str, end_s: str) -> list[dict[str, Any]]:
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start_s, "$lte": end_s}}},
        {"$addFields": {"date_obj": {"$dateFromString": {"dateString": "$date"}}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": {"$cond": [
                {"$and": [{"$in": ["$status", list(PRESENCE_STATUSES)]}, {"$lte": [{"$dayOfWeek": "$date_obj"}, 6]}, {"$gte": [{"$dayOfWeek": "$date_obj"}, 2]}]},
                {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]},
                0,
            ]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]


def department_summary_pipeline(month: str, department: Optional[str] = None) -> list[dict[str, Any]]:
    start_s, end_s, _, _ = month_match(month)
    match: dict[str, Any] = {"joined_on": {"$lte": end_s}}
    if department:
        match["department"] = department
    return [
        {"$match": match},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"emp": "$emp_code", "joined": "$joined_on"},
            "pipeline": [
                {"$match": {"$expr": {"$and": [{"$eq": ["$emp_code", "$$emp"]}, {"$gte": ["$date", start_s]}, {"$gte": ["$date", "$$joined"]}, {"$lte": ["$date", end_s]}]}}},
                {"$addFields": {"date_obj": {"$dateFromString": {"dateString": "$date"}}}},
            ],
            "as": "logs",
        }},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": "$department",
            "employees": {"$addToSet": "$emp_code"},
            "present_days": {"$sum": {"$cond": [
                {"$and": [{"$in": ["$logs.status", list(PRESENCE_STATUSES)]}, {"$lte": [{"$dayOfWeek": "$logs.date_obj"}, 6]}, {"$gte": [{"$dayOfWeek": "$logs.date_obj"}, 2]}]},
                {"$cond": [{"$ifNull": ["$logs.half_day", False]}, 0.5, 1]},
                0,
            ]}},
            "work_sum": {"$sum": {"$cond": [{"$and": [{"$in": ["$logs.status", list(PRESENCE_STATUSES)]}, {"$ne": ["$logs.work_hours", None]}]}, "$logs.work_hours", 0]}},
            "work_count": {"$sum": {"$cond": [{"$and": [{"$in": ["$logs.status", list(PRESENCE_STATUSES)]}, {"$ne": ["$logs.work_hours", None]}]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}},
        }},
        {"$project": {
            "_id": 0,
            "department": "$_id",
            "headcount": {"$size": "$employees"},
            "present_days": 1,
            "avg_work_hours_raw": {"$cond": [{"$gt": ["$work_count", 0]}, {"$divide": ["$work_sum", "$work_count"]}, None]},
            "late_count": 1,
            "total_late_minutes": 1,
            "leave_count": 1,
            "on_duty_count": 1,
        }},
        {"$sort": {"department": 1}},
    ]


def leaderboard_pipeline(month: str, limit: int, department: Optional[str] = None, apply_limit: bool = True) -> list[dict[str, Any]]:
    start_s, end_s, _, _ = month_match(month)
    emp_match = {"emp.department": department} if department else {}
    pipeline: list[dict[str, Any]] = [
        {"$match": {"date": {"$gte": start_s, "$lte": end_s}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},
    ]
    if emp_match:
        pipeline.append({"$match": emp_match})
    pipeline.extend([
        {"$setWindowFields": {
            "sortBy": {"total_late_minutes": -1},
            "output": {"rank": {"$rank": {}}},
        }},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
    ])
    if apply_limit:
        pipeline.append({"$match": {"rank": {"$lte": limit}}})
    pipeline.append({"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$emp.name", "department": "$emp.department", "total_late_minutes": 1, "late_count": 1}})
    return pipeline


def trend_pipeline(department: str, from_s: str, to_s: str) -> list[dict[str, Any]]:
    return [
        {"$match": {"department": department}},
        {"$group": {"_id": "$department", "employees": {"$push": {"emp_code": "$emp_code", "joined_on": "$joined_on"}}}},
        {"$project": {
            "department": "$_id",
            "day": {"$dateFromString": {"dateString": from_s}},
            "end": {"$dateFromString": {"dateString": to_s}},
            "employees": 1,
        }},
        {"$project": {
            "department": 1,
            "employees": 1,
            "days": {"$map": {
                "input": {"$range": [0, {"$add": [{"$dateDiff": {"startDate": "$day", "endDate": "$end", "unit": "day"}}, 1]}]},
                "as": "i",
                "in": {"$dateAdd": {"startDate": "$day", "unit": "day", "amount": "$$i"}},
            }},
        }},
        {"$unwind": "$days"},
        {"$project": {
            "department": 1,
            "employees": 1,
            "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$days"}},
            "dow": {"$dayOfWeek": "$days"},
        }},
        {"$addFields": {
            "eligible_emp_codes": {"$map": {
                "input": {"$filter": {"input": "$employees", "as": "e", "cond": {"$lte": ["$$e.joined_on", "$date"]}}},
                "as": "e",
                "in": "$$e.emp_code",
            }},
        }},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"emp_codes": "$eligible_emp_codes", "d": "$date"},
            "pipeline": [{"$match": {"$expr": {"$and": [{"$in": ["$emp_code", "$$emp_codes"]}, {"$eq": ["$date", "$$d"]}]}}}],
            "as": "logs",
        }},
        {"$project": {
            "_id": 0,
            "date": 1,
            "is_working_day": {"$and": [{"$gte": ["$dow", 2]}, {"$lte": ["$dow", 6]}]},
            "headcount": {"$size": "$eligible_emp_codes"},
            "present_count": {"$sum": {"$map": {"input": "$logs", "as": "l", "in": {"$cond": [{"$in": ["$$l.status", list(PRESENCE_STATUSES)]}, {"$cond": [{"$ifNull": ["$$l.half_day", False]}, 0.5, 1]}, 0]}}}},
            "late_count": {"$sum": {"$map": {"input": "$logs", "as": "l", "in": {"$cond": [{"$gt": [{"$ifNull": ["$$l.late_minutes", 0]}, 0]}, 1, 0]}}}},
        }},
        {"$addFields": {"attendance_rate_raw": {"$cond": [{"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]}, {"$divide": ["$present_count", "$headcount"]}, None]}}},
        {"$setWindowFields": {"sortBy": {"date": 1}, "output": {"moving_avg_raw": {"$avg": "$attendance_rate_raw", "window": {"documents": [-6, 0]}}}}},
        {"$sort": {"date": 1}},
    ]


def explain_find(collection, query: dict[str, Any], sort: list[tuple[str, int]], page: int, page_size: int) -> dict[str, Any]:
    return collection.find(query, {"_id": 0}).sort(sort).skip((page - 1) * page_size).limit(page_size).explain()


def explain_aggregate(collection_name: str, pipeline: list[dict[str, Any]]) -> dict[str, Any]:
    return db.command("explain", {"aggregate": collection_name, "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")


@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, "MongoDB unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
        employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    q: dict[str, Any] = {}
    if department is not None:
        q["department"] = department
    total = employees.count_documents(q)
    docs = employees.find(q).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    return {"items": [serialize_employee(d) for d in docs], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    punched_at = ms_to_dt(body.punched_at)
    att_date = attendance_date_for_punch(punched_at, emp)
    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": punched_at,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(punched_at, emp, att_date),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        attendance.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return serialize_attendance(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    punched_at = ms_to_dt(body.punched_at)
    candidate = attendance.find_one({"emp_code": body.emp_code, "punch_in": {"$lte": punched_at}}, sort=[("punch_in", DESCENDING)])
    if not candidate:
        raise HTTPException(404, "no eligible punch-in found")
    if candidate.get("punch_out") is not None:
        raise HTTPException(409, "attendance record already punched out")
    validate_punch_interval(candidate["punch_in"], punched_at)
    updates = recompute_derived({**candidate, "punch_out": punched_at}, emp)
    updates["punch_out"] = punched_at
    updated = attendance.find_one_and_update(
        {"_id": candidate["_id"], "punch_out": None},
        {"$set": updates},
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(409, "attendance record already punched out")
    return serialize_attendance(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = attendance_filter(emp_code, date_from, date_to, status)
    total = attendance.count_documents(q)
    docs = attendance.find(q).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip((page - 1) * page_size).limit(page_size)
    return {"items": [serialize_attendance(d) for d in docs], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    body: RegularizeIn,
    emp_code: str = Path(..., alias="emp_code"),
    att_date: str = Path(..., alias="date"),
):
    try:
        parse_date(att_date)
    except ValueError:
        raise HTTPException(422, "date must be YYYY-MM-DD")
    emp = employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    current = attendance.find_one({"emp_code": emp_code, "date": att_date})
    if not current:
        raise HTTPException(404, "attendance record not found")
    proposed = {**current, "history": current.get("history", [])}
    fields_set = body.model_fields_set
    status = body.status if "status" in fields_set else proposed["status"]
    proposed["status"] = status
    if status in {"ABSENT", "LEAVE"}:
        if "punch_in" in fields_set or "punch_out" in fields_set:
            raise HTTPException(422, "ABSENT or LEAVE records cannot include punch times")
        proposed["punch_in"] = None
        proposed["punch_out"] = None
    else:
        if "punch_in" in fields_set:
            if body.punch_in is None:
                raise HTTPException(422, "presence status requires punch_in")
            proposed["punch_in"] = ms_to_dt(body.punch_in)
        if "punch_out" in fields_set:
            proposed["punch_out"] = ms_to_dt(body.punch_out) if body.punch_out is not None else None
        if proposed.get("punch_in") is None:
            raise HTTPException(422, "presence status requires punch_in")
        ensure_regularized_date(proposed["punch_in"], emp, att_date)
        if proposed.get("punch_out") is not None:
            validate_punch_interval(proposed["punch_in"], proposed["punch_out"])
    proposed.update(recompute_derived(proposed, emp))

    comparable = ["status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day"]
    changes = {}
    for key in comparable:
        old = current_value(current, key)
        new = proposed.get(key)
        if old != new:
            changes[key] = {"from": old, "to": new}
    if not changes:
        raise HTTPException(422, "regularization changes nothing")

    history_entry = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    set_fields = {key: proposed[key] for key in comparable}
    history_filter: dict[str, Any]
    if "history" in current:
        history_filter = {"history": current.get("history", [])}
    else:
        history_filter = {"history": {"$exists": False}}
    updated = attendance.find_one_and_update(
        {"_id": current["_id"], **history_filter},
        {"$set": set_fields, "$push": {"history": history_entry}},
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(409, "attendance record was modified concurrently")
    return serialize_attendance(updated)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    emp = employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    start_s, end_s, start_d, end_d = month_match(month)
    effective_start = max(start_d, parse_date(emp["joined_on"]))
    start_s = effective_start.isoformat()
    working_days = working_days_between(effective_start, end_d)
    rows = list(attendance.aggregate(employee_monthly_pipeline(emp_code, start_s, end_s)))
    row = rows[0] if rows else {}
    present_days = round2(row.get("present_days", 0))
    attendance_pct = None if working_days == 0 else round4(Decimal(str(present_days)) / Decimal(working_days) * Decimal(100))
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": int(row.get("leave_days", 0) or 0),
        "late_count": int(row.get("late_count", 0) or 0),
        "total_late_minutes": int(row.get("total_late_minutes", 0) or 0),
        "total_overtime_minutes": int(row.get("total_overtime_minutes", 0) or 0),
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"), department: Optional[str] = None):
    items = []
    for row in db.employees.aggregate(department_summary_pipeline(month, department)):
        items.append({
            "department": row["department"],
            "headcount": int(row["headcount"]),
            "present_days": round2(row.get("present_days", 0)),
            "avg_work_hours": None if row.get("avg_work_hours_raw") is None else round2(row["avg_work_hours_raw"]),
            "late_count": int(row.get("late_count", 0) or 0),
            "total_late_minutes": int(row.get("total_late_minutes", 0) or 0),
            "leave_count": int(row.get("leave_count", 0) or 0),
            "on_duty_count": int(row.get("on_duty_count", 0) or 0),
        })
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    rows = list(attendance.aggregate(leaderboard_pipeline(month, limit, department)))
    return {"month": month, "items": rows}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    date_from: str = Query(..., alias="from"),
    date_to: str = Query(..., alias="to"),
):
    from_d = parse_query_date(date_from, "from")
    to_d = parse_query_date(date_to, "to")
    if to_d < from_d:
        raise HTTPException(422, "to must be on or after from")
    if (to_d - from_d).days > 91:
        raise HTTPException(422, "range cannot exceed 92 days")
    if not employees.find_one({"department": department}):
        raise HTTPException(404, "department not found")
    rows = list(employees.aggregate(trend_pipeline(department, from_d.isoformat(), to_d.isoformat())))
    items = []
    for row in rows:
        rate = row.get("attendance_rate_raw")
        moving = row.get("moving_avg_raw")
        items.append({
            "date": row["date"],
            "is_working_day": bool(row["is_working_day"]),
            "headcount": int(row["headcount"]),
            "present_count": round2(row.get("present_count", 0)),
            "late_count": int(row.get("late_count", 0) or 0),
            "attendance_rate": None if rate is None else round4(rate),
            "moving_avg_7d": None if moving is None else round4(moving),
        })
    return {"department": department, "items": items}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    try:
        if endpoint == "attendance_list":
            q = attendance_filter(emp_code, date_from, date_to, status)
            return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_find(attendance, q, [("date", DESCENDING), ("emp_code", ASCENDING)], page, page_size)}
        if endpoint == "employee_monthly":
            if not emp_code or not month:
                raise HTTPException(422, "emp_code and month are required")
            start_s, end_s, _, _ = month_match(month)
            return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_aggregate("attendance_logs", employee_monthly_pipeline(emp_code, start_s, end_s))}
        if endpoint == "department_summary":
            if not month:
                raise HTTPException(422, "month is required")
            return {"endpoint": endpoint, "collection": "employees", "explain": explain_aggregate("employees", department_summary_pipeline(month, department))}
        if endpoint == "late_leaderboard":
            if not month:
                raise HTTPException(422, "month is required")
            return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_aggregate("attendance_logs", leaderboard_pipeline(month, limit, department))}
        if not department or not from_ or not to:
            raise HTTPException(422, "department, from and to are required")
        from_d = parse_query_date(from_, "from")
        to_d = parse_query_date(to, "to")
        if to_d < from_d or (to_d - from_d).days > 91:
            raise HTTPException(422, "invalid trend range")
        return {"endpoint": endpoint, "collection": "employees", "explain": explain_aggregate("employees", trend_pipeline(department, from_d.isoformat(), to_d.isoformat()))}
    except OperationFailure as exc:
        raise HTTPException(422, str(exc))
