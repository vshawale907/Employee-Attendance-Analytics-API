"""
dev/scratch/demo_starter_bugs.py
Demonstrates each defect in the original app/main.py BEFORE the fix.
Run: .venv/Scripts/python dev/scratch/demo_starter_bugs.py
"""
import sys
sys.path.insert(0, ".")

from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP

IST = timezone(timedelta(hours=5, minutes=30))

print("=" * 60)
print("BUG 1: compute_late_minutes (L33-37)")
print("=" * 60)

def compute_late_minutes_BROKEN(punch_in: datetime, shift_start: str) -> int:
    """Exact copy of the starter's function."""
    h, m = map(int, shift_start.split(":"))
    start = punch_in.replace(hour=h, minute=m, second=0, microsecond=0)
    minutes = int((punch_in - start).total_seconds() / 60)  # floors first!
    return minutes if minutes > 10 else 0   # then compares

def compute_late_minutes_FIXED(punch_in_utc: datetime, shift_start: str, date_str: str, shift_end: str) -> int:
    from datetime import date as date_type
    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))
    d = date_type.fromisoformat(date_str)
    start_ist = datetime(d.year, d.month, d.day, sh, sm, 0, tzinfo=IST)
    start_utc = start_ist.astimezone(timezone.utc)
    elapsed_s = int((punch_in_utc.replace(microsecond=0) - start_utc).total_seconds())
    return elapsed_s // 60 if elapsed_s > 600 else 0

# 09:40:01 IST = 04:10:01 UTC (shift 09:30 IST = 04:00 UTC)
punch_0940_01 = datetime(2026, 7, 6, 4, 10, 1, tzinfo=timezone.utc)  # 09:40:01 IST

# Bug: starter anchors on punch_in's own timezone/date, not IST
# And it floors minutes BEFORE comparing
broken_result = compute_late_minutes_BROKEN(punch_0940_01, "09:30")
fixed_result  = compute_late_minutes_FIXED(punch_0940_01, "09:30", "2026-07-06", "18:30")
print(f"  09:40:01 IST punch-in with 09:30 shift:")
print(f"  BROKEN → {broken_result}  (seconds elapsed=601, but starter floors to 10 then asks 10>10=False → 0)")
print(f"  FIXED  → {fixed_result}  (601 > 600 is True → 601//60 = 10)")
assert broken_result == 0, "Bug confirmed"
assert fixed_result == 10, "Fix verified"

# Test: 09:40:00 should also give 0
punch_0940_00 = datetime(2026, 7, 6, 4, 10, 0, tzinfo=timezone.utc)  # 09:40:00 IST
print(f"  09:40:00 IST: broken={compute_late_minutes_BROKEN(punch_0940_00,'09:30')}, fixed={compute_late_minutes_FIXED(punch_0940_00,'09:30','2026-07-06','18:30')}")

# IST timezone bug: starter uses punch_in.replace(hour=h,...) — this anchors shift start on UTC time,
# ignoring IST offset entirely.
utc_punch = datetime(2026, 7, 6, 4, 0, 1, tzinfo=timezone.utc)  # 09:30:01 IST
broken_on_utc = compute_late_minutes_BROKEN(utc_punch, "09:30")
fixed_on_utc  = compute_late_minutes_FIXED(utc_punch, "09:30", "2026-07-06", "18:30")
print(f"\n  IST timezone bug: punch 04:00:01 UTC (= 09:30:01 IST, 1 sec late):")
print(f"  BROKEN → {broken_on_utc}  (uses UTC 04:00 as 'start', thinks elapsed=1s, correct by accident on UTC server!)")
print(f"  FIXED  → {fixed_on_utc}  (1 second > 600? No → 0, correctly not late)")

print()
print("=" * 60)
print("BUG 2: compute_work_hours uses Python round() — half-even")
print("=" * 60)

def compute_work_hours_BROKEN(punch_in, punch_out):
    return round((punch_out - punch_in).total_seconds() / 3600, 2)

