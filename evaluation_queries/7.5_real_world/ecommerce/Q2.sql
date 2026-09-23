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
        A AS A.event_type = 'view',
        B AS B.event_type = 'view' AND B.user_id = A.user_id AND B.category_id = A.category_id AND B.product_id <> A.product_id AND B.price BETWEEN A.price - 50 AND A.price + 50 AND B.real_time BETWEEN A.real_time AND A.real_time + INTERVAL '30' MINUTE,
        C AS C.event_type = 'purchase' AND C.user_id = A.user_id AND C.category_id = A.category_id AND C.real_time BETWEEN A.real_time AND A.real_time + INTERVAL '30' MINUTE,
        Z AS TRUE
)
