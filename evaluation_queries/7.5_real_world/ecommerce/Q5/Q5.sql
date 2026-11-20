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
        A AS A.event_type = 'view',
        B AS B.event_type = 'view' AND B.user_id = A.user_id AND B.product_id = A.product_id AND B.real_time BETWEEN A.real_time AND A.real_time + INTERVAL '60' MINUTE,
        C AS C.event_type = 'cart' AND C.user_id = B.user_id AND C.product_id = B.product_id AND C.real_time BETWEEN B.real_time AND B.real_time + INTERVAL '60' MINUTE,
        D AS D.event_type = 'view' AND D.user_id = C.user_id AND D.product_id = C.product_id AND D.real_time BETWEEN C.real_time AND C.real_time + INTERVAL '60' MINUTE,
        E AS E.event_type = 'purchase' AND E.user_id = D.user_id AND E.product_id = D.product_id AND E.real_time BETWEEN D.real_time AND D.real_time + INTERVAL '60' MINUTE AND E.real_time BETWEEN A.real_time AND A.real_time + INTERVAL '2' HOUR,
        Z AS TRUE
)
