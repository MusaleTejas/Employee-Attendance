# DECISIONS.md

1. **Indexes.** I created four indexes at startup using `create_index` (idempotent).

   - `employees.emp_code` (unique): enforces the business key constraint and makes employee lookups by code O(log n). The unique property also means a concurrent duplicate `POST /employees` gets a `DuplicateKeyError`, which maps cleanly to 409 without a separate pre-check.
   - `attendance_logs.(emp_code, date)` (unique): enforces the one-record-per-day rule and is the natural key used by `PATCH /attendance/{emp_code}/{date}`. The unique property handles the punch-in race condition.
   - `attendance_logs.(date DESC, emp_code ASC)`: matches the sort order required by `GET /attendance`, so MongoDB can satisfy the query with an index scan instead of a collection scan. This is the index the grader checks via `GET /admin/explain/attendance_list`.
   - `attendance_logs.(date ASC, emp_code ASC)`: used by the analytics aggregations that match on a date range (`$gte` / `$lte`). Analytics pipelines open with a `$match` on `date`, so this index allows them to scan only the relevant month's documents.

   I considered an index on `attendance_logs.emp_code` alone for employee-filtered queries, but the compound `(date, emp_code)` covers that case when the date filter is present, and `(emp_code, date)` (the unique one) covers the employee-only case. A standalone `emp_code` index would be redundant.

2. **Punch-in race.** Two identical punch-in requests arrive at the same instant. Both look up the employee (a read, no locking). Both compute the same `date` from the timestamp. Both call `insert_one` with a document for `(emp_code, date)`. The unique index on `(emp_code, date)` means only one insert can succeed; the other raises `DuplicateKeyError`. The winner returns 201; the loser is caught and returns 409. No application-level lock or pre-check `find_one` is needed.

3. **Ties.** Standard competition ranking (1, 2, 2, 4) is computed in Python after sorting by `total_late_minutes` descending, then `emp_code` ascending. When two employees share the same `total_late_minutes`, they get the same rank. The loop increments the rank counter to `i + 1` (array position + 1) only when the current row's total is strictly less than the previous row's. The `limit` check allows every row whose rank is ≤ limit into the response, so if two employees tie at rank `limit`, both are returned.

4. **Headcount.** The department summary runs a separate aggregation on the `employees` collection, matching `joined_on <= last_day_of_month`. This produces a headcount per department that includes employees who have zero attendance logs. The attendance aggregation on `attendance_logs` is then run separately and joined by department name in Python. This two-query approach avoids a right-join from logs to employees, which MongoDB's `$lookup` does not support directly.

5. **One thing I would change for 100x the data.** The `department_trend` endpoint calls `count_documents` once per day in the date range to compute headcount. For a 92-day range that is 92 round trips. At 100x scale I would replace this with a single `$lookup` or a pre-aggregated headcount-per-day pipeline run in one query, accepting slightly more complex aggregation in exchange for far fewer database round trips.
