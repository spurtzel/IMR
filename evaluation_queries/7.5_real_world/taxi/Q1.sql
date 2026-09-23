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
        A AS A.total_amount BETWEEN 12 AND 30,
        B AS B.total_amount > 0 AND B.pu_location_id = A.pu_location_id AND B.do_location_id = A.do_location_id AND B.total_amount > A.total_amount + 15 AND B.real_time BETWEEN A.real_time AND A.real_time + INTERVAL '15' MINUTE,
        C AS C.total_amount > 0 AND C.pu_location_id = B.pu_location_id AND C.do_location_id = B.do_location_id AND C.total_amount > B.total_amount + 15 AND C.real_time BETWEEN B.real_time AND B.real_time + INTERVAL '60' MINUTE,
        Z AS TRUE
)
