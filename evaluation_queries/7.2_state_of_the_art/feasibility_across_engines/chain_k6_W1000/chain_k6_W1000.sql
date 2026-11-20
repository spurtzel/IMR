SELECT *
FROM events MATCH_RECOGNIZE (
    ORDER BY time
    MEASURES
        A.id AS a_id,
        A.time AS a_time,
        B.id AS b_id,
        B.time AS b_time,
        C.id AS c_id,
        C.time AS c_time,
        D.id AS d_id,
        D.time AS d_time,
        E.id AS e_id,
        E.time AS e_time,
        F.id AS f_id,
        F.time AS f_time
    ONE ROW PER MATCH
    AFTER MATCH SKIP TO NEXT ROW
    PATTERN (A Z* B Z* C Z* D Z* E Z* F)
    DEFINE
        A AS A.primary_type = 'T0',
        B AS B.primary_type = 'T1' AND B.lon BETWEEN A.lon - 0.02 AND A.lon + 0.02 AND B.lat BETWEEN A.lat - 0.02 AND A.lat + 0.02,
        C AS C.primary_type = 'T2' AND C.lon BETWEEN B.lon - 0.02 AND B.lon + 0.02 AND C.lat BETWEEN B.lat - 0.02 AND B.lat + 0.02,
        D AS D.primary_type = 'T3' AND D.lon BETWEEN C.lon - 0.02 AND C.lon + 0.02 AND D.lat BETWEEN C.lat - 0.02 AND C.lat + 0.02,
        E AS E.primary_type = 'T4' AND E.lon BETWEEN D.lon - 0.02 AND D.lon + 0.02 AND E.lat BETWEEN D.lat - 0.02 AND D.lat + 0.02,
        F AS F.primary_type = 'T5' AND F.lon BETWEEN E.lon - 0.02 AND E.lon + 0.02 AND F.lat BETWEEN E.lat - 0.02 AND E.lat + 0.02 AND F.ts BETWEEN A.ts AND A.ts + INTERVAL '1' SECOND,
        Z AS TRUE
)
