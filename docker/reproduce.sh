#!/usr/bin/env bash
# The demo suite, run INSIDE the pipeline container (or on any host with a Trino
# reachable via TRINO_SERVER). Six phases; every phase writes its artifacts under
# $OUT ( default /eimer/docker/out ) and the summarizer decides the final verdict.
set -u
cd "$(dirname "$0")/.."
OUT="${OUT:-$PWD/docker/out}"
mkdir -p "$OUT"
: > "$OUT/phase_status.txt"

# Trino connection: the docker image presets TRINO_*; on a bare host these default
# to a local Trino.
export TRINO_SERVER="${TRINO_SERVER:-localhost:8080}"
export TRINO_CATALOG="${TRINO_CATALOG:-memory}"
export TRINO_SCHEMA="${TRINO_SCHEMA:-default}"
export TRINO_USER="${TRINO_USER:-eimer}"
echo "Trino connection: server=$TRINO_SERVER catalog=$TRINO_CATALOG schema=$TRINO_SCHEMA user=$TRINO_USER"

phase() {  # phase <name> <cmd...>
  local name="$1"; shift
  echo
  echo "=============================================================="
  echo "== PHASE $name"
  echo "=============================================================="
  if "$@" > "$OUT/${name}.log" 2>&1; then
    echo "$name PASS" >> "$OUT/phase_status.txt"
    echo "-> PASS (log: $OUT/${name}.log)"
  else
    echo "$name FAIL" >> "$OUT/phase_status.txt"
    echo "-> FAIL (log: $OUT/${name}.log)"
    tail -n 25 "$OUT/${name}.log"
  fi
}

# 1. compiler front-end: SQL -> dependency graph (no DB needed)
phase frontend python3 -m eimer \
  --sql-file examples/valid/03_five_vars_dense_dependencies.sql \
  --emit dependency-graph

# 2. the config-driven pipeline end-to-end, with the per-batch native-MATCH_RECOGNIZE
#    full-tuple correctness capture ON (generate -> load -> b_unified sigma -> select
#    -> execute rank-1 -> verify)
phase pipeline python3 runner/run.py \
  --experiment demo/configs/demo_experiment.yaml \
  --environment demo/configs/demo_environment.yaml \
  --out-dir "$OUT/pipeline"

# 3. EIMER vs native MATCH_RECOGNIZE + the cover portfolio,
#    full-tuple identity gate per arm
phase eimer_vs_mr python3 demo/run_eimer_vs_mr.py \
  --n 5000 --k 4 --topology chain --sigma 0.05 --batches 5 \
  --out-dir "$OUT/eimer_vs_mr"

# 4. the storage budget M: engine-free descent + live gates
phase memory_budget python3 demo/run_memory_budget.py \
  --n 3000 --k 4 --topology star --sigma 0.05 --batches 3 \
  --out-dir "$OUT/memory_budget"

# 5.+6. dependent-predicate classes: equi and band+equi cells (tiny), same
#    full-tuple identity gates as phase 3
phase predicates_equi python3 demo/run_eimer_vs_mr.py \
  --predicates equi --n 2000 --k 3 --topology chain --sigma 0.10 --batches 2 \
  --out-dir "$OUT/predicates_equi"
phase predicates_band_equi python3 demo/run_eimer_vs_mr.py \
  --predicates band_equi --n 2000 --k 3 --topology chain --sigma 0.20 --batches 2 \
  --out-dir "$OUT/predicates_band_equi"

echo
python3 docker/summarize_demo.py "$OUT"
