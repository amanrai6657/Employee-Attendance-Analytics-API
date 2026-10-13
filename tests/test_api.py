import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pymongo.errors import PyMongoError

os.environ.setdefault("MONGO_URI", "mongodb://localhost:27017")
os.environ["MONGO_DB"] = "attendance_test"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.main import app, create_indexes, db  # noqa: E402


def ms(year, month, day, hour, minute, second=0):
    return int(datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc).timestamp() * 1000)


@pytest.fixture(autouse=True)
def clean_db():
    try:
        db.client.admin.command("ping")
    except PyMongoError:
        pytest.skip("MongoDB is not available")
    db.employees.drop()
    db.attendance_logs.drop()
    create_indexes()
    yield
    db.employees.drop()
    db.attendance_logs.drop()


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def employee(emp_code="EMP0001", department="Engineering", joined_on="2026-07-01", shift_start="09:30", shift_end="18:30"):
    return {
        "emp_code": emp_code,
        "name": f"User {emp_code}",
        "email": f"{emp_code.lower()}@example.com",
        "department": department,
        "joined_on": joined_on,
        "shift_start": shift_start,
        "shift_end": shift_end,
    }


def test_employee_validation_pagination_and_duplicate(client):
    assert client.post("/employees", json=employee()).status_code == 201
    assert client.post("/employees", json=employee()).status_code == 409
    assert client.post("/employees", json={**employee("EMP0002"), "shift_start": "09:30", "shift_end": "09:30"}).status_code == 422
    assert client.get("/employees", params={"page": 0}).status_code == 422
    body = client.get("/employees").json()
    assert body["total"] == 1
    assert "_id" not in body["items"][0]
    assert isinstance(body["items"][0]["created_at"], int)


def test_punch_in_boundaries_and_duplicate_race(client):
    client.post("/employees", json=employee())
    r = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 4, 10)})
    assert r.status_code == 201
    assert r.json()["late_minutes"] == 0
    patch = client.patch("/attendance/EMP0001/2026-07-06", json={"punch_in": ms(2026, 7, 6, 4, 10, 1), "reason": "boundary check", "regularized_by": "hr"})
    assert patch.status_code == 200, patch.text
    assert client.get("/attendance", params={"emp_code": "EMP0001"}).json()["items"][0]["late_minutes"] == 10

    def punch():
        return client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 7, 4, 0)}).status_code

    with ThreadPoolExecutor(max_workers=4) as pool:
        statuses = list(pool.map(lambda _: punch(), range(4)))
    assert statuses.count(201) == 1
    assert statuses.count(409) == 3


def test_punch_out_atomic_and_overtime(client):
    client.post("/employees", json=employee())
    client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 4, 0)})

    def punch_out():
        return client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 13, 40)}).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(lambda _: punch_out(), range(2)))
    assert statuses.count(200) == 1
    assert statuses.count(409) == 1
    row = client.get("/attendance", params={"emp_code": "EMP0001"}).json()["items"][0]
    assert row["work_hours"] == 9.67
    assert row["overtime_minutes"] == 40
    assert row["half_day"] is False


def test_regularization_history_and_noop(client):
    client.post("/employees", json=employee())
    client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 4, 35)})
    client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 13, 0)})
    patch = client.patch("/attendance/EMP0001/2026-07-06", json={"punch_in": ms(2026, 7, 6, 3, 58), "reason": "biometric glitch", "regularized_by": "hr.admin"})
    assert patch.status_code == 200
    body = patch.json()
    assert body["late_minutes"] == 0
    assert len(body["history"]) == 1
    assert set(body["history"][0]["changes"]).issuperset({"punch_in", "late_minutes", "work_hours"})
    assert client.patch("/attendance/EMP0001/2026-07-06", json={"reason": "nothing changed", "regularized_by": "hr.admin"}).status_code == 422


def test_analytics_trend_and_explain(client):
    client.post("/employees", json=employee("EMP0001", "Engineering", "2026-07-01"))
    client.post("/employees", json=employee("EMP0002", "Engineering", "2026-07-08"))
    client.post("/employees", json=employee("EMP0003", "Sales", "2026-07-01"))
    db.attendance_logs.insert_many([
        {"emp_code": "EMP0001", "date": "2026-07-06", "status": "PRESENT", "punch_in": datetime(2026, 7, 6, 4, 0, tzinfo=timezone.utc), "punch_out": datetime(2026, 7, 6, 13, 0, tzinfo=timezone.utc), "work_hours": 9.0, "late_minutes": 0, "overtime_minutes": 0, "half_day": False, "history": []},
        {"emp_code": "EMP0001", "date": "2026-07-07", "status": "WFH", "punch_in": datetime(2026, 7, 7, 4, 0, tzinfo=timezone.utc), "punch_out": datetime(2026, 7, 7, 7, 30, tzinfo=timezone.utc), "work_hours": 3.5, "late_minutes": 0, "overtime_minutes": 0, "half_day": True, "history": []},
        {"emp_code": "EMP0003", "date": "2026-07-06", "status": "PRESENT", "punch_in": datetime(2026, 7, 6, 4, 35, tzinfo=timezone.utc), "punch_out": datetime(2026, 7, 6, 13, 40, tzinfo=timezone.utc), "work_hours": 9.08, "late_minutes": 35, "overtime_minutes": 40, "half_day": False, "history": []},
    ])
    monthly = client.get("/analytics/employees/EMP0001/monthly", params={"month": "2026-07"}).json()
    assert monthly["working_days"] == 23
    assert monthly["present_days"] == 1.5
    summary = client.get("/analytics/departments/summary", params={"month": "2026-07"}).json()["items"]
    engineering = next(x for x in summary if x["department"] == "Engineering")
    assert engineering["headcount"] == 2
    assert engineering["present_days"] == 1.5
    trend = client.get("/analytics/departments/Engineering/trend", params={"from": "2026-07-06", "to": "2026-07-08"}).json()["items"]
    assert [x["date"] for x in trend] == ["2026-07-06", "2026-07-07", "2026-07-08"]
    assert trend[0]["attendance_rate"] == 1.0
    assert trend[2]["headcount"] == 2
    leaderboard = client.get("/analytics/leaderboard/late", params={"month": "2026-07"}).json()["items"]
    assert leaderboard[0]["emp_code"] == "EMP0003"
    explain = client.get("/admin/explain/attendance_list").json()["explain"]
    assert "COLLSCAN" not in str(explain)
