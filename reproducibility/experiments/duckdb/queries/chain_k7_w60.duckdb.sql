WITH RECURSIVE 
  automaton("from", "to", transition, penalty, start, finished, all_rows) AS MATERIALIZED (
    SELECT *
    FROM   ((VALUES (0, 1, 1, 0, TRUE, FALSE, TRUE),
                    (11, 12, 2, 0, FALSE, FALSE, TRUE),
                    (9, 11, 4, 1, FALSE, FALSE, TRUE),
                    (10, 11, 4, 1, FALSE, FALSE, TRUE),
                    (9, 10, 2, 0, FALSE, FALSE, TRUE),
                    (7, 9, 8, 1, FALSE, FALSE, TRUE),
                    (8, 9, 8, 1, FALSE, FALSE, TRUE),
                    (7, 8, 2, 0, FALSE, FALSE, TRUE),
                    (5, 7, 16, 1, FALSE, FALSE, TRUE),
                    (6, 7, 16, 1, FALSE, FALSE, TRUE),
                    (5, 6, 2, 0, FALSE, FALSE, TRUE),
                    (3, 5, 32, 1, FALSE, FALSE, TRUE),
                    (4, 5, 32, 1, FALSE, FALSE, TRUE),
                    (3, 4, 2, 0, FALSE, FALSE, TRUE),
                    (1, 3, 64, 1, FALSE, FALSE, TRUE),
                    (2, 3, 64, 1, FALSE, FALSE, TRUE),
                    (1, 2, 2, 0, FALSE, FALSE, TRUE),
                    (2, 2, 2, 0, FALSE, FALSE, TRUE),
                    (4, 4, 2, 0, FALSE, FALSE, TRUE),
                    (6, 6, 2, 0, FALSE, FALSE, TRUE),
                    (8, 8, 2, 0, FALSE, FALSE, TRUE),
                    (10, 10, 2, 0, FALSE, FALSE, TRUE),
                    (12, 12, 2, 0, FALSE, FALSE, TRUE),
                    (11, 13, 128, 2, FALSE, TRUE, TRUE),
                    (12, 13, 128, 2, FALSE, TRUE, TRUE)))
  ),
  prep AS MATERIALIZED (
    SELECT t.*,
           row_number() OVER win1 AS row_id
    FROM   events_window AS t
    WINDOW win1 AS (ORDER BY t.time)
  ),
  bfs AS MATERIALIZED (
    SELECT a.finished,
           p.row_id AS start,
           p.row_id,
           a."to" AS cur,
           CAST(a.penalty AS uhugeint) AS path,
           1 AS length,
           CASE WHEN (a.transition & 1) > 0
                THEN p.id
           END AS a_id,
           CASE WHEN (a.transition & 1) > 0
                THEN p.id
           END AS id_1,
           CASE WHEN (a.transition & 1) > 0
                THEN p.time
           END AS a_time,
           CASE WHEN (a.transition & 1) > 0
                THEN p.time
           END AS time_2,
           CASE WHEN (a.transition & 64) > 0
                THEN p.id
           END AS b_id,
           CASE WHEN (a.transition & 64) > 0
                THEN p.id
           END AS id_3,
           CASE WHEN (a.transition & 64) > 0
                THEN p.time
           END AS b_time,
           CASE WHEN (a.transition & 64) > 0
                THEN p.time
           END AS time_4,
           CASE WHEN (a.transition & 32) > 0
                THEN p.id
           END AS c_id,
           CASE WHEN (a.transition & 32) > 0
                THEN p.id
           END AS id_5,
           CASE WHEN (a.transition & 32) > 0
                THEN p.time
           END AS c_time,
           CASE WHEN (a.transition & 32) > 0
                THEN p.time
           END AS time_6,
           CASE WHEN (a.transition & 16) > 0
                THEN p.id
           END AS d_id,
           CASE WHEN (a.transition & 16) > 0
                THEN p.id
           END AS id_7,
           CASE WHEN (a.transition & 16) > 0
                THEN p.time
           END AS d_time,
           CASE WHEN (a.transition & 16) > 0
                THEN p.time
           END AS time_8,
           CASE WHEN (a.transition & 8) > 0
                THEN p.id
           END AS e_id,
           CASE WHEN (a.transition & 8) > 0
                THEN p.id
           END AS id_9,
           CASE WHEN (a.transition & 8) > 0
                THEN p.time
           END AS e_time,
           CASE WHEN (a.transition & 8) > 0
                THEN p.time
           END AS time_10,
           CASE WHEN (a.transition & 4) > 0
                THEN p.id
           END AS f_id,
           CASE WHEN (a.transition & 4) > 0
                THEN p.id
           END AS id_11,
           CASE WHEN (a.transition & 4) > 0
                THEN p.time
           END AS f_time,
           CASE WHEN (a.transition & 4) > 0
                THEN p.time
           END AS time_12,
           CASE WHEN (a.transition & 128) > 0
                THEN p.id
           END AS g_id,
           CASE WHEN (a.transition & 128) > 0
                THEN p.id
           END AS id_13,
           CASE WHEN (a.transition & 128) > 0
                THEN p.time
           END AS g_time,
           CASE WHEN (a.transition & 128) > 0
                THEN p.time
           END AS time_14,
           CASE WHEN (a.transition & 1) > 0
                THEN p.primary_type
           END AS primary_type_15,
           CASE WHEN (a.transition & 64) > 0
                THEN p.primary_type
           END AS primary_type_16,
           CASE WHEN (a.transition & 64) > 0
                THEN p.lon
           END AS lon_17,
           CASE WHEN (a.transition & 1) > 0
                THEN p.lon
           END AS lon_18,
           CASE WHEN (a.transition & 64) > 0
                THEN p.lon
           END AS lon_19,
           CASE WHEN (a.transition & 1) > 0
                THEN p.lon
           END AS lon_20,
           CASE WHEN (a.transition & 64) > 0
                THEN p.lat
           END AS lat_21,
           CASE WHEN (a.transition & 1) > 0
                THEN p.lat
           END AS lat_22,
           CASE WHEN (a.transition & 64) > 0
                THEN p.lat
           END AS lat_23,
           CASE WHEN (a.transition & 1) > 0
                THEN p.lat
           END AS lat_24,
           CASE WHEN (a.transition & 32) > 0
                THEN p.primary_type
           END AS primary_type_25,
           CASE WHEN (a.transition & 32) > 0
                THEN p.lon
           END AS lon_26,
           CASE WHEN (a.transition & 64) > 0
                THEN p.lon
           END AS lon_27,
           CASE WHEN (a.transition & 32) > 0
                THEN p.lon
           END AS lon_28,
           CASE WHEN (a.transition & 64) > 0
                THEN p.lon
           END AS lon_29,
           CASE WHEN (a.transition & 32) > 0
                THEN p.lat
           END AS lat_30,
           CASE WHEN (a.transition & 64) > 0
                THEN p.lat
           END AS lat_31,
           CASE WHEN (a.transition & 32) > 0
                THEN p.lat
           END AS lat_32,
           CASE WHEN (a.transition & 64) > 0
                THEN p.lat
           END AS lat_33,
           CASE WHEN (a.transition & 16) > 0
                THEN p.primary_type
           END AS primary_type_34,
           CASE WHEN (a.transition & 16) > 0
                THEN p.lon
           END AS lon_35,
           CASE WHEN (a.transition & 32) > 0
                THEN p.lon
           END AS lon_36,
           CASE WHEN (a.transition & 16) > 0
                THEN p.lon
           END AS lon_37,
           CASE WHEN (a.transition & 32) > 0
                THEN p.lon
           END AS lon_38,
           CASE WHEN (a.transition & 16) > 0
                THEN p.lat
           END AS lat_39,
           CASE WHEN (a.transition & 32) > 0
                THEN p.lat
           END AS lat_40,
           CASE WHEN (a.transition & 16) > 0
                THEN p.lat
           END AS lat_41,
           CASE WHEN (a.transition & 32) > 0
                THEN p.lat
           END AS lat_42,
           CASE WHEN (a.transition & 8) > 0
                THEN p.primary_type
           END AS primary_type_43,
           CASE WHEN (a.transition & 8) > 0
                THEN p.lon
           END AS lon_44,
           CASE WHEN (a.transition & 16) > 0
                THEN p.lon
           END AS lon_45,
           CASE WHEN (a.transition & 8) > 0
                THEN p.lon
           END AS lon_46,
           CASE WHEN (a.transition & 16) > 0
                THEN p.lon
           END AS lon_47,
           CASE WHEN (a.transition & 8) > 0
                THEN p.lat
           END AS lat_48,
           CASE WHEN (a.transition & 16) > 0
                THEN p.lat
           END AS lat_49,
           CASE WHEN (a.transition & 8) > 0
                THEN p.lat
           END AS lat_50,
           CASE WHEN (a.transition & 16) > 0
                THEN p.lat
           END AS lat_51,
           CASE WHEN (a.transition & 4) > 0
                THEN p.primary_type
           END AS primary_type_52,
           CASE WHEN (a.transition & 4) > 0
                THEN p.lon
           END AS lon_53,
           CASE WHEN (a.transition & 8) > 0
                THEN p.lon
           END AS lon_54,
           CASE WHEN (a.transition & 4) > 0
                THEN p.lon
           END AS lon_55,
           CASE WHEN (a.transition & 8) > 0
                THEN p.lon
           END AS lon_56,
           CASE WHEN (a.transition & 4) > 0
                THEN p.lat
           END AS lat_57,
           CASE WHEN (a.transition & 8) > 0
                THEN p.lat
           END AS lat_58,
           CASE WHEN (a.transition & 4) > 0
                THEN p.lat
           END AS lat_59,
           CASE WHEN (a.transition & 8) > 0
                THEN p.lat
           END AS lat_60,
           CASE WHEN (a.transition & 128) > 0
                THEN p.primary_type
           END AS primary_type_61,
           CASE WHEN (a.transition & 128) > 0
                THEN p.lon
           END AS lon_62,
           CASE WHEN (a.transition & 4) > 0
                THEN p.lon
           END AS lon_63,
           CASE WHEN (a.transition & 128) > 0
                THEN p.lon
           END AS lon_64,
           CASE WHEN (a.transition & 4) > 0
                THEN p.lon
           END AS lon_65,
           CASE WHEN (a.transition & 128) > 0
                THEN p.lat
           END AS lat_66,
           CASE WHEN (a.transition & 4) > 0
                THEN p.lat
           END AS lat_67,
           CASE WHEN (a.transition & 128) > 0
                THEN p.lat
           END AS lat_68,
           CASE WHEN (a.transition & 4) > 0
                THEN p.lat
           END AS lat_69,
           CASE WHEN (a.transition & 1) > 0
                THEN p.ts
           END AS ts_70,
           CASE WHEN (a.transition & 128) > 0
                THEN p.ts
           END AS ts_71,
           CASE WHEN (a.transition & 1) > 0
                THEN p.ts
           END AS ts_72,
           CASE WHEN (a.transition & 2) > 0
                THEN p.ts
           END AS ts_73
    FROM   prep AS p, automaton AS a
    WHERE  a.start
      AND  CASE a.transition
           WHEN 1
           THEN CASE WHEN (a.transition & 1) > 0
                     THEN p.primary_type
                END = 'T0'
           WHEN 64
           THEN CASE WHEN (a.transition & 64) > 0
                     THEN p.primary_type
                END = 'T1'
           AND  CASE WHEN (a.transition & 64) > 0
                     THEN p.lon
                END >= (CASE WHEN (a.transition & 1) > 0
                             THEN p.lon
                        END - 0.02)
           AND  CASE WHEN (a.transition & 64) > 0
                     THEN p.lon
                END <= (CASE WHEN (a.transition & 1) > 0
                             THEN p.lon
                        END + 0.02)
           AND  CASE WHEN (a.transition & 64) > 0
                     THEN p.lat
                END >= (CASE WHEN (a.transition & 1) > 0
                             THEN p.lat
                        END - 0.02)
           AND  CASE WHEN (a.transition & 64) > 0
                     THEN p.lat
                END <= (CASE WHEN (a.transition & 1) > 0
                             THEN p.lat
                        END + 0.02)
           WHEN 32
           THEN CASE WHEN (a.transition & 32) > 0
                     THEN p.primary_type
                END = 'T2'
           AND  CASE WHEN (a.transition & 32) > 0
                     THEN p.lon
                END >= (CASE WHEN (a.transition & 64) > 0
                             THEN p.lon
                        END - 0.02)
           AND  CASE WHEN (a.transition & 32) > 0
                     THEN p.lon
                END <= (CASE WHEN (a.transition & 64) > 0
                             THEN p.lon
                        END + 0.02)
           AND  CASE WHEN (a.transition & 32) > 0
                     THEN p.lat
                END >= (CASE WHEN (a.transition & 64) > 0
                             THEN p.lat
                        END - 0.02)
           AND  CASE WHEN (a.transition & 32) > 0
                     THEN p.lat
                END <= (CASE WHEN (a.transition & 64) > 0
                             THEN p.lat
                        END + 0.02)
           WHEN 16
           THEN CASE WHEN (a.transition & 16) > 0
                     THEN p.primary_type
                END = 'T3'
           AND  CASE WHEN (a.transition & 16) > 0
                     THEN p.lon
                END >= (CASE WHEN (a.transition & 32) > 0
                             THEN p.lon
                        END - 0.02)
           AND  CASE WHEN (a.transition & 16) > 0
                     THEN p.lon
                END <= (CASE WHEN (a.transition & 32) > 0
                             THEN p.lon
                        END + 0.02)
           AND  CASE WHEN (a.transition & 16) > 0
                     THEN p.lat
                END >= (CASE WHEN (a.transition & 32) > 0
                             THEN p.lat
                        END - 0.02)
           AND  CASE WHEN (a.transition & 16) > 0
                     THEN p.lat
                END <= (CASE WHEN (a.transition & 32) > 0
                             THEN p.lat
                        END + 0.02)
           WHEN 8
           THEN CASE WHEN (a.transition & 8) > 0
                     THEN p.primary_type
                END = 'T4'
           AND  CASE WHEN (a.transition & 8) > 0
                     THEN p.lon
                END >= (CASE WHEN (a.transition & 16) > 0
                             THEN p.lon
                        END - 0.02)
           AND  CASE WHEN (a.transition & 8) > 0
                     THEN p.lon
                END <= (CASE WHEN (a.transition & 16) > 0
                             THEN p.lon
                        END + 0.02)
           AND  CASE WHEN (a.transition & 8) > 0
                     THEN p.lat
                END >= (CASE WHEN (a.transition & 16) > 0
                             THEN p.lat
                        END - 0.02)
           AND  CASE WHEN (a.transition & 8) > 0
                     THEN p.lat
                END <= (CASE WHEN (a.transition & 16) > 0
                             THEN p.lat
                        END + 0.02)
           WHEN 4
           THEN CASE WHEN (a.transition & 4) > 0
                     THEN p.primary_type
                END = 'T0'
           AND  CASE WHEN (a.transition & 4) > 0
                     THEN p.lon
                END >= (CASE WHEN (a.transition & 8) > 0
                             THEN p.lon
                        END - 0.02)
           AND  CASE WHEN (a.transition & 4) > 0
                     THEN p.lon
                END <= (CASE WHEN (a.transition & 8) > 0
                             THEN p.lon
                        END + 0.02)
           AND  CASE WHEN (a.transition & 4) > 0
                     THEN p.lat
                END >= (CASE WHEN (a.transition & 8) > 0
                             THEN p.lat
                        END - 0.02)
           AND  CASE WHEN (a.transition & 4) > 0
                     THEN p.lat
                END <= (CASE WHEN (a.transition & 8) > 0
                             THEN p.lat
                        END + 0.02)
           WHEN 128
           THEN CASE WHEN (a.transition & 128) > 0
                     THEN p.primary_type
                END = 'T1'
           AND  CASE WHEN (a.transition & 128) > 0
                     THEN p.lon
                END >= (CASE WHEN (a.transition & 4) > 0
                             THEN p.lon
                        END - 0.02)
           AND  CASE WHEN (a.transition & 128) > 0
                     THEN p.lon
                END <= (CASE WHEN (a.transition & 4) > 0
                             THEN p.lon
                        END + 0.02)
           AND  CASE WHEN (a.transition & 128) > 0
                     THEN p.lat
                END >= (CASE WHEN (a.transition & 4) > 0
                             THEN p.lat
                        END - 0.02)
           AND  CASE WHEN (a.transition & 128) > 0
                     THEN p.lat
                END <= (CASE WHEN (a.transition & 4) > 0
                             THEN p.lat
                        END + 0.02)
           AND  abs(date_diff('millisecond',
                              CASE WHEN (a.transition & 1) > 0
                                   THEN p.ts
                              END,
                              CASE WHEN (a.transition & 128) > 0
                                   THEN p.ts
                              END)) <= 60
           WHEN 2
           THEN date_diff('millisecond',
                          CASE WHEN (a.transition & 1) > 0
                               THEN p.ts
                          END,
                          CASE WHEN (a.transition & 2) > 0
                               THEN p.ts
                          END) <= 60
           END

    UNION ALL

    SELECT a.finished,
           b.start,
           p.row_id,
           a."to" AS cur,
           (b.path << CAST(2 AS uhugeint)) + CAST(a.penalty AS uhugeint) AS path,
           b.length + 1 AS length,
           CASE WHEN (a.transition & 1) > 0
                THEN p.id
                ELSE b.id_1
           END AS a_id,
           CASE WHEN (a.transition & 1) > 0
                THEN p.id
                ELSE b.id_1
           END AS id_1,
           CASE WHEN (a.transition & 1) > 0
                THEN p.time
                ELSE b.time_2
           END AS a_time,
           CASE WHEN (a.transition & 1) > 0
                THEN p.time
                ELSE b.time_2
           END AS time_2,
           CASE WHEN (a.transition & 64) > 0
                THEN p.id
                ELSE b.id_3
           END AS b_id,
           CASE WHEN (a.transition & 64) > 0
                THEN p.id
                ELSE b.id_3
           END AS id_3,
           CASE WHEN (a.transition & 64) > 0
                THEN p.time
                ELSE b.time_4
           END AS b_time,
           CASE WHEN (a.transition & 64) > 0
                THEN p.time
                ELSE b.time_4
           END AS time_4,
           CASE WHEN (a.transition & 32) > 0
                THEN p.id
                ELSE b.id_5
           END AS c_id,
           CASE WHEN (a.transition & 32) > 0
                THEN p.id
                ELSE b.id_5
           END AS id_5,
           CASE WHEN (a.transition & 32) > 0
                THEN p.time
                ELSE b.time_6
           END AS c_time,
           CASE WHEN (a.transition & 32) > 0
                THEN p.time
                ELSE b.time_6
           END AS time_6,
           CASE WHEN (a.transition & 16) > 0
                THEN p.id
                ELSE b.id_7
           END AS d_id,
           CASE WHEN (a.transition & 16) > 0
                THEN p.id
                ELSE b.id_7
           END AS id_7,
           CASE WHEN (a.transition & 16) > 0
                THEN p.time
                ELSE b.time_8
           END AS d_time,
           CASE WHEN (a.transition & 16) > 0
                THEN p.time
                ELSE b.time_8
           END AS time_8,
           CASE WHEN (a.transition & 8) > 0
                THEN p.id
                ELSE b.id_9
           END AS e_id,
           CASE WHEN (a.transition & 8) > 0
                THEN p.id
                ELSE b.id_9
           END AS id_9,
           CASE WHEN (a.transition & 8) > 0
                THEN p.time
                ELSE b.time_10
           END AS e_time,
           CASE WHEN (a.transition & 8) > 0
                THEN p.time
                ELSE b.time_10
           END AS time_10,
           CASE WHEN (a.transition & 4) > 0
                THEN p.id
                ELSE b.id_11
           END AS f_id,
           CASE WHEN (a.transition & 4) > 0
                THEN p.id
                ELSE b.id_11
           END AS id_11,
           CASE WHEN (a.transition & 4) > 0
                THEN p.time
                ELSE b.time_12
           END AS f_time,
           CASE WHEN (a.transition & 4) > 0
                THEN p.time
                ELSE b.time_12
           END AS time_12,
           CASE WHEN (a.transition & 128) > 0
                THEN p.id
                ELSE b.id_13
           END AS g_id,
           CASE WHEN (a.transition & 128) > 0
                THEN p.id
                ELSE b.id_13
           END AS id_13,
           CASE WHEN (a.transition & 128) > 0
                THEN p.time
                ELSE b.time_14
           END AS g_time,
           CASE WHEN (a.transition & 128) > 0
                THEN p.time
                ELSE b.time_14
           END AS time_14,
           CASE WHEN (a.transition & 1) > 0
                THEN p.primary_type
                ELSE b.primary_type_15
           END AS primary_type_15,
           CASE WHEN (a.transition & 64) > 0
                THEN p.primary_type
                ELSE b.primary_type_16
           END AS primary_type_16,
           CASE WHEN (a.transition & 64) > 0
                THEN p.lon
                ELSE b.lon_17
           END AS lon_17,
           CASE WHEN (a.transition & 1) > 0
                THEN p.lon
                ELSE b.lon_18
           END AS lon_18,
           CASE WHEN (a.transition & 64) > 0
                THEN p.lon
                ELSE b.lon_19
           END AS lon_19,
           CASE WHEN (a.transition & 1) > 0
                THEN p.lon
                ELSE b.lon_20
           END AS lon_20,
           CASE WHEN (a.transition & 64) > 0
                THEN p.lat
                ELSE b.lat_21
           END AS lat_21,
           CASE WHEN (a.transition & 1) > 0
                THEN p.lat
                ELSE b.lat_22
           END AS lat_22,
           CASE WHEN (a.transition & 64) > 0
                THEN p.lat
                ELSE b.lat_23
           END AS lat_23,
           CASE WHEN (a.transition & 1) > 0
                THEN p.lat
                ELSE b.lat_24
           END AS lat_24,
           CASE WHEN (a.transition & 32) > 0
                THEN p.primary_type
                ELSE b.primary_type_25
           END AS primary_type_25,
           CASE WHEN (a.transition & 32) > 0
                THEN p.lon
                ELSE b.lon_26
           END AS lon_26,
           CASE WHEN (a.transition & 64) > 0
                THEN p.lon
                ELSE b.lon_27
           END AS lon_27,
           CASE WHEN (a.transition & 32) > 0
                THEN p.lon
                ELSE b.lon_28
           END AS lon_28,
           CASE WHEN (a.transition & 64) > 0
                THEN p.lon
                ELSE b.lon_29
           END AS lon_29,
           CASE WHEN (a.transition & 32) > 0
                THEN p.lat
                ELSE b.lat_30
           END AS lat_30,
           CASE WHEN (a.transition & 64) > 0
                THEN p.lat
                ELSE b.lat_31
           END AS lat_31,
           CASE WHEN (a.transition & 32) > 0
                THEN p.lat
                ELSE b.lat_32
           END AS lat_32,
           CASE WHEN (a.transition & 64) > 0
                THEN p.lat
                ELSE b.lat_33
           END AS lat_33,
           CASE WHEN (a.transition & 16) > 0
                THEN p.primary_type
                ELSE b.primary_type_34
           END AS primary_type_34,
           CASE WHEN (a.transition & 16) > 0
                THEN p.lon
                ELSE b.lon_35
           END AS lon_35,
           CASE WHEN (a.transition & 32) > 0
                THEN p.lon
                ELSE b.lon_36
           END AS lon_36,
           CASE WHEN (a.transition & 16) > 0
                THEN p.lon
                ELSE b.lon_37
           END AS lon_37,
           CASE WHEN (a.transition & 32) > 0
                THEN p.lon
                ELSE b.lon_38
           END AS lon_38,
           CASE WHEN (a.transition & 16) > 0
                THEN p.lat
                ELSE b.lat_39
           END AS lat_39,
           CASE WHEN (a.transition & 32) > 0
                THEN p.lat
                ELSE b.lat_40
           END AS lat_40,
           CASE WHEN (a.transition & 16) > 0
                THEN p.lat
                ELSE b.lat_41
           END AS lat_41,
           CASE WHEN (a.transition & 32) > 0
                THEN p.lat
                ELSE b.lat_42
           END AS lat_42,
           CASE WHEN (a.transition & 8) > 0
                THEN p.primary_type
                ELSE b.primary_type_43
           END AS primary_type_43,
           CASE WHEN (a.transition & 8) > 0
                THEN p.lon
                ELSE b.lon_44
           END AS lon_44,
           CASE WHEN (a.transition & 16) > 0
                THEN p.lon
                ELSE b.lon_45
           END AS lon_45,
           CASE WHEN (a.transition & 8) > 0
                THEN p.lon
                ELSE b.lon_46
           END AS lon_46,
           CASE WHEN (a.transition & 16) > 0
                THEN p.lon
                ELSE b.lon_47
           END AS lon_47,
           CASE WHEN (a.transition & 8) > 0
                THEN p.lat
                ELSE b.lat_48
           END AS lat_48,
           CASE WHEN (a.transition & 16) > 0
                THEN p.lat
                ELSE b.lat_49
           END AS lat_49,
           CASE WHEN (a.transition & 8) > 0
                THEN p.lat
                ELSE b.lat_50
           END AS lat_50,
           CASE WHEN (a.transition & 16) > 0
                THEN p.lat
                ELSE b.lat_51
           END AS lat_51,
           CASE WHEN (a.transition & 4) > 0
                THEN p.primary_type
                ELSE b.primary_type_52
           END AS primary_type_52,
           CASE WHEN (a.transition & 4) > 0
                THEN p.lon
                ELSE b.lon_53
           END AS lon_53,
           CASE WHEN (a.transition & 8) > 0
                THEN p.lon
                ELSE b.lon_54
           END AS lon_54,
           CASE WHEN (a.transition & 4) > 0
                THEN p.lon
                ELSE b.lon_55
           END AS lon_55,
           CASE WHEN (a.transition & 8) > 0
                THEN p.lon
                ELSE b.lon_56
           END AS lon_56,
           CASE WHEN (a.transition & 4) > 0
                THEN p.lat
                ELSE b.lat_57
           END AS lat_57,
           CASE WHEN (a.transition & 8) > 0
                THEN p.lat
                ELSE b.lat_58
           END AS lat_58,
           CASE WHEN (a.transition & 4) > 0
                THEN p.lat
                ELSE b.lat_59
           END AS lat_59,
           CASE WHEN (a.transition & 8) > 0
                THEN p.lat
                ELSE b.lat_60
           END AS lat_60,
           CASE WHEN (a.transition & 128) > 0
                THEN p.primary_type
                ELSE b.primary_type_61
           END AS primary_type_61,
           CASE WHEN (a.transition & 128) > 0
                THEN p.lon
                ELSE b.lon_62
           END AS lon_62,
           CASE WHEN (a.transition & 4) > 0
                THEN p.lon
                ELSE b.lon_63
           END AS lon_63,
           CASE WHEN (a.transition & 128) > 0
                THEN p.lon
                ELSE b.lon_64
           END AS lon_64,
           CASE WHEN (a.transition & 4) > 0
                THEN p.lon
                ELSE b.lon_65
           END AS lon_65,
           CASE WHEN (a.transition & 128) > 0
                THEN p.lat
                ELSE b.lat_66
           END AS lat_66,
           CASE WHEN (a.transition & 4) > 0
                THEN p.lat
                ELSE b.lat_67
           END AS lat_67,
           CASE WHEN (a.transition & 128) > 0
                THEN p.lat
                ELSE b.lat_68
           END AS lat_68,
           CASE WHEN (a.transition & 4) > 0
                THEN p.lat
                ELSE b.lat_69
           END AS lat_69,
           CASE WHEN (a.transition & 1) > 0
                THEN p.ts
                ELSE b.ts_70
           END AS ts_70,
           CASE WHEN (a.transition & 128) > 0
                THEN p.ts
                ELSE b.ts_71
           END AS ts_71,
           CASE WHEN (a.transition & 1) > 0
                THEN p.ts
                ELSE b.ts_72
           END AS ts_72,
           CASE WHEN (a.transition & 2) > 0
                THEN p.ts
                ELSE b.ts_73
           END AS ts_73
    FROM   bfs AS b, prep AS p, automaton AS a
    WHERE  a."from" = b.cur
      AND  (b.row_id + 1) = p.row_id
      AND  CASE a.transition
           WHEN 1
           THEN CASE WHEN (a.transition & 1) > 0
                     THEN p.primary_type
                     ELSE b.primary_type_15
                END = 'T0'
           WHEN 64
           THEN CASE WHEN (a.transition & 64) > 0
                     THEN p.primary_type
                     ELSE b.primary_type_16
                END = 'T1'
           AND  CASE WHEN (a.transition & 64) > 0
                     THEN p.lon
                     ELSE b.lon_17
                END >= (CASE WHEN (a.transition & 1) > 0
                             THEN p.lon
                             ELSE b.lon_18
                        END - 0.02)
           AND  CASE WHEN (a.transition & 64) > 0
                     THEN p.lon
                     ELSE b.lon_19
                END <= (CASE WHEN (a.transition & 1) > 0
                             THEN p.lon
                             ELSE b.lon_20
                        END + 0.02)
           AND  CASE WHEN (a.transition & 64) > 0
                     THEN p.lat
                     ELSE b.lat_21
                END >= (CASE WHEN (a.transition & 1) > 0
                             THEN p.lat
                             ELSE b.lat_22
                        END - 0.02)
           AND  CASE WHEN (a.transition & 64) > 0
                     THEN p.lat
                     ELSE b.lat_23
                END <= (CASE WHEN (a.transition & 1) > 0
                             THEN p.lat
                             ELSE b.lat_24
                        END + 0.02)
           WHEN 32
           THEN CASE WHEN (a.transition & 32) > 0
                     THEN p.primary_type
                     ELSE b.primary_type_25
                END = 'T2'
           AND  CASE WHEN (a.transition & 32) > 0
                     THEN p.lon
                     ELSE b.lon_26
                END >= (CASE WHEN (a.transition & 64) > 0
                             THEN p.lon
                             ELSE b.lon_27
                        END - 0.02)
           AND  CASE WHEN (a.transition & 32) > 0
                     THEN p.lon
                     ELSE b.lon_28
                END <= (CASE WHEN (a.transition & 64) > 0
                             THEN p.lon
                             ELSE b.lon_29
                        END + 0.02)
           AND  CASE WHEN (a.transition & 32) > 0
                     THEN p.lat
                     ELSE b.lat_30
                END >= (CASE WHEN (a.transition & 64) > 0
                             THEN p.lat
                             ELSE b.lat_31
                        END - 0.02)
           AND  CASE WHEN (a.transition & 32) > 0
                     THEN p.lat
                     ELSE b.lat_32
                END <= (CASE WHEN (a.transition & 64) > 0
                             THEN p.lat
                             ELSE b.lat_33
                        END + 0.02)
           WHEN 16
           THEN CASE WHEN (a.transition & 16) > 0
                     THEN p.primary_type
                     ELSE b.primary_type_34
                END = 'T3'
           AND  CASE WHEN (a.transition & 16) > 0
                     THEN p.lon
                     ELSE b.lon_35
                END >= (CASE WHEN (a.transition & 32) > 0
                             THEN p.lon
                             ELSE b.lon_36
                        END - 0.02)
           AND  CASE WHEN (a.transition & 16) > 0
                     THEN p.lon
                     ELSE b.lon_37
                END <= (CASE WHEN (a.transition & 32) > 0
                             THEN p.lon
                             ELSE b.lon_38
                        END + 0.02)
           AND  CASE WHEN (a.transition & 16) > 0
                     THEN p.lat
                     ELSE b.lat_39
                END >= (CASE WHEN (a.transition & 32) > 0
                             THEN p.lat
                             ELSE b.lat_40
                        END - 0.02)
           AND  CASE WHEN (a.transition & 16) > 0
                     THEN p.lat
                     ELSE b.lat_41
                END <= (CASE WHEN (a.transition & 32) > 0
                             THEN p.lat
                             ELSE b.lat_42
                        END + 0.02)
           WHEN 8
           THEN CASE WHEN (a.transition & 8) > 0
                     THEN p.primary_type
                     ELSE b.primary_type_43
                END = 'T4'
           AND  CASE WHEN (a.transition & 8) > 0
                     THEN p.lon
                     ELSE b.lon_44
                END >= (CASE WHEN (a.transition & 16) > 0
                             THEN p.lon
                             ELSE b.lon_45
                        END - 0.02)
           AND  CASE WHEN (a.transition & 8) > 0
                     THEN p.lon
                     ELSE b.lon_46
                END <= (CASE WHEN (a.transition & 16) > 0
                             THEN p.lon
                             ELSE b.lon_47
                        END + 0.02)
           AND  CASE WHEN (a.transition & 8) > 0
                     THEN p.lat
                     ELSE b.lat_48
                END >= (CASE WHEN (a.transition & 16) > 0
                             THEN p.lat
                             ELSE b.lat_49
                        END - 0.02)
           AND  CASE WHEN (a.transition & 8) > 0
                     THEN p.lat
                     ELSE b.lat_50
                END <= (CASE WHEN (a.transition & 16) > 0
                             THEN p.lat
                             ELSE b.lat_51
                        END + 0.02)
           WHEN 4
           THEN CASE WHEN (a.transition & 4) > 0
                     THEN p.primary_type
                     ELSE b.primary_type_52
                END = 'T0'
           AND  CASE WHEN (a.transition & 4) > 0
                     THEN p.lon
                     ELSE b.lon_53
                END >= (CASE WHEN (a.transition & 8) > 0
                             THEN p.lon
                             ELSE b.lon_54
                        END - 0.02)
           AND  CASE WHEN (a.transition & 4) > 0
                     THEN p.lon
                     ELSE b.lon_55
                END <= (CASE WHEN (a.transition & 8) > 0
                             THEN p.lon
                             ELSE b.lon_56
                        END + 0.02)
           AND  CASE WHEN (a.transition & 4) > 0
                     THEN p.lat
                     ELSE b.lat_57
                END >= (CASE WHEN (a.transition & 8) > 0
                             THEN p.lat
                             ELSE b.lat_58
                        END - 0.02)
           AND  CASE WHEN (a.transition & 4) > 0
                     THEN p.lat
                     ELSE b.lat_59
                END <= (CASE WHEN (a.transition & 8) > 0
                             THEN p.lat
                             ELSE b.lat_60
                        END + 0.02)
           WHEN 128
           THEN CASE WHEN (a.transition & 128) > 0
                     THEN p.primary_type
                     ELSE b.primary_type_61
                END = 'T1'
           AND  CASE WHEN (a.transition & 128) > 0
                     THEN p.lon
                     ELSE b.lon_62
                END >= (CASE WHEN (a.transition & 4) > 0
                             THEN p.lon
                             ELSE b.lon_63
                        END - 0.02)
           AND  CASE WHEN (a.transition & 128) > 0
                     THEN p.lon
                     ELSE b.lon_64
                END <= (CASE WHEN (a.transition & 4) > 0
                             THEN p.lon
                             ELSE b.lon_65
                        END + 0.02)
           AND  CASE WHEN (a.transition & 128) > 0
                     THEN p.lat
                     ELSE b.lat_66
                END >= (CASE WHEN (a.transition & 4) > 0
                             THEN p.lat
                             ELSE b.lat_67
                        END - 0.02)
           AND  CASE WHEN (a.transition & 128) > 0
                     THEN p.lat
                     ELSE b.lat_68
                END <= (CASE WHEN (a.transition & 4) > 0
                             THEN p.lat
                             ELSE b.lat_69
                        END + 0.02)
           AND  abs(date_diff('millisecond',
                              CASE WHEN (a.transition & 1) > 0
                                   THEN p.ts
                                   ELSE b.ts_70
                              END,
                              CASE WHEN (a.transition & 128) > 0
                                   THEN p.ts
                                   ELSE b.ts_71
                              END)) <= 60
           WHEN 2
           THEN date_diff('millisecond',
                          CASE WHEN (a.transition & 1) > 0
                               THEN p.ts
                               ELSE b.ts_72
                          END,
                          CASE WHEN (a.transition & 2) > 0
                               THEN p.ts
                               ELSE b.ts_73
                          END) <= 60
           END
  ),
  matches AS MATERIALIZED (
    SELECT b.start,
           arg_min(b.row_id,
                   b.path << CAST((64 - b.length) * 2 AS uhugeint)) AS "end",
           arg_min(b.path,
                   b.path << CAST((64 - b.length) * 2 AS uhugeint)) AS path,
           arg_min(b.length,
                   b.path << CAST((64 - b.length) * 2 AS uhugeint)) AS length
    FROM   bfs AS b
    WHERE  b.finished
    GROUP BY b.start
  ),
  skipping AS (
    SELECT *
    FROM   matches AS m
  ),
  collect AS (
    SELECT s.start,
           b.row_id,
           s.path,
           s.length,
           a."to" AS cur,
           a.all_rows,
           b.a_id,
           b.a_time,
           b.b_id,
           b.b_time,
           b.c_id,
           b.c_time,
           b.d_id,
           b.d_time,
           b.e_id,
           b.e_time,
           b.f_id,
           b.f_time,
           b.g_id,
           b.g_time
    FROM   skipping AS s, bfs AS b, automaton AS a
    WHERE  s.start = b.start
      AND  s.start = b.row_id
      AND  a.start
      AND  a."to" = b.cur
      AND  s.length >= b.length
      AND  (s.path >> CAST((s.length - b.length) * 2 AS uhugeint)) = b.path
      AND  s.start <> 0

    UNION ALL

    SELECT c.start,
           b.row_id,
           c.path,
           c.length,
           a."to" AS cur,
           a.all_rows,
           b.a_id,
           b.a_time,
           b.b_id,
           b.b_time,
           b.c_id,
           b.c_time,
           b.d_id,
           b.d_time,
           b.e_id,
           b.e_time,
           b.f_id,
           b.f_time,
           b.g_id,
           b.g_time
    FROM   collect AS c, bfs AS b, automaton AS a
    WHERE  c.start = b.start
      AND  (c.row_id + 1) = b.row_id
      AND  a."from" = c.cur
      AND  a."to" = b.cur
      AND  c.length >= b.length
      AND  (c.path >> CAST((c.length - b.length) * 2 AS uhugeint)) = b.path
  ),
  postprocessing AS (
    SELECT p.* EXCLUDE(row_id),
           c.a_id,
           c.a_time,
           c.b_id,
           c.b_time,
           c.c_id,
           c.c_time,
           c.d_id,
           c.d_time,
           c.e_id,
           c.e_time,
           c.f_id,
           c.f_time,
           c.g_id,
           c.g_time
    FROM   (SELECT arg_max(c.a_id, c.row_id) AS a_id,
                   arg_max(c.a_time, c.row_id) AS a_time,
                   arg_max(c.b_id, c.row_id) AS b_id,
                   arg_max(c.b_time, c.row_id) AS b_time,
                   arg_max(c.c_id, c.row_id) AS c_id,
                   arg_max(c.c_time, c.row_id) AS c_time,
                   arg_max(c.d_id, c.row_id) AS d_id,
                   arg_max(c.d_time, c.row_id) AS d_time,
                   arg_max(c.e_id, c.row_id) AS e_id,
                   arg_max(c.e_time, c.row_id) AS e_time,
                   arg_max(c.f_id, c.row_id) AS f_id,
                   arg_max(c.f_time, c.row_id) AS f_time,
                   arg_max(c.g_id, c.row_id) AS g_id,
                   arg_max(c.g_time, c.row_id) AS g_time,
                   max(c.row_id) AS row_id
            FROM   collect AS c
            GROUP BY c.start) AS c,
           prep AS p
    WHERE  p.row_id = c.row_id
  )
SELECT *
FROM   postprocessing;
