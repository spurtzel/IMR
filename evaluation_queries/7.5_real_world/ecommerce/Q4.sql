SELECT *
FROM events MATCH_RECOGNIZE (
    ORDER BY time
    MEASURES
        A.id AS a_id,
        A.time AS a_time,
        COUNT(B.id) AS b_count,
        FIRST(B.id) AS b_first_id,
        LAST(B.id) AS b_last_id,
        C.id AS c_id,
        C.time AS c_time
    ONE ROW PER MATCH
    AFTER MATCH SKIP TO NEXT ROW
    PATTERN (A Z*? B+ Z* C)
    DEFINE
        A AS A.event_type = 'cart',
        B AS B.event_type = 'view',
        C AS C.event_type = 'purchase' AND C.user_id = A.user_id AND C.product_id = A.product_id AND C.real_time BETWEEN A.real_time AND A.real_time + INTERVAL '30' MINUTE,
        Z AS TRUE
)
