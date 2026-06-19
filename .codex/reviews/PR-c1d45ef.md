# Codex review — v3.0.0 statistics-first redesign

## Verdict: REQUEST_CHANGES

### MAJOR
1. statistics.py: missing/null revisions don't self-correct. A day previously
   imported then retracted (is_missing/null) stays in the ledger AND its recorder
   row persists (HA import updates/inserts, never deletes omitted rows).
2. sensor.py _latest_point: returns newest dated point even if is_missing/null →
   latest_daily_consumption can show None for a missing placeholder, hiding the
   real latest reading.

### MINOR
1. monthly import dropped the 1<=month<=12 guard → a malformed month raises before
   any stats emit for that property.
2. ledger load has no shape validation → corrupted Store fails import forever.
3. account-ref change starts a new ledger identity (acceptable if contractual).

### Security
- ledger Store not removed on entry removal (async_remove_entry removes snapshot +
  auth only). Contains account ref + dated consumption/cost.

### Performance
- _auth_lock held across the whole _fetch_all incl. Store writes + stats import;
  blocks heartbeat/manual refresh. Import after releasing the lock.

### Other
- test removal list misses cost_last_month_total + average_monthly_cost.
