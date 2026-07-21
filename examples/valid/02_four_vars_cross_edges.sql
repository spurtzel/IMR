SELECT *
FROM events MATCH_RECOGNIZE (
    ORDER BY ts, id
    MEASURES
        A.id AS a_id,
        B.id AS b_id,
        C.id AS c_id,
        D.id AS d_id
    ONE ROW PER MATCH
    AFTER MATCH SKIP TO NEXT ROW
    PATTERN (A Z*? B Z* C Z*? D)
    DEFINE
        A AS A.kind = 'a',
        B AS B.kind = 'b' AND B.user_id = A.user_id,
        C AS C.kind = 'c' AND C.ts >= B.ts,
        D AS D.kind = 'd' AND D.region <> A.region AND D.score >= C.score,
        Z AS TRUE
)
