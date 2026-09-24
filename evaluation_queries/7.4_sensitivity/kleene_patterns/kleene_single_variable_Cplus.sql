-- view-maintenance microbenchmark (fig:kleene, left panel): one Kleene variable over the event log
SELECT first_id, first_ts, first_val, last_id, last_ts, last_val, cnt
FROM (SELECT id, ts, m, val FROM events) MATCH_RECOGNIZE (
    ORDER BY ts
    MEASURES FIRST(C.id) AS first_id, FIRST(C.ts) AS first_ts, FIRST(C.val) AS first_val, LAST(C.id) AS last_id, LAST(C.ts) AS last_ts, LAST(C.val) AS last_val, COUNT(C.id) AS cnt
    ONE ROW PER MATCH
    AFTER MATCH SKIP TO NEXT ROW
    PATTERN (C+)
    DEFINE C AS C.m = 1
)
