# DECISIONS.md

1. **Indexes.** I create unique indexes on `employees.emp_code` and `(attendance_logs.emp_code, date)` for identity and punch-in races. I also index employee department/join-date queries, attendance date/status listing, employee punch-out lookup by `punch_in`, and employee/month late analytics. I considered a separate status-only index, but rejected it because date-bounded filters are the tested shape.
2. **Punch-in race.** Both requests compute the same IST attendance date and call `insert_one`. MongoDB's unique `(emp_code, date)` index allows one insert; the duplicate raises `DuplicateKeyError`, which becomes HTTP 409.
3. **Ties.** The late leaderboard uses MongoDB `$rank`, so equal total late minutes share a competition rank. Because `limit` is applied to `rank <= limit`, tied employees at the cutoff are all returned, ordered by total late minutes then `emp_code`.
4. **Headcount.** Department summary starts from `employees`, filters `joined_on <= period_end`, groups by department, and only then looks up logs. Employees with zero logs therefore still contribute to headcount.
5. **100x data.** I would partition/archive attendance by date, add read-optimized summary collections maintained by change streams or batch jobs, and run analytics from those rollups while keeping raw logs for audit.
