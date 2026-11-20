#!/usr/bin/env bash
# Fig. 12 on Trino: native MATCH_RECOGNIZE vs. EIMER on 15 queries over three real-world tables, queried after every
# batch of 200 rows. Runs on Trino's in-memory tables.
#   bash reproducibility/run_trino_realworld.sh                                     the small default
#   DATASETS=taxi QUERIES=2,3 RW_N=20000 bash reproducibility/run_trino_realworld.sh   a subset (SCALE=paper: 100,000 rows)
# Results: reproducibility/out/trino_realworld/ (records, summary.md, figs/trino_fig12_realworld.pdf). The exit code is 0 if every check passed. Parameters: reproducibility/README.md.
export EXPERIMENT_CATALOG=memory CATALOG="${CATALOG:-memory}"
case ",${DATASETS:-taxi,chicago,ecommerce}," in *,taxi,*) WITH_TAXI=1 ;; *) WITH_TAXI=0 ;; esac
if [ "${SCALE:-small}" = paper ] && [ "$CATALOG" = memory ] && [ "$WITH_TAXI" = 1 ] && [ -z "${TRINO_MEMORY_CATALOG_GB:-}" ]; then
  export TRINO_MEMORY_CATALOG_GB=16
  [ "${TRINO_HEAP_GB:-0}" -ge 20 ] || export TRINO_HEAP_GB=40
  [ "${TRINO_CONTAINER_GB:-0}" -ge $((TRINO_HEAP_GB + 4)) ] || export TRINO_CONTAINER_GB=$((TRINO_HEAP_GB + 4))
fi
exec "$(dirname "$0")/docker/run_trino_experiment.sh" realworld
