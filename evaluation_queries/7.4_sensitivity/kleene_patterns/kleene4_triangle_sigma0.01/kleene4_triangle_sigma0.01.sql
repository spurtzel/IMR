SELECT *
FROM events MATCH_RECOGNIZE (
    ORDER BY ts
    MEASURES
        A.id AS a_id,
        B.id AS b_id,
        FIRST(C.id) AS c_first_id,
        LAST(C.id) AS c_last_id,
        D.id AS d_id
    ONE ROW PER MATCH
    AFTER MATCH SKIP TO NEXT ROW
    PATTERN (A Z* B Z*? C+ Z* D)
    DEFINE
        A AS A.primary_type = 'A',
        B AS B.primary_type = 'B' AND B.veq = A.veq,
        C AS C.primary_type = 'C',
        D AS D.primary_type = 'D' AND D.vr1 BETWEEN A.vr1 - 5000 AND A.vr1 + 5000 AND D.vr2 BETWEEN B.vr2 - 5000 AND B.vr2 + 5000,
        Z AS TRUE
)
