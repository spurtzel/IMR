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
        E.time AS e_time
    ONE ROW PER MATCH
    AFTER MATCH SKIP TO NEXT ROW
    PATTERN (A Z* B Z* C Z* D Z* E)
    DEFINE
        A AS A.primary_type = 'BURGLARY',
        B AS B.primary_type = 'THEFT' AND B.x_coord BETWEEN A.x_coord - 2640 AND A.x_coord + 2640 AND B.y_coord BETWEEN A.y_coord - 2640 AND A.y_coord + 2640 AND B.real_time BETWEEN A.real_time AND A.real_time + INTERVAL '72' HOUR,
        C AS C.primary_type = 'MOTOR VEHICLE THEFT' AND C.x_coord BETWEEN B.x_coord - 2640 AND B.x_coord + 2640 AND C.y_coord BETWEEN B.y_coord - 2640 AND B.y_coord + 2640 AND C.real_time BETWEEN B.real_time AND B.real_time + INTERVAL '72' HOUR,
        D AS D.primary_type = 'CRIMINAL DAMAGE' AND D.x_coord BETWEEN C.x_coord - 2640 AND C.x_coord + 2640 AND D.y_coord BETWEEN C.y_coord - 2640 AND C.y_coord + 2640 AND D.real_time BETWEEN C.real_time AND C.real_time + INTERVAL '72' HOUR,
        E AS E.primary_type = 'BATTERY' AND E.x_coord BETWEEN D.x_coord - 2640 AND D.x_coord + 2640 AND E.y_coord BETWEEN D.y_coord - 2640 AND D.y_coord + 2640 AND E.real_time BETWEEN D.real_time AND D.real_time + INTERVAL '72' HOUR AND E.real_time BETWEEN A.real_time AND A.real_time + INTERVAL '7' DAY,
        Z AS TRUE
)
