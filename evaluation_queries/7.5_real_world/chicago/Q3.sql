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
        D.time AS d_time
    ONE ROW PER MATCH
    AFTER MATCH SKIP TO NEXT ROW
    PATTERN (A Z* B Z* C Z* D)
    DEFINE
        A AS A.primary_type = 'BURGLARY',
        B AS B.primary_type = 'MOTOR VEHICLE THEFT' AND B.beat = A.beat AND B.x_coord BETWEEN A.x_coord - 1320 AND A.x_coord + 1320 AND B.y_coord BETWEEN A.y_coord - 1320 AND A.y_coord + 1320 AND B.real_time BETWEEN A.real_time AND A.real_time + INTERVAL '24' HOUR,
        C AS C.primary_type = 'THEFT' AND C.x_coord BETWEEN B.x_coord - 2640 AND B.x_coord + 2640 AND C.y_coord BETWEEN B.y_coord - 2640 AND B.y_coord + 2640 AND C.real_time BETWEEN B.real_time AND B.real_time + INTERVAL '48' HOUR,
        D AS D.primary_type = 'CRIMINAL DAMAGE' AND D.x_coord BETWEEN C.x_coord - 2640 AND C.x_coord + 2640 AND D.y_coord BETWEEN C.y_coord - 2640 AND C.y_coord + 2640 AND D.real_time BETWEEN C.real_time AND C.real_time + INTERVAL '48' HOUR AND D.real_time BETWEEN A.real_time AND A.real_time + INTERVAL '72' HOUR,
        Z AS TRUE
)
