SELECT *
FROM events MATCH_RECOGNIZE (
    ORDER BY ts
    MEASURES
        A.id AS a_id,
        B.id AS b_id,
        C.id AS c_id
    ONE ROW PER MATCH
    AFTER MATCH SKIP TO NEXT ROW
    PATTERN (A Z* B Z* C)
    DEFINE
        A AS A.kind = 'a',
        B AS B.kind = 'b',
        C AS C.kind = 'c' AND C.user_id = A.user_id,
        Z AS TRUE
)
