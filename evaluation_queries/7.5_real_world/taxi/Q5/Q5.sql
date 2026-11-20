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
        A AS A.pu_location_id = 132,
        B AS B.total_amount > 0 AND B.pu_location_id = A.do_location_id AND B.do_location_id = A.pu_location_id AND B.real_time BETWEEN A.real_time AND A.real_time + INTERVAL '90' MINUTE,
        C AS C.total_amount > 0 AND C.pu_location_id = B.do_location_id AND C.do_location_id = B.pu_location_id AND C.real_time BETWEEN B.real_time AND B.real_time + INTERVAL '24' HOUR AND C.real_time BETWEEN A.real_time AND A.real_time + INTERVAL '24' HOUR,
        Z AS TRUE
)
