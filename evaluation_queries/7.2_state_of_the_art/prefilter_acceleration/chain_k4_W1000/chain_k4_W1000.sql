SELECT *
FROM events MATCH_RECOGNIZE (
    ORDER BY time
    MEASURES
        V0.id AS v0_id,
        V0.time AS v0_time,
        V1.id AS v1_id,
        V1.time AS v1_time,
        V2.id AS v2_id,
        V2.time AS v2_time,
        V3.id AS v3_id,
        V3.time AS v3_time
    ONE ROW PER MATCH
    AFTER MATCH SKIP TO NEXT ROW
    PATTERN (V0 Z* V1 Z* V2 Z* V3)
    DEFINE
        V0 AS V0.primary_type = 'T0',
        V1 AS V1.primary_type = 'T1' AND V1.lon BETWEEN V0.lon - 0.05 AND V0.lon + 0.05 AND V1.lat BETWEEN V0.lat - 0.02 AND V0.lat + 0.02,
        V2 AS V2.primary_type = 'T2' AND V2.lon BETWEEN V1.lon - 0.05 AND V1.lon + 0.05 AND V2.lat BETWEEN V1.lat - 0.02 AND V1.lat + 0.02,
        V3 AS V3.primary_type = 'T3' AND V3.lon BETWEEN V2.lon - 0.05 AND V2.lon + 0.05 AND V3.lat BETWEEN V2.lat - 0.02 AND V2.lat + 0.02 AND V3.ts BETWEEN V0.ts AND V0.ts+INTERVAL '1.0' SECOND,
        Z AS TRUE
)
