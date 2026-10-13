"""
Unit test suite verifying all business rules (R1 - R8) and calculations.
"""
import sys
from datetime import datetime, timezone

# Add app to path
sys.path.insert(0, ".")
from app.main import (
    compute_attendance_date,
    compute_half_day,
    compute_late_minutes,
    compute_overtime,
    compute_work_hours,
    count_working_days,
    is_overnight_shift,
    ms_to_datetime_utc,
    round_half_up,
    truncate_to_seconds,
)


def test_business_rules():
    print("Testing Business Rules R1 - R8...")

    # --- R1: Overnight shift detection and attendance date ---
    assert is_overnight_shift("22:00", "06:00") is True
    assert is_overnight_shift("09:30", "18:30") is False
    assert is_overnight_shift("18:00", "18:00") is True

    # Overnight punch-in on 2026-07-07 at 01:15 IST (which is 2026-07-06T19:45:00Z)
    dt_early_morning = datetime(2026, 7, 6, 19, 45, 0, tzinfo=timezone.utc)
    date_assigned = compute_attendance_date(dt_early_morning, "22:00", "06:00")
    assert date_assigned == "2026-07-06", f"Expected 2026-07-06, got {date_assigned}"

    # Normal overnight punch-in on 2026-07-06 at 21:55 IST (2026-07-06T16:25:00Z)
    dt_evening = datetime(2026, 7, 6, 16, 25, 0, tzinfo=timezone.utc)
    date_evening = compute_attendance_date(dt_evening, "22:00", "06:00")
    assert date_evening == "2026-07-06", f"Expected 2026-07-06, got {date_evening}"

    print("  [PASS] R1 Overnight date logic passed")

    # --- R2: Late Minutes ---
    # Shift 09:30:
    # 09:40:00 IST -> 0 (grace 10 min)
    dt_094000 = datetime(2026, 7, 6, 4, 10, 0, tzinfo=timezone.utc)
    assert compute_late_minutes(dt_094000, "09:30", "2026-07-06") == 0

    # 09:40:01 IST -> 10 (strictly more than 10 min)
    dt_094001 = datetime(2026, 7, 6, 4, 10, 1, tzinfo=timezone.utc)
    assert compute_late_minutes(dt_094001, "09:30", "2026-07-06") == 10

    # 10:15:59 IST -> 45
    dt_101559 = datetime(2026, 7, 6, 4, 45, 59, tzinfo=timezone.utc)
    assert compute_late_minutes(dt_101559, "09:30", "2026-07-06") == 45

    print("  [PASS] R2 Late minutes boundary checks passed")

    # --- Sample 1 from DATA_MODEL.md ---
    # In 09:28 IST (03:58 UTC), Out 18:35 IST (13:05 UTC)
    s1_in = datetime(2026, 7, 6, 3, 58, 0, tzinfo=timezone.utc)
    s1_out = datetime(2026, 7, 6, 13, 5, 0, tzinfo=timezone.utc)
    assert compute_late_minutes(s1_in, "09:30", "2026-07-06") == 0
    assert compute_work_hours(s1_in, s1_out) == 9.12
    assert compute_overtime(s1_out, "09:30", "18:30", "2026-07-06") == 0
    assert compute_half_day(9.12) is False
    print("  [PASS] Sample 1 (Normal full day) verified")

    # --- Sample 2 from DATA_MODEL.md ---
    # In 10:05 IST (04:35 UTC), Out 19:10 IST (13:40 UTC)
    s2_in = datetime(2026, 7, 6, 4, 35, 0, tzinfo=timezone.utc)
    s2_out = datetime(2026, 7, 6, 13, 40, 0, tzinfo=timezone.utc)
    assert compute_late_minutes(s2_in, "09:30", "2026-07-06") == 35
    assert compute_work_hours(s2_in, s2_out) == 9.08
    assert compute_overtime(s2_out, "09:30", "18:30", "2026-07-06") == 40
    assert compute_half_day(9.08) is False
    print("  [PASS] Sample 2 (Late, with overtime) verified")

    # --- Sample 4 from DATA_MODEL.md ---
    # Half day: 3.5 hours worked (< 4.50)
    assert compute_half_day(3.50) is True
    assert compute_half_day(4.49) is True
    assert compute_half_day(4.50) is False
    print("  [PASS] Sample 4 (Half day) verified")

    # --- Sample 7 from DATA_MODEL.md ---
    # Overnight shift (22:00 - 06:00), in 21:55 IST, out 06:40 IST next day
    s7_in = datetime(2026, 7, 6, 16, 25, 0, tzinfo=timezone.utc)
    s7_out = datetime(2026, 7, 7, 1, 10, 0, tzinfo=timezone.utc)
    assert compute_attendance_date(s7_in, "22:00", "06:00") == "2026-07-06"
    assert compute_late_minutes(s7_in, "22:00", "2026-07-06") == 0
    assert compute_work_hours(s7_in, s7_out) == 8.75
    assert compute_overtime(s7_out, "22:00", "06:00", "2026-07-06") == 40
    assert compute_half_day(8.75) is False
    print("  [PASS] Sample 7 (Overnight shift) verified")

    # --- R7: Mid-month joiner working days ---
    # July 2026: 31 days. Starts on Wednesday, ends on Friday.
    # Total Mon-Fri in July 2026: 23 working days.
    full_month_days = count_working_days("2026-01-01", "2026-07")
    assert full_month_days == 23, f"Expected 23, got {full_month_days}"

    # Joined on 2026-07-20 (Monday). Remaining July working days: 10
    mid_join_days = count_working_days("2026-07-20", "2026-07")
    assert mid_join_days == 10, f"Expected 10, got {mid_join_days}"

    # Future joiner: joined on 2026-08-01. July working days: 0
    future_join_days = count_working_days("2026-08-01", "2026-07")
    assert future_join_days == 0, f"Expected 0, got {future_join_days}"
    print("  [PASS] R7 Working days calculation verified")

    # --- R8: Rounding half-up ---
    assert round_half_up(9.125, 2) == 9.13
    assert round_half_up(9.124, 2) == 9.12
    assert round_half_up(0.85715, 4) == 0.8572
    print("  [PASS] R8 Round half-up verified")

    print("\nALL BUSINESS RULE TESTS PASSED SUCCESSFULLY! OK")




