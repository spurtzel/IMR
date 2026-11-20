SELECT *
FROM events MATCH_RECOGNIZE (
    ORDER BY time
    MEASURES
        COUNT(B.id) AS b_count,
        FIRST(B.id) AS b_first_id,
        LAST(B.id) AS b_last_id,
        C.id AS c_id,
        C.time AS c_time
    ONE ROW PER MATCH
    AFTER MATCH SKIP TO NEXT ROW
    PATTERN (B+ Z* C)
    DEFINE
        B AS B.cbd_congestion_fee > 0 AND B.total_amount <= 80,
        C AS C.cbd_congestion_fee > 0 AND C.total_amount > 80,
        Z AS TRUE
)
