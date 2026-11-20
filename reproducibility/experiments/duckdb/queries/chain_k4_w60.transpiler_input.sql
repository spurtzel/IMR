SELECT *
FROM events_window MATCH_RECOGNIZE (
    ORDER BY time
    MEASURES
        A.id AS a_id,
        A.time AS a_time,
        B.id AS b_id,
        B.time AS b_time,
        C.id AS c_id,
        C.time AS c_time,
        D.id AS d_id,
        D.time AS d_time
    ONE ROW PER MATCH
    AFTER MATCH SKIP TO NEXT ROW
    PATTERN (A Z* B Z* C Z* D)
    DEFINE
        A AS A.primary_type = 'T0',
        B AS B.primary_type = 'T1' AND B.lon >= A.lon - 0.02 AND B.lon <= A.lon + 0.02 AND B.lat >= A.lat - 0.02 AND B.lat <= A.lat + 0.02,
        C AS C.primary_type = 'T2' AND C.lon >= B.lon - 0.02 AND C.lon <= B.lon + 0.02 AND C.lat >= B.lat - 0.02 AND C.lat <= B.lat + 0.02,
        D AS D.primary_type = 'T3' AND D.lon >= C.lon - 0.02 AND D.lon <= C.lon + 0.02 AND D.lat >= C.lat - 0.02 AND D.lat <= C.lat + 0.02 AND abs(date_diff('millisecond', A.ts, D.ts)) <= 60,
        Z AS date_diff('millisecond', A.ts, Z.ts) <= 60
);
