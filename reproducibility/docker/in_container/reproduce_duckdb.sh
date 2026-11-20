#!/usr/bin/env bash
# The DuckDB experiment (Fig. 8b): EIMER on DuckDB vs. re-executing the MATCH_RECOGNIZE query transpiled to SQL
# (Lambrecht et al., PVLDB 2025), over the table size N and the pattern length k.
# Runs INSIDE the experiments container (reproducibility/run_duckdb.sh does that), or on a host with the pinned Python
# packages. Everything is written under $OUT (default reproducibility/out/duckdb).
# Stages:
#   1 runtime   run_cells.py        both approaches per workload, single-threaded, no warm-up, results compared on full tuples
#   2 peakmem   run_peak_memory.py  peak memory per approach, each in its own process
#   3 figure    plot_paper.py       the figure from these records
#   4 summary   summarize_experiments.py  table and checks
# Parameters and limits come from the environment (reproducibility/README.md).
set -uo pipefail
cd "$(dirname "$0")/../../.."
REPO="$PWD"
source reproducibility/docker/in_container/experiments_common.sh

OUT="${OUT:-$REPO/reproducibility/out/duckdb}"; LOGS="$OUT/logs"; export LOGS
EXP="$REPO/reproducibility/experiments/duckdb"
if [ "${SCALE:-small}" = "paper" ]; then NS="${NS:-5000,10000,25000,50000,100000,250000,500000,1000000}"; KS="${KS:-3,4,5,6,7,8}"; fi
export EIMER_REPRO_DUCKDB_NS="${NS:-5000,10000,25000}" EIMER_REPRO_DUCKDB_KS="${KS:-3,4,5}"
[ -n "${WALL:-}" ] && export EIMER_REPRO_WALL="$WALL"
# the configuration of the paper's runs
export KAPPA_FILE="$EXP/kappa_duckdb.json" FULL_ANCHOR=0 BASELINE_OPTIMIZER=1 EIMER_OPTIMIZER=0
unset EIMER_SELECT_ON_DERIVED EIMER_DISABLE_COMPOSE_TREE EIMER_DISABLE_WINDOW_PROPAGATION EIMER_DISABLE_PREFIX_PRUNE \
      EIMER_DISABLE_SCHEMA_MIN EIMER_DEPENDENT_PREDICATES_IN_ON DUCKDB_THREADS DUCKDB_DATABASE EIMER_REPRO_STAR

mkdir -p "$OUT" "$LOGS"
export EIMER_REPRO_SPECS_DIR="$OUT/specs"   # generated files go next to the results (the repository may be read-only)
CELLS="$(cd "$EXP" && python3 cells.py --list)" || die "cells.py failed"
log "== DuckDB experiment: N in {$EIMER_REPRO_DUCKDB_NS} (batches of 1,000 events), k in {$EIMER_REPRO_DUCKDB_KS} (N = 25,000, B = 25)"
log "   $(python3 -c 'import duckdb, numpy, pandas, pyarrow; print("duckdb", duckdb.__version__, "| numpy", numpy.__version__, "| pandas", pandas.__version__, "| pyarrow", pyarrow.__version__)')"
log "   kappa $(python3 -c "import json;print(json.load(open('$KAPPA_FILE'))['kappa'])") | baseline: join-order optimizer on | EIMER: bushy composition tree, join order pinned | 1 thread, 12 GB memory limit"
log "   cells: $(echo $CELLS)"

RC=0
run_stage runtime "$EXP" "$OUT/runtime" "$CELLS" python3 -u run_cells.py --results-dir "$OUT/runtime" || RC=1
if [ "${MEMORY:-1}" = "1" ]; then
  run_stage peakmem "$EXP" "$OUT/peakmem" "$CELLS" python3 -u run_peak_memory.py --runtime "$OUT/runtime" --out "$OUT/peakmem" || RC=1
fi

log "== figure"
# below the paper's scale the largest values sit under the in-panel legend: give the panel headroom (paper scale: unchanged)
[ "$EIMER_REPRO_DUCKDB_NS,$EIMER_REPRO_DUCKDB_KS" = "5000,10000,25000,50000,100000,250000,500000,1000000,3,4,5,6,7,8" ] || export FIG_LEGEND_ROOM=1
bash reproducibility/docker/in_container/render_figures.sh duckdb "$OUT" > "$LOGS/figure.log" 2>&1 && log "   $(ls "$OUT"/figs/*.pdf 2>/dev/null | tr '\n' ' ')" || { log "   figure FAILED (see $LOGS/figure.log)"; tail -n 15 "$LOGS/figure.log" | sed 's/^/        /'; RC=1; }

log "== summary"
python3 reproducibility/docker/in_container/summarize_experiments.py duckdb "$OUT" || RC=1
[ "$RC" = 0 ] && log "== DuckDB experiment: ALL CHECKS PASSED" || log "== DuckDB experiment: CHECKS FAILED (see above)"
exit $RC