def compute_work_hours_FIXED(punch_in, punch_out):
    secs = int((punch_out.replace(microsecond=0) - punch_in.replace(microsecond=0)).total_seconds())
    return float((Decimal(secs) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

# 0.125 hours = 450 seconds
t0 = datetime(2026,7,6, 0, 0, 0, tzinfo=timezone.utc)
t1 = datetime(2026,7,6, 0, 7, 30, tzinfo=timezone.utc)  # 450 seconds = 0.125 h
broken_wh = compute_work_hours_BROKEN(t0, t1)
fixed_wh  = compute_work_hours_FIXED(t0, t1)
print(f"  450 seconds (0.125 hours): broken={broken_wh} (half-even rounds DOWN), fixed={fixed_wh} (half-up rounds UP)")
assert broken_wh == 0.12, f"Bug confirmed, got {broken_wh}"
assert fixed_wh  == 0.13, f"Fix verified, got {fixed_wh}"

# R5 half_day check: 16199 seconds
t2 = datetime(2026,7,6, 4,30, 0, tzinfo=timezone.utc)
t3 = t2 + timedelta(seconds=16199)
broken_16199 = compute_work_hours_BROKEN(t2, t3)
fixed_16199  = compute_work_hours_FIXED(t2, t3)
print(f"  16199 seconds: broken={broken_16199}, fixed={fixed_16199}")
print(f"  half_day with BROKEN: {broken_16199 < 4.50}, with FIXED: {fixed_16199 < 4.50}  ← different!")

print()
print("=" * 60)
print("BUG 3: compute_overtime — no 30-min minimum + naive/aware mix")
print("=" * 60)

def compute_overtime_BROKEN(punch_out: datetime, shift_end: str, date_str: str) -> int:
    h, m = map(int, shift_end.split(":"))
    end = datetime.fromisoformat(date_str).replace(hour=h, minute=m, tzinfo=IST)  # aware
    # punch_out from fromtimestamp() would be naive — TypeError here!
    return max(0, int((punch_out - end).total_seconds() / 60))

# Test 29-minute overtime — BROKEN gives 29, should give 0
punch_out_29min = datetime(2026, 7, 6, 13, 59, 0, tzinfo=timezone.utc)  # 18:30+29min=18:59 IST... actually let me calc
# Shift ends 18:30 IST = 13:00 UTC. 29 min later = 13:29 UTC
punch_out_29 = datetime(2026, 7, 6, 13, 29, 0, tzinfo=timezone.utc)
broken_ot_29 = compute_overtime_BROKEN(punch_out_29, "18:30", "2026-07-06")
print(f"  29 min overtime: BROKEN → {broken_ot_29}  (should be 0, R3 requires >=30 min)")
assert broken_ot_29 == 29, "Bug confirmed: no 30-min threshold"

print()
print("=" * 60)
print("BUG 10: create_employee — datetime.now() is naive local time")
print("=" * 60)
naive_now = datetime.now()
aware_utc = datetime.now(timezone.utc)
print(f"  datetime.now()        → {naive_now!r}  ← NO timezone info (tzinfo=None)")
print(f"  datetime.now(UTC)     → {aware_utc!r}  ← has tzinfo")
print(f"  On IST laptop they look similar; on UTC grader server naive vs aware matters")

print()
print("=" * 60)
print("BUG 11: list_employees — skip = page * page_size (off by one)")
print("=" * 60)
for page in [1, 2, 3]:
    broken_skip = page * 20
    fixed_skip  = (page - 1) * 20
    print(f"  page={page}: BROKEN skip={broken_skip}, FIXED skip={fixed_skip}")
print("  page=1 should show items 1-20 (skip=0), but starter skips 20!")

print()
print("=" * 60)
print("BUG 2 rounding vectors from spec §5.3")
print("=" * 60)
cases = [
    ("0.125 → 0.13", Decimal("0.125"), "0.01"),
    ("2.675 → 2.68", Decimal("2.675"), "0.01"),
]
for label, val, dp in cases:
    python_round = round(float(val), len(dp)-2 if dp=='0.01' else 4)
    half_up = float(val.quantize(Decimal(dp), rounding=ROUND_HALF_UP))
    print(f"  {label}: Python round={python_round}, half-up={half_up}")

print()
print("ALL BUGS DEMONSTRATED ✓")
