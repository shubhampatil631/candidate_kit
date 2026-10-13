# Employee Attendance & Analytics API

## How to run

1. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
2. Set environment variables (or rely on default localhost MongoDB):
   ```bash
   export MONGO_URI="mongodb://localhost:27017"
   export MONGO_DB="attendance_db"
   ```
3. Run the service:
   ```bash
   uvicorn app.main:app --port 8000
   ```

## Notes

All business rules, concurrency scenarios, and aggregation explain plans were implemented and validated against the local dataset and concurrency test scripts. I have not run the service against the full 100,000-record dataset on a live replica set, but all query plans demonstrate index scans without collection scans.
