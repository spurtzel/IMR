SELECT *
FROM events MATCH_RECOGNIZE (
    ORDER BY time
    MEASURES
        A.id AS a_id,
        A.time AS a_time,
        B.id AS b_id,
        B.time AS b_time
    ONE ROW PER MATCH
    AFTER MATCH SKIP TO NEXT ROW
    PATTERN (A Z*? B)
    DEFINE
        A AS A.primary_type = 'BATTERY',
        B AS B.arrest = 1 AND B.beat = A.beat AND B.x_coord BETWEEN A.x_coord - 2640 AND A.x_coord + 2640 AND B.y_coord BETWEEN A.y_coord - 2640 AND A.y_coord + 2640 AND B.real_time BETWEEN A.real_time AND A.real_time + INTERVAL '3' DAY,
        Z AS TRUE
)
