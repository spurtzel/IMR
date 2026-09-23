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
        A AS A.primary_type = 'ROBBERY',
        B AS B.primary_type = 'BATTERY' AND B.x_coord BETWEEN A.x_coord - 1320 AND A.x_coord + 1320 AND B.y_coord BETWEEN A.y_coord - 1320 AND A.y_coord + 1320 AND B.real_time BETWEEN A.real_time AND A.real_time + INTERVAL '7' DAY,
        C AS C.primary_type = 'ASSAULT' AND C.x_coord BETWEEN A.x_coord - 1320 AND A.x_coord + 1320 AND C.y_coord BETWEEN A.y_coord - 1320 AND A.y_coord + 1320 AND C.real_time BETWEEN A.real_time AND A.real_time + INTERVAL '7' DAY AND C.x_coord BETWEEN B.x_coord - 1320 AND B.x_coord + 1320 AND C.y_coord BETWEEN B.y_coord - 1320 AND B.y_coord + 1320 AND C.real_time BETWEEN B.real_time AND B.real_time + INTERVAL '7' DAY,
        Z AS TRUE
)