# --------------------------------------------------------------------------- #
# Extra checks (no MongoDB needed)
# --------------------------------------------------------------------------- #
def test_edge_cases_and_pipelines():
    import json

    from bson import json_util
    from fastapi.testclient import TestClient

    from app import main

    # R4 half-up: 4.505 h must round to 4.51 (banker's/float rounding would give 4.5)
    pin = datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc)
    pout = datetime(2026, 7, 6, 8, 30, 18, tzinfo=timezone.utc)  # 4.505 h exactly
    assert compute_work_hours(pin, pout) == 4.51

    # R3: 29 min -> 0, 30 min -> 30, 59 s inside a minute floors
    assert compute_overtime(datetime(2026, 7, 6, 13, 30, 0, tzinfo=timezone.utc), "09:30", "18:30", "2026-07-06") == 30
    assert compute_overtime(datetime(2026, 7, 6, 13, 29, 59, tzinfo=timezone.utc), "09:30", "18:30", "2026-07-06") == 0

    # R1: instants are truncated to whole seconds
    assert ms_to_datetime_utc(1783312500900) == ms_to_datetime_utc(1783312500000)

    # Every pipeline builder must produce BSON-serialisable stages (catches typos before a DB exists)
    pipes = [
        main.pipeline_employee_monthly("EMP0001", "2026-07"),
        main.pipeline_department_summary("2026-07", None),
        main.pipeline_late_leaderboard("2026-07", 10, "Engineering"),
        main.pipeline_department_trend("Engineering", "2026-07-01", "2026-07-31"),
    ]
    for p in pipes:
        assert isinstance(p, list) and p
        json_util.dumps(p)

    # The trend pipeline rounds half-up by hand, so it must not contain Mongo's half-even $round
    assert "$round" not in json_util.dumps(pipes[3])

    # Validation paths that never touch the database
    c = TestClient(main.app)
    assert c.get("/attendance?status=BOGUS").status_code == 422
    assert c.get("/attendance?date_from=2026-07-10&date_to=2026-07-01").status_code == 422
    assert c.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": 1700000000}).status_code == 422
    assert c.post("/employees", json={"emp_code": "EMP0001", "name": "A", "email": "a@b.co",
                                      "department": "X", "joined_on": "2026-02-31"}).status_code == 422
    assert c.get("/analytics/leaderboard/late?month=2026-13").status_code == 422
    assert c.get("/admin/explain/not_an_endpoint").status_code == 422


if __name__ == "__main__":
    test_business_rules()
    test_edge_cases_and_pipelines()
    print("EDGE CASE + PIPELINE BUILD TESTS PASSED")
