# REVIEW.md

List every defect you found in the starter `app/main.py` (helpers and endpoints).

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `health` | Always returned ok without checking MongoDB. | Stop MongoDB and `/health` still returned 200. | Ping MongoDB and return 503 on driver errors. |
| 2 | module startup | No indexes were created. | Duplicate employees or attendance records could be inserted under concurrency. | Added idempotent startup index creation, including both unique constraints. |
| 3 | Mongo client setup | Used default naive datetime handling and loose timeouts. | BSON datetimes could serialize inconsistently and health checks could hang. | Enabled `tz_aware=True`, UTC tzinfo, and server/connect timeouts. |
| 4 | `EmployeeIn` | Missing contract validation for emp code, email, dates, field lengths, and identical shifts. | Invalid employee bodies were accepted. | Added Pydantic validators and strict patterns/defaults. |
| 5 | `create_employee` | Used check-then-insert for duplicate detection. | Concurrent duplicate creates could both pass the precheck. | Rely on the unique index and convert `DuplicateKeyError` to 409. |
| 6 | `create_employee` | Stored `created_at` as local naive `datetime.now()`. | Returned times depended on server timezone. | Store truncated UTC server time and return epoch milliseconds. |
| 7 | `list_employees` | Pagination used `page * page_size`, skipping page 1. | `GET /employees?page=1` missed the first records. | Use `(page - 1) * page_size`. |
| 8 | `list_employees` | `total` ignored the department filter. | Filtered requests reported the collection total. | Count with the same query used for the page. |
| 9 | `list_employees` | Did not sort by `emp_code` and did not validate page limits. | Result order was unstable and `page_size=10000` was accepted. | Added FastAPI query constraints and `emp_code` ascending sort. |
| 10 | `compute_late_minutes` | Calculated shift start by replacing fields on the punch datetime and ignored IST/overnight dates. | Overnight and timezone boundary punches produced wrong dates/late minutes. | Compute the attendance date and shift boundary in IST, then compare UTC instants. |
| 11 | `compute_work_hours` | Used Python `round`, which is banker's rounding on binary floats. | Half-up boundary cases rounded incorrectly. | Use `Decimal` with `ROUND_HALF_UP`. |
| 12 | `compute_overtime` | Counted any positive overtime and did not handle overnight shift ends. | 5 minutes after shift end was returned as overtime; overnight shifts were wrong. | Apply the 30-minute threshold and compute shift end from IST attendance date. |
| 13 | `punch_in` | Did not handle unknown employees before indexing `emp["shift_start"]`. | Unknown employee caused a 500 instead of 404. | Added explicit employee lookup and 404. |
| 14 | `punch_in` | Accepted any status, including `ABSENT`/`LEAVE`. | `POST /attendance/punch-in` could create non-presence records. | Restricted status to `PRESENT`, `WFH`, and `ON_DUTY`. |
| 15 | `punch_in` | Used local naive timestamps and did not reject strings/floats/seconds. | Invalid timestamp inputs were accepted or misinterpreted. | Added strict epoch-millisecond validation and whole-second truncation. |
| 16 | `punch_in` | Used check-then-insert for attendance uniqueness. | Parallel identical punch-ins could both succeed. | Use database-enforced unique `(emp_code, date)` insert and map duplicate key to 409. |
| 17 | `punch_in` | Returned an `id` field derived from Mongo `_id`. | Contract says no `_id` or attendance id in responses. | Removed all internal identifiers from serializers. |
| 18 | `list_attendance` | Loaded all matching records into Python and sorted/sliced in memory. | Large datasets would be slow and memory-heavy. | Push filtering, sorting, skip, and limit into MongoDB. |
| 19 | `list_attendance` | Returned Mongo `_id` as `id` and raw datetimes. | Response schema did not match OpenAPI. | Added response serializers converting BSON dates to epoch milliseconds and omitting `_id`. |
| 20 | `list_attendance` | Did not validate date ranges, status, or pagination bounds. | Bad dates and `date_from > date_to` were accepted. | Added explicit validation and FastAPI query constraints. |

Also inspected: the collection names `employees` and `attendance_logs`, the use of `emp_code` as the business key, and the default employee shift values. Those matched the supplied data model and were preserved.
