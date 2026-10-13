"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)
"""
import calendar
import os
import re
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from pydantic import BaseModel, Field, field_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

load_dotenv()

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    serverSelectionTimeoutMS=3000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


@app.on_event("startup")
def create_indexes():
    # Employees indexes
    db.employees.create_index("emp_code", unique=True)
    db.employees.create_index([("department", 1), ("emp_code", 1)])
    db.employees.create_index("joined_on")

    # Attendance logs indexes
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)])


def round_half_up(val: Optional[float], decimals: int = 2) -> Optional[float]:
    if val is None:
        return None
    d = Decimal(str(val))
    target = Decimal("1" if decimals == 0 else f"0.{'0' * (decimals - 1)}1")
    return float(d.quantize(target, rounding=ROUND_HALF_UP))


EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt - EPOCH) // timedelta(milliseconds=1)


def from_epoch_ms(ms: int) -> datetime:
    # R1: truncate sub-seconds before calculation
    return datetime.fromtimestamp(ms // 1000, tz=timezone.utc)


def parse_date(s: str) -> date:
    if not re.fullmatch(r"^\d{4}-\d{2}-\d{2}$", s or ""):
        raise HTTPException(422, "Invalid date format, expected YYYY-MM-DD")
    try:
        return date.fromisoformat(s)
    except ValueError:
        raise HTTPException(422, "Invalid calendar date")


def check_month(month: str) -> str:
    if not re.fullmatch(r"^\d{4}-(0[1-9]|1[0-2])$", month or ""):
        raise HTTPException(422, "Invalid month format, expected YYYY-MM")
    return month


def compute_late_minutes(punch_in: datetime, shift_start: str, date_str: str) -> int:
    h, m = map(int, shift_start.split(":"))
    start_ist = datetime.fromisoformat(date_str).replace(hour=h, minute=m, second=0, microsecond=0, tzinfo=IST)
    diff_sec = int((punch_in.astimezone(IST) - start_ist).total_seconds())
    # R2: late only if strictly > 10:00 (600s); floor minutes since shift_start
    return (diff_sec // 60) if diff_sec > 600 else 0


def compute_work_hours(punch_in: datetime, punch_out: Optional[datetime]) -> Optional[float]:
    if not punch_out:
        return None
    sec = Decimal(str(int((punch_out - punch_in).total_seconds())))
    # R4: 2 decimals half-up, not banker's rounding
    hours = (sec / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(hours)


def compute_overtime(punch_out: datetime, shift_start: str, shift_end: str, date_str: str) -> int:
    end_h, end_m = map(int, shift_end.split(":"))
    start_h, start_m = map(int, shift_start.split(":"))

    base_date = datetime.fromisoformat(date_str).date()
    # Overnight shift ends on the next calendar day
    if time(end_h, end_m) <= time(start_h, start_m):
        base_date += timedelta(days=1)

    end_ist = datetime.combine(base_date, time(end_h, end_m), tzinfo=IST)
    diff_sec = int((punch_out.astimezone(IST) - end_ist).total_seconds())
    minutes = diff_sec // 60
    # R3: counted only when >= 30 minutes
    return minutes if minutes >= 30 else 0


def serialize_employee(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": to_epoch_ms(doc["created_at"]),
    }


def serialize_log(doc: dict) -> dict:
    out_history = []
    for h in doc.get("history") or []:
        entry = {
            "at": to_epoch_ms(h.get("at")),
            "by": h.get("by"),
            "reason": h.get("reason"),
            "changes": {},
        }
        for k, v in h.get("changes", {}).items():
            fr = v.get("from")
            to = v.get("to")
            if isinstance(fr, datetime):
                fr = to_epoch_ms(fr)
            if isinstance(to, datetime):
                to = to_epoch_ms(to)
            entry["changes"][k] = {"from": fr, "to": to}
        out_history.append(entry)

    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc.get("status", "PRESENT"),
        "punch_in": to_epoch_ms(doc.get("punch_in")),
        "punch_out": to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": out_history,
    }


def build_attendance_query(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
) -> dict:
    q = {}
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
    return q


def build_employee_monthly_pipeline(emp_code: str, month: str) -> list:
    year, mon = map(int, month.split("-"))
    last_day = calendar.monthrange(year, mon)[1]
    return [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": f"{month}-01", "$lte": f"{month}-{last_day:02d}"},
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
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 5]},
                                ]
                            },
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
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


def build_department_summary_pipeline(month: str, department: Optional[str] = None) -> list:
    year, mon = map(int, month.split("-"))
    last_day = calendar.monthrange(year, mon)[1]

    match_filter = {"joined_on": {"$lte": f"{month}-{last_day:02d}"}}
    if department:
        match_filter["department"] = department

    return [
        {"$match": match_filter},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$code"]},
                                    {"$gte": ["$date", f"{month}-01"]},
                                    {"$lte": ["$date", f"{month}-{last_day:02d}"]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {
            "$addFields": {
                "emp_present_days": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {
                                "$cond": [
                                    {
                                        "$and": [
                                            {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                            {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$$l.date"}}}, 5]},
                                        ]
                                    },
                                    {"$cond": [{"$eq": ["$$l.half_day", True]}, 0.5, 1.0]},
                                    0.0,
                                ]
                            },
                        }
                    }
                },
                "emp_late_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {"$cond": [{"$gt": ["$$l.late_minutes", 0]}, 1, 0]},
                        }
                    }
                },
                "emp_late_minutes": {"$sum": "$logs.late_minutes"},
                "emp_leave_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {"$cond": [{"$eq": ["$$l.status", "LEAVE"]}, 1, 0]},
                        }
                    }
                },
                "emp_on_duty_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {"$cond": [{"$eq": ["$$l.status", "ON_DUTY"]}, 1, 0]},
                        }
                    }
                },
                "emp_work_hours": {
                    "$map": {
                        "input": {
                            "$filter": {
                                "input": "$logs",
                                "as": "l",
                                "cond": {
                                    "$and": [
                                        {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                        {"$ne": ["$$l.work_hours", None]},
                                    ]
                                },
                            }
                        },
                        "as": "f",
                        "in": "$$f.work_hours",
                    }
                },
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": "$emp_present_days"},
                "late_count": {"$sum": "$emp_late_count"},
                "total_late_minutes": {"$sum": "$emp_late_minutes"},
                "leave_count": {"$sum": "$emp_leave_count"},
                "on_duty_count": {"$sum": "$emp_on_duty_count"},
                "all_work_hours": {"$push": "$emp_work_hours"},
            }
        },
        {"$sort": {"_id": 1}},
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "present_days": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
                "avg_work_hours": {
                    "$let": {
                        "vars": {
                            "flattened": {
                                "$reduce": {
                                    "input": "$all_work_hours",
                                    "initialValue": [],
                                    "in": {"$concatArrays": ["$$value", "$$this"]},
                                }
                            }
                        },
                        "in": {
                            "$cond": [
                                {"$gt": [{"$size": "$$flattened"}, 0]},
                                {"$avg": "$$flattened"},
                                None,
                            ]
                        },
                    }
                },
            }
        },
    ]


def build_late_leaderboard_pipeline(month: str, limit: int = 10, department: Optional[str] = None) -> list:
    year, mon = map(int, month.split("-"))
    last_day = calendar.monthrange(year, mon)[1]

    pipe = [
        {
            "$match": {
                "date": {"$gte": f"{month}-01", "$lte": f"{month}-{last_day:02d}"},
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
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]

    if department:
        pipe.append({"$match": {"emp.department": department}})

    pipe.extend([
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {"$rank": {}}
                },
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ])
    return pipe


def build_department_trend_pipeline(department: str, from_date: str, to_date: str) -> list:
    d_from = date.fromisoformat(from_date)
    d_to = date.fromisoformat(to_date)
    from_dt = datetime.combine(d_from, time.min, tzinfo=timezone.utc)
    to_dt_plus_1 = datetime.combine(d_to + timedelta(days=1), time.min, tzinfo=timezone.utc)

    emps = list(db.employees.find({"department": department}, {"_id": 0, "emp_code": 1}))
    emp_codes = [e["emp_code"] for e in emps]

    return [
        {
            "$match": {
                "emp_code": {"$in": emp_codes},
                "date": {"$gte": from_date, "$lte": to_date},
            }
        },
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
            }
        },
        {
            "$project": {
                "date_val": {"$dateFromString": {"dateString": "$_id"}},
                "present_count": 1,
                "late_count": 1,
            }
        },
        {
            "$densify": {
                "field": "date_val",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [from_dt, to_dt_plus_1],
                },
            }
        },
        {
            "$project": {
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date_val"}},
                "date_val": 1,
                "is_working_day": {"$lte": [{"$isoDayOfWeek": "$date_val"}, 5]},
                "present_count": {"$ifNull": ["$present_count", 0.0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"cur_date": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$cur_date"]},
                                ]
                            }
                        }
                    },
                    {"$count": "count"},
                ],
                "as": "hc",
            }
        },
        {
            "$addFields": {
                "headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.count", 0]}, 0]},
            }
        },
        {
            "$addFields": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                {"$eq": ["$is_working_day", True]},
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date_val": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {
                            "documents": [-6, "current"]
                        }
                    }
                }
            }
        },
        {"$sort": {"date": 1}},
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1,
            }
        },
    ]


class EmployeeIn(BaseModel):
    emp_code: str = Field(..., min_length=1, max_length=30)
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("emp_code")
    @classmethod
    def check_emp_code(cls, v: str) -> str:
        v = v.strip()
        if not v or any(c.isspace() for c in v) or len(v) > 30:
            raise ValueError("emp_code must be 1-30 characters without whitespace")
        return v

    @field_validator("joined_on")
    @classmethod
    def check_joined_on(cls, v: str) -> str:
        if not re.fullmatch(r"^\d{4}-\d{2}-\d{2}$", v or ""):
            raise ValueError("Invalid date format, expected YYYY-MM-DD")
        try:
            date.fromisoformat(v)
        except ValueError:
            raise ValueError("Invalid calendar date")
        return v


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, strict=True, ge=100_000_000_000, le=4_102_444_800_000)
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, strict=True, ge=100_000_000_000, le=4_102_444_800_000)


class RegularizeIn(BaseModel):
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[int] = Field(None, strict=True, ge=100_000_000_000, le=4_102_444_800_000)
    punch_out: Optional[int] = Field(None, strict=True, ge=100_000_000_000, le=4_102_444_800_000)
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)


@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(503, "Database unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(timezone.utc).replace(microsecond=0)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}
    if department:
        q["department"] = department
    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    items = [
        serialize_employee(d)
        for d in db.employees.find(q).sort("emp_code", 1).skip(skip).limit(page_size)
    ]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    if body.punched_at is not None:
        ts = from_epoch_ms(body.punched_at)
    else:
        ts = datetime.now(timezone.utc).replace(microsecond=0)

    punch_ist = ts.astimezone(IST)
    start_t = time.fromisoformat(emp["shift_start"])
    end_t = time.fromisoformat(emp["shift_end"])

    # Overnight shift: punches before shift_end belong to yesterday's shift
    if end_t <= start_t and punch_ist.time() < end_t:
        att_date = (punch_ist.date() - timedelta(days=1)).isoformat()
    else:
        att_date = punch_ist.date().isoformat()

    late_mins = compute_late_minutes(ts, emp["shift_start"], att_date)

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_mins,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "Already punched in for this date")

    return serialize_log(doc)


@app.post("/attendance/punch-out", status_code=200)
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    if body.punched_at is not None:
        ts = from_epoch_ms(body.punched_at)
    else:
        ts = datetime.now(timezone.utc).replace(microsecond=0)

    # Most recent record whose punch_in is at or before punch_out
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": ts}},
        sort=[("punch_in", -1)],
    )
    if not rec:
        raise HTTPException(404, "No punch-in record found")

    pin = rec["punch_in"]
    if pin.tzinfo is None:
        pin = pin.replace(tzinfo=timezone.utc)

    if ts <= pin:
        raise HTTPException(422, "punch_out must be strictly after punch_in")
    if (ts - pin).total_seconds() > 24 * 3600:
        raise HTTPException(422, "punch_out cannot be more than 24 hours after punch_in")

    if rec.get("punch_out") is not None:
        raise HTTPException(409, "Record is already punched out")

    wh = compute_work_hours(pin, ts)
    ot = compute_overtime(ts, emp["shift_start"], emp["shift_end"], rec["date"])
    hd = (wh is not None and wh < 4.50)

    # Atomic close guarded on punch_out: None so concurrent calls can't both succeed
    res = db.attendance_logs.update_one(
        {"_id": rec["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": ts,
                "work_hours": wh,
                "overtime_minutes": ot,
                "half_day": hd,
            }
        },
    )
    if res.matched_count == 0:
        raise HTTPException(409, "Record is already punched out")

    rec["punch_out"] = ts
    rec["work_hours"] = wh
    rec["overtime_minutes"] = ot
    rec["half_day"] = hd
    return serialize_log(rec)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    d_from = parse_date(date_from) if date_from else None
    d_to = parse_date(date_to) if date_to else None
    if d_from and d_to and d_from > d_to:
        raise HTTPException(422, "date_from must be <= date_to")
    if status and status not in ("PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"):
        raise HTTPException(422, "Invalid status")

    q = build_attendance_query(emp_code, date_from, date_to, status)
    skip = (page - 1) * page_size
    total = db.attendance_logs.count_documents(q)
    cursor = (
        db.attendance_logs.find(q)
        .sort([("date", -1), ("emp_code", 1)])
        .skip(skip)
        .limit(page_size)
    )
    items = [serialize_log(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str,
    path_date: str = Path(..., alias="date"),
    body: RegularizeIn = ...,
):
    parse_date(path_date)

    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    for attempt in range(2):
        rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": path_date})
        if not rec:
            raise HTTPException(404, "Attendance record not found")

        new_status = body.status if body.status is not None else rec["status"]
        if new_status in ("ABSENT", "LEAVE") and (body.punch_in is not None or body.punch_out is not None):
            raise HTTPException(422, "Cannot supply punch_in or punch_out when status is ABSENT or LEAVE")

        if new_status in ("ABSENT", "LEAVE"):
            new_punch_in = None
            new_punch_out = None
            new_work_hours = None
            new_late_minutes = 0
            new_overtime_minutes = 0
            new_half_day = False
        else:
            new_punch_in = from_epoch_ms(body.punch_in) if body.punch_in is not None else rec.get("punch_in")
            if new_punch_in is None:
                raise HTTPException(422, "Presence status requires punch_in")
            if new_punch_in.tzinfo is None:
                new_punch_in = new_punch_in.replace(tzinfo=timezone.utc)

            # R1: punch_in must stay on the record's attendance date
            punch_ist = new_punch_in.astimezone(IST)
            start_t = time.fromisoformat(emp["shift_start"])
            end_t = time.fromisoformat(emp["shift_end"])
            if end_t <= start_t and punch_ist.time() < end_t:
                derived_date = (punch_ist.date() - timedelta(days=1)).isoformat()
            else:
                derived_date = punch_ist.date().isoformat()
            if derived_date != path_date:
                raise HTTPException(422, "punch_in must stay on the record's attendance date")

            new_punch_out = from_epoch_ms(body.punch_out) if body.punch_out is not None else rec.get("punch_out")
            if new_punch_out is not None:
                if new_punch_out.tzinfo is None:
                    new_punch_out = new_punch_out.replace(tzinfo=timezone.utc)
                if new_punch_out <= new_punch_in:
                    raise HTTPException(422, "punch_out must be strictly after punch_in")
                if (new_punch_out - new_punch_in).total_seconds() > 24 * 3600:
                    raise HTTPException(422, "punch_out cannot be more than 24 hours after punch_in")
                new_work_hours = compute_work_hours(new_punch_in, new_punch_out)
                new_overtime_minutes = compute_overtime(new_punch_out, emp["shift_start"], emp["shift_end"], path_date)
                new_half_day = (new_work_hours is not None and new_work_hours < 4.50)
            else:
                new_work_hours = None
                new_overtime_minutes = 0
                new_half_day = False

            new_late_minutes = compute_late_minutes(new_punch_in, emp["shift_start"], path_date)

        # Record only fields that actually changed
        changes = {}
        if rec.get("status") != new_status:
            changes["status"] = {"from": rec.get("status"), "to": new_status}
        if rec.get("punch_in") != new_punch_in:
            changes["punch_in"] = {"from": rec.get("punch_in"), "to": new_punch_in}
        if rec.get("punch_out") != new_punch_out:
            changes["punch_out"] = {"from": rec.get("punch_out"), "to": new_punch_out}
        if rec.get("work_hours") != new_work_hours:
            changes["work_hours"] = {"from": rec.get("work_hours"), "to": new_work_hours}
        if rec.get("late_minutes", 0) != new_late_minutes:
            changes["late_minutes"] = {"from": rec.get("late_minutes", 0), "to": new_late_minutes}
        if rec.get("overtime_minutes", 0) != new_overtime_minutes:
            changes["overtime_minutes"] = {"from": rec.get("overtime_minutes", 0), "to": new_overtime_minutes}
        if rec.get("half_day", False) != new_half_day:
            changes["half_day"] = {"from": rec.get("half_day", False), "to": new_half_day}

        if not changes:
            raise HTTPException(422, "No fields changed")

        now_utc = datetime.now(timezone.utc).replace(microsecond=0)
        hist_entry = {
            "at": now_utc,
            "by": body.regularized_by,
            "reason": body.reason,
            "changes": changes,
        }

        set_fields = {
            "status": new_status,
            "punch_in": new_punch_in,
            "punch_out": new_punch_out,
            "work_hours": new_work_hours,
            "late_minutes": new_late_minutes,
            "overtime_minutes": new_overtime_minutes,
            "half_day": new_half_day,
        }

        # Compare-and-set: ensure status, punch_in, and punch_out haven't changed since read
        cas_filter = {
            "emp_code": emp_code,
            "date": path_date,
            "status": rec.get("status"),
            "punch_in": rec.get("punch_in"),
            "punch_out": rec.get("punch_out"),
        }

        updated = db.attendance_logs.find_one_and_update(
            cas_filter,
            {
                "$set": set_fields,
                "$push": {"history": hist_entry},
            },
            return_document=ReturnDocument.AFTER,
        )
        if updated:
            return serialize_log(updated)

    raise HTTPException(409, "Record was modified concurrently")


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    year, mon = map(int, month.split("-"))
    last_day = calendar.monthrange(year, mon)[1]
    m_start = date(year, mon, 1)
    m_end = date(year, mon, last_day)

    # R7: working days count Mon-Fri from joined_on for mid-month joiners
    joined_d = date.fromisoformat(emp["joined_on"])
    working_days = 0
    if joined_d <= m_end:
        cur = max(m_start, joined_d)
        while cur <= m_end:
            if cur.weekday() < 5:
                working_days += 1
            cur += timedelta(days=1)

    pipeline = build_employee_monthly_pipeline(emp_code, month)
    docs = list(db.attendance_logs.aggregate(pipeline))
    res = docs[0] if docs else {}

    present_days = round_half_up(res.get("present_days", 0.0), 2)
    pct = (
        round_half_up((present_days / working_days) * 100, 2)
        if working_days > 0
        else None
    )

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": res.get("leave_days", 0),
        "late_count": res.get("late_count", 0),
        "total_late_minutes": res.get("total_late_minutes", 0),
        "total_overtime_minutes": res.get("total_overtime_minutes", 0),
        "attendance_pct": pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    pipeline = build_department_summary_pipeline(month, department)
    items = []
    for d in db.employees.aggregate(pipeline, hint="joined_on_1"):
        d["present_days"] = round_half_up(d["present_days"], 2)
        d["avg_work_hours"] = round_half_up(d["avg_work_hours"], 2)
        items.append(d)

    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    pipeline = build_late_leaderboard_pipeline(month, limit, department)
    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to"),
):
    d_from = parse_date(from_date)
    d_to = parse_date(to_date)

    if d_to < d_from:
        raise HTTPException(422, "to must be >= from")
    if (d_to - d_from).days > 92:
        raise HTTPException(422, "Date range exceeds 92 days")

    if not db.employees.find_one({"department": department}):
        raise HTTPException(404, "Department not found")

    pipeline = build_department_trend_pipeline(department, from_date, to_date)
    items = []
    for row in db.attendance_logs.aggregate(pipeline):
        row["present_count"] = round_half_up(row["present_count"], 2)
        row["attendance_rate"] = round_half_up(row["attendance_rate"], 4)
        row["moving_avg_7d"] = round_half_up(row["moving_avg_7d"], 4)
        items.append(row)

    return {"department": department, "items": items}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    valid_endpoints = (
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    )
    if endpoint not in valid_endpoints:
        raise HTTPException(404, f"Unknown endpoint: {endpoint}")

    if endpoint == "attendance_list":
        d_from = parse_date(date_from) if date_from else None
        d_to = parse_date(date_to) if date_to else None
        if d_from and d_to and d_from > d_to:
            raise HTTPException(422, "date_from must be <= date_to")
        if status and status not in ("PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"):
            raise HTTPException(422, "Invalid status")
        q = build_attendance_query(emp_code, date_from, date_to, status)
        skip = (page - 1) * page_size
        explain = db.command({
            "explain": {
                "find": "attendance_logs",
                "filter": q,
                "sort": {"date": -1, "emp_code": 1},
                "skip": skip,
                "limit": page_size,
            },
            "verbosity": "executionStats",
        })
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "Missing emp_code or month query parameter")
        check_month(month)
        pipe = build_employee_monthly_pipeline(emp_code, month)
        explain = db.command({
            "explain": {
                "aggregate": "attendance_logs",
                "pipeline": pipe,
                "cursor": {},
            },
            "verbosity": "executionStats",
        })
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "Missing month query parameter")
        check_month(month)
        pipe = build_department_summary_pipeline(month, department)
        explain = db.command({
            "explain": {
                "aggregate": "employees",
                "pipeline": pipe,
                "cursor": {},
                "hint": "joined_on_1",
            },
            "verbosity": "executionStats",
        })
        return {"endpoint": endpoint, "collection": "employees", "explain": explain}

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "Missing month query parameter")
        check_month(month)
        pipe = build_late_leaderboard_pipeline(month, limit, department)
        explain = db.command({
            "explain": {
                "aggregate": "attendance_logs",
                "pipeline": pipe,
                "cursor": {},
            },
            "verbosity": "executionStats",
        })
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}

    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(422, "Missing department, from, or to query parameter")
        d_from = parse_date(from_date)
        d_to = parse_date(to_date)
        if d_to < d_from:
            raise HTTPException(422, "to must be >= from")
        if (d_to - d_from).days > 92:
            raise HTTPException(422, "Date range exceeds 92 days")
        pipe = build_department_trend_pipeline(department, from_date, to_date)
        explain = db.command({
            "explain": {
                "aggregate": "attendance_logs",
                "pipeline": pipe,
                "cursor": {},
            },
            "verbosity": "executionStats",
        })
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}
