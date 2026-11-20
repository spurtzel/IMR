SELECT *
FROM events MATCH_RECOGNIZE (
    ORDER BY time
    MEASURES
        A.id AS a_id,
        A.time AS a_time,
        B.id AS b_id,
        B.time AS b_time,
        C.id AS c_id,
        C.time AS c_time
    ONE ROW PER MATCH
    AFTER MATCH SKIP TO NEXT ROW
    PATTERN (A Z* B Z* C)
    DEFINE
        A AS A.pu_location_id = 161,
        B AS B.pu_location_id = 161 AND B.total_amount > A.total_amount + 8 AND B.real_time BETWEEN A.real_time AND A.real_time + INTERVAL '20' MINUTE,
        C AS C.pu_location_id = 161 AND C.total_amount > B.total_amount + 8 AND C.real_time BETWEEN B.real_time AND B.real_time + INTERVAL '20' MINUTE,
        Z AS TRUE
)
