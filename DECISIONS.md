# DECISIONS.md

1. **Indexes.** I created unique `employees.emp_code`, compound `(department, emp_code)`, and `employees.joined_on` because department summary filters employees by `joined_on`. On `attendance_logs`, I indexed unique `(emp_code, date)`, `(date, emp_code)` for pagination, and `(emp_code, punch_in)` for punch-out. I rejected indexing `emp_code` alone since `(emp_code, date)` covers it as a prefix, and rejected `status` due to low cardinality.

2. **Punch-in race.** Both requests execute `insert_one`. The unique compound index on `(emp_code, date)` ensures atomicity: exactly one insert succeeds (201), while the other raises `DuplicateKeyError`. My code catches this and returns 409 without state corruption.

3. **Ties.** I use MongoDB's `$rank` window function for competition ranking (1, 2, 2, 4) and filter `rank <= limit`. All employees tied at the cutoff boundary are returned, sorted deterministically by `emp_code` ascending.

4. **Headcount.** The pipeline starts from employees with `joined_on <= month_end`, runs a `$lookup` into `attendance_logs` per employee, and then groups by department. Because grouping happens after lookup, an employee with no logs still counts.

5. **100x data.** At 100x scale, on-the-fly monthly aggregations would be too slow. I would incrementally pre-aggregate daily rollups into monthly summary collections on punch-out and regularization events, and partition logs by date.
