#!/usr/bin/env bash
# Demo suite: six phases (frontend, pipeline, eimer_vs_mr, memory_budget, predicates_equi,
# predicates_band_equi), run inside the pipeline container or on any host with Trino reachable.
# Usage: docker/reproduce.sh
# Env: OUT (default $PWD/docker/out), TRINO_SERVER (localhost:8080), TRINO_CATALOG (memory),
#      TRINO_SCHEMA (default), TRINO_USER (eimer)
# Results: per-phase logs and artifacts under $OUT; docker/summarize_demo.py prints the verdict.
set -u
cd "$(dirname "$0")/.."
OUT="${OUT:-$PWD/docker/out}"
mkdir -p "$OUT"
: > "$OUT/phase_status.txt"

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

phase frontend python3 -m eimer \
  --sql-file examples/valid/03_five_vars_dense_dependencies.sql \
  --emit dependency-graph

phase pipeline python3 runner/run.py \
  --experiment demo/configs/demo_experiment.yaml \
  --environment demo/configs/demo_environment.yaml \
  --out-dir "$OUT/pipeline"

phase eimer_vs_mr python3 demo/run_eimer_vs_mr.py \
  --n 5000 --k 4 --topology chain --sigma 0.05 --batches 5 \
  --out-dir "$OUT/eimer_vs_mr"

phase memory_budget python3 demo/run_memory_budget.py \
  --n 3000 --k 4 --topology star --sigma 0.05 --batches 3 \
  --out-dir "$OUT/memory_budget"

phase predicates_equi python3 demo/run_eimer_vs_mr.py \
  --predicates equi --n 2000 --k 3 --topology chain --sigma 0.10 --batches 2 \
  --out-dir "$OUT/predicates_equi"
phase predicates_band_equi python3 demo/run_eimer_vs_mr.py \
  --predicates band_equi --n 2000 --k 3 --topology chain --sigma 0.20 --batches 2 \
  --out-dir "$OUT/predicates_band_equi"

echo
python3 docker/summarize_demo.py "$OUT"
