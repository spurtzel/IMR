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
        E.time AS e_time,
        F.id AS f_id,
        F.time AS f_time,
        G.id AS g_id,
        G.time AS g_time,
        H.id AS h_id,
        H.time AS h_time,
        I.id AS i_id,
        I.time AS i_time,
        J.id AS j_id,
        J.time AS j_time,
        K.id AS k_id,
        K.time AS k_time,
        L.id AS l_id,
        L.time AS l_time
    ONE ROW PER MATCH
    AFTER MATCH SKIP TO NEXT ROW
    PATTERN (A Z* B Z* C Z* D Z* E Z* F Z* G Z* H Z* I Z* J Z* K Z* L)
    DEFINE
        A AS A.primary_type = 'T0',
        B AS B.primary_type = 'T1' AND B.lon BETWEEN A.lon - 0.02 AND A.lon + 0.02 AND B.lat BETWEEN A.lat - 0.02 AND A.lat + 0.02,
        C AS C.primary_type = 'T2' AND C.lon BETWEEN A.lon - 0.02 AND A.lon + 0.02 AND C.lat BETWEEN A.lat - 0.02 AND A.lat + 0.02,
        D AS D.primary_type = 'T3' AND D.lon BETWEEN A.lon - 0.02 AND A.lon + 0.02 AND D.lat BETWEEN A.lat - 0.02 AND A.lat + 0.02,
        E AS E.primary_type = 'T4' AND E.lon BETWEEN A.lon - 0.02 AND A.lon + 0.02 AND E.lat BETWEEN A.lat - 0.02 AND A.lat + 0.02,
        F AS F.primary_type = 'T5' AND F.lon BETWEEN A.lon - 0.02 AND A.lon + 0.02 AND F.lat BETWEEN A.lat - 0.02 AND A.lat + 0.02,
        G AS G.primary_type = 'T6' AND G.lon BETWEEN A.lon - 0.02 AND A.lon + 0.02 AND G.lat BETWEEN A.lat - 0.02 AND A.lat + 0.02,
        H AS H.primary_type = 'T7' AND H.lon BETWEEN A.lon - 0.02 AND A.lon + 0.02 AND H.lat BETWEEN A.lat - 0.02 AND A.lat + 0.02,
        I AS I.primary_type = 'T8' AND I.lon BETWEEN A.lon - 0.02 AND A.lon + 0.02 AND I.lat BETWEEN A.lat - 0.02 AND A.lat + 0.02,
        J AS J.primary_type = 'T9' AND J.lon BETWEEN A.lon - 0.02 AND A.lon + 0.02 AND J.lat BETWEEN A.lat - 0.02 AND A.lat + 0.02,
        K AS K.primary_type = 'T10' AND K.lon BETWEEN A.lon - 0.02 AND A.lon + 0.02 AND K.lat BETWEEN A.lat - 0.02 AND A.lat + 0.02,
        L AS L.primary_type = 'T11' AND L.lon BETWEEN A.lon - 0.02 AND A.lon + 0.02 AND L.lat BETWEEN A.lat - 0.02 AND A.lat + 0.02,
        Z AS TRUE
)
