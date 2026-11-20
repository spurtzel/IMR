SELECT *
FROM events_window MATCH_RECOGNIZE (
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
        E.time AS e_time,
        F.id AS f_id,
        F.time AS f_time,
        G.id AS g_id,
        G.time AS g_time,
        H.id AS h_id,
        H.time AS h_time
    ONE ROW PER MATCH
    AFTER MATCH SKIP TO NEXT ROW
    PATTERN (A Z* B Z* C Z* D Z* E Z* F Z* G Z* H)
    DEFINE
        A AS A.primary_type = 'T0',
        B AS B.primary_type = 'T1' AND B.lon >= A.lon - 0.02 AND B.lon <= A.lon + 0.02 AND B.lat >= A.lat - 0.02 AND B.lat <= A.lat + 0.02,
        C AS C.primary_type = 'T2' AND C.lon >= B.lon - 0.02 AND C.lon <= B.lon + 0.02 AND C.lat >= B.lat - 0.02 AND C.lat <= B.lat + 0.02,
        D AS D.primary_type = 'T3' AND D.lon >= C.lon - 0.02 AND D.lon <= C.lon + 0.02 AND D.lat >= C.lat - 0.02 AND D.lat <= C.lat + 0.02,
        E AS E.primary_type = 'T4' AND E.lon >= D.lon - 0.02 AND E.lon <= D.lon + 0.02 AND E.lat >= D.lat - 0.02 AND E.lat <= D.lat + 0.02,
        F AS F.primary_type = 'T0' AND F.lon >= E.lon - 0.02 AND F.lon <= E.lon + 0.02 AND F.lat >= E.lat - 0.02 AND F.lat <= E.lat + 0.02,
        G AS G.primary_type = 'T1' AND G.lon >= F.lon - 0.02 AND G.lon <= F.lon + 0.02 AND G.lat >= F.lat - 0.02 AND G.lat <= F.lat + 0.02,
        H AS H.primary_type = 'T2' AND H.lon >= G.lon - 0.02 AND H.lon <= G.lon + 0.02 AND H.lat >= G.lat - 0.02 AND H.lat <= G.lat + 0.02 AND abs(date_diff('millisecond', A.ts, H.ts)) <= 60,
        Z AS date_diff('millisecond', A.ts, Z.ts) <= 60
);
