# Employee Attendance & Analytics API

FastAPI + PyMongo implementation of the HROne attendance assignment. The app stores employees and attendance logs in MongoDB, exposes punch-in/out and regularization workflows, and computes monthly, department, leaderboard, and trend analytics with MongoDB queries/aggregation pipelines.

## Stack

- Python 3.11+
- FastAPI
- PyMongo
- MongoDB 6.0+ (tested locally with a real MongoDB instance)
- Pytest for integration tests

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

Configure MongoDB with environment variables. A local `.env` is loaded for convenience, but real environment variables take precedence:

```bash
set MONGO_URI=mongodb://localhost:27017
set MONGO_DB=attendance_db
```

Seed the sample data:

```bash
python sample_seed.py
```

Run the API:

```bash
uvicorn app.main:app --port 8000
```

API docs are available at:

- `http://127.0.0.1:8000/docs`
- `http://127.0.0.1:8000/openapi.json`

## Tests

```bash
pytest -q
```

The tests use a separate `attendance_test` database and require a real MongoDB server. They skip only if MongoDB is unavailable.

## Indexes

Startup creates indexes idempotently:

- `employees.emp_code` unique for employee identity.
- `employees.department, joined_on, emp_code` for department trend and filtered employee queries.
- `employees.joined_on, department` for monthly department summaries.
- `attendance_logs.emp_code, date` unique for one record per employee/day.
- `attendance_logs.date desc, emp_code` for attendance list sorting.
- `attendance_logs.emp_code, punch_in desc` for punch-out lookup.
- `attendance_logs.date, status, emp_code` for date-bounded analytics and listing filters.
- `attendance_logs.emp_code, date, late_minutes` for employee monthly and late analytics.

## Verification Status

Executed locally against real MongoDB:

- `python sample_seed.py` loaded 6 employees and 9 attendance logs.
- `pytest -q` passed: 5 tests.
- `uvicorn app.main:app --port 8000` started successfully.
- `GET /health` returned `{"status":"ok"}`.
- Explain checks for `attendance_list`, `employee_monthly`, `department_summary`, `late_leaderboard`, and `department_trend` returned plans containing `IXSCAN` and no `COLLSCAN` on the seeded/local test data.
- A temporary `attendance_perf` database with 500 employees and 100,000 attendance logs was generated and dropped after measurement. Local response times were: attendance list 29.6 ms, employee monthly 7.1 ms, department summary 635.3 ms, late leaderboard 2216.0 ms, department trend 140.7 ms. Explain checks on those query shapes also showed `IXSCAN` and no `COLLSCAN`.

## Known Limitations

The 100,000-record run was a local smoke benchmark, not a controlled production benchmark. For substantially larger deployments, the late leaderboard and department-wide analytics should move to rollups or precomputed reporting collections.
