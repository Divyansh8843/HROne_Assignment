# DECISIONS.md

1. **Indexes.**  
   I created five indexes. A unique index on `employees.emp_code` enforces the one-employee-per-code guarantee at the database layer and makes every employee lookup O(log n). A unique compound index on `attendance_logs(emp_code, date)` is the natural key for attendance records, enforces one-record-per-employee-per-day atomically, and is the primary index for punch-in duplicate detection. A compound index on `(date DESC, emp_code ASC)` matches the required list sort exactly, so the attendance list never needs a sort stage. A single-field index on `status` supports status-filtered list queries. Indexes on `employees.department` and `employees.joined_on` accelerate the cross-collection aggregations. I considered a separate index on `attendance_logs.emp_code` alone but rejected it because the compound `(emp_code, date)` already serves as a left-prefix index for all emp_code-only lookups.

2. **Punch-in race.**  
   Both requests reach `insert_one` at nearly the same instant. MongoDB's unique compound index on `(emp_code, date)` means only one insert can win; the other raises `DuplicateKeyError`. The winning request returns 201; the losing request catches the error and returns 409. There is no check-then-insert pattern, so the race window is zero.

3. **Ties.**  
   `$setWindowFields` with `$rank` assigns standard competition ranks. If limit=2 and two employees share rank 2, both are returned — the response may contain more rows than `limit`. The `$match {"rank": {"$lte": limit}}` stage handles this correctly; rows are then sorted by `total_late_minutes DESC, emp_code ASC`.

4. **Headcount.**  
   The department summary pipeline starts from `employees` (filtered by `joined_on <= last day of month`), then does a `$lookup` with `preserveNullAndEmptyArrays: True` to left-join attendance logs. Employees with no logs survive the unwind as a single row with `logs = null`, so they still contribute one unit to the `$addToSet` headcount accumulator.

5. **One thing for 100× data.**  
   I would move the department trend's headcount calculation into a MongoDB aggregation with `$lookup` rather than fetching all employees into Python. At 100× scale (10 million attendance records, tens of thousands of employees) the Python loop over `dept_employees` per day would become a bottleneck; a correlated `$lookup` with the `(department, joined_on)` index handles it server-side with O(days × log n) work.
