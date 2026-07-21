SELECT *
FROM events MATCH_RECOGNIZE (
    ORDER BY ts, id
    MEASURES
        A.id AS a_id,
        B.id AS b_id,
        C.id AS c_id,
        D.id AS d_id,
        E.id AS e_id
    ONE ROW PER MATCH
    AFTER MATCH SKIP TO NEXT ROW
    PATTERN (A Z* B Z* C Z* D Z* E)
    DEFINE
        A AS A.kind = 'a',
        B AS B.kind = 'b' AND B.account_id = A.account_id,
        C AS C.kind = 'c' AND C.ts >= A.ts AND C.amount > B.amount,
        D AS D.kind = 'd' AND D.zone = B.zone AND D.ts >= C.ts,
        E AS E.kind = 'e' AND E.user_id = A.user_id AND E.ts >= D.ts,
        Z AS TRUE
)
