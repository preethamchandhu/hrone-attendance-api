# DECISIONS.md (about 150-200 words in total - plain language, your own words)

1. **Indexes.**
   I created unique indexes on `employees(emp_code)` and `attendance_logs(emp_code, date)` to enforce natural uniqueness and guarantee race-safe operations. For query performance, I added `attendance_logs(date DESC, emp_code ASC)` for list pagination, `(emp_code, punch_in DESC)` for fast punch-out lookups, and `(date, late_minutes, emp_code)` for analytics. I considered and rejected indexing `history` sub-documents, as audit entries are append-only and never queried individually.

2. **Punch-in race.**
   Both requests validate the employee and calculate the target attendance date. Upon concurrent `insert_one`, MongoDB's unique index on `(emp_code, date)` guarantees atomic serialization: the first insert commits (HTTP 201), while the second fails with `DuplicateKeyError` (E11000). The exception is caught and immediately returned as HTTP 409 Conflict without data corruption.

3. **Ties.**
   The aggregation pipeline uses `$setWindowFields` with `$rank: {}` to assign standard competition ranks (1, 2, 2, 4). Filtering with `rank <= limit` returns all employees whose rank qualifies. If two employees tie at the cutoff, both are returned (yielding more than `limit` items), ordered deterministically by `emp_code` ascending as specified by the contract.

4. **Headcount.**
   The department summary aggregation starts on `employees` (filtering `joined_on <= month_end`) rather than attendance logs. Grouping by department counts every employee (`$sum: 1`), ensuring headcount includes employees with zero attendance records before correlating logs via `$lookup`.

5. **One thing you would change for 100x data.**
   I would maintain pre-aggregated daily and monthly rollup collections updated incrementally at punch/regularization time, removing the need for real-time cross-collection joins and `$densify` stages over tens of millions of raw logs.
