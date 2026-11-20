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
        C AS C.primary_type = 'T2' AND C.lon BETWEEN A.lon - 0.02 AND A.lon + 0.02 AND C.lat BETWEEN A.lat - 0.02 AND A.lat + 0.02,
        D AS D.primary_type = 'T3' AND D.lon BETWEEN A.lon - 0.02 AND A.lon + 0.02 AND D.lat BETWEEN A.lat - 0.02 AND A.lat + 0.02,
        E AS E.primary_type = 'T4' AND E.lon BETWEEN A.lon - 0.02 AND A.lon + 0.02 AND E.lat BETWEEN A.lat - 0.02 AND A.lat + 0.02,
        F AS F.primary_type = 'T5' AND F.lon BETWEEN A.lon - 0.02 AND A.lon + 0.02 AND F.lat BETWEEN A.lat - 0.02 AND A.lat + 0.02,
        Z AS TRUE
)
