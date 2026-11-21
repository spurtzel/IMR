#!/usr/bin/env bash
# The Trino experiments:  reproduce_trino.sh fig8|fig9|fig10|kleene|budget_shift|realworld|topology_memory
# Runs inside the experiments container next to a running Trino server (reproducibility/run_trino_<experiment>.sh
# starts both), or on a host with the pinned Python packages and a Trino 479 reachable through TRINO_SERVER /
# TRINO_CATALOG / TRINO_SCHEMA. Everything is written under $OUT (default reproducibility/out/trino_<experiment>).
# The approaches run one after the other on the same server: single-threaded, join reordering off. Before the first
# workload that workload is run once, untimed, and discarded. The workloads run in ascending order per axis; an
# approach that did not finish is not run at larger values.
# ARMS=eimer,gdyn (fig8, fig9) runs a subset of the approaches; PANELS=W (fig9) runs only these panels.
# Parameters and limits come from the environment (reproducibility/README.md); the defaults are small, SCALE=paper sets
# the paper's values.
set -uo pipefail
cd "$(dirname "$0")/../../.."
REPO="$PWD"
WHICH="${1:-}"
case "$WHICH" in fig8|fig9|fig10|kleene|budget_shift|realworld|topology_memory) ;; *) echo "usage: reproduce_trino.sh fig8|fig9|fig10|kleene|budget_shift|realworld|topology_memory"; exit 2 ;; esac
PARALLEL=1
source reproducibility/docker/in_container/experiments_common.sh

OUT="${OUT:-$REPO/reproducibility/out/trino_$WHICH}"; LOGS="$OUT/logs"; export LOGS
EXP="$REPO/reproducibility/experiments/trino"
export TRINO_SERVER="${TRINO_SERVER:-localhost:8080}" TRINO_CATALOG="${TRINO_CATALOG:-iceberg}" TRINO_SCHEMA="${TRINO_SCHEMA:-default}" TRINO_USER="${TRINO_USER:-eimer}"
[ -n "${WALL:-}" ] && export EIMER_REPRO_WALL="$WALL"
[ -n "${MR_TOTAL:-}" ] && export EIMER_REPRO_MR_CELL_BOUND="$MR_TOTAL"
[ -n "${MR_WALL:-}" ] && export EIMER_REPRO_MR_WALL="$MR_WALL"
[ "$TRINO_CATALOG" = memory ] && export EIMER_REPRO_TRINO_SESSION="${EIMER_REPRO_TRINO_SESSION:-enable_dynamic_filtering = false}"
unset EIMER_SELECT_ON_DERIVED EIMER_DISABLE_COMPOSE_TREE EIMER_DISABLE_WINDOW_PROPAGATION EIMER_DISABLE_PREFIX_PRUNE \
      EIMER_DISABLE_SCHEMA_MIN EIMER_DEPENDENT_PREDICATES_IN_ON EIMER_REPRO_ARMS
[ -n "${ARMS:-}" ] && export EIMER_REPRO_ARMS="$ARMS"
[ -n "${PANELS:-}" ] && export EIMER_REPRO_FIG9_PANELS="$PANELS"
PAPER="${SCALE:-small}"
if [ "$WHICH" = fig8 ]; then
  if [ "$PAPER" = paper ]; then NS="${NS:-2000,5000,10000,25000,50000,100000,250000}"; KS="${KS:-3,4,5,6,7,8}"; fi
  export EIMER_REPRO_TRINO_NS="${NS:-2000,5000}" EIMER_REPRO_TRINO_KS="${KS:-3,4,5}"
  DESC="N in {$EIMER_REPRO_TRINO_NS} (200 events per batch, k = 4), k in {$EIMER_REPRO_TRINO_KS} (N = 5,000, B = 25)"
elif [ "$WHICH" = budget_shift ]; then
  if [ "$PAPER" = paper ]; then
    PAPER_BUDGET="chain:4:0.01:0.02:8000:25,chain:5:0.005:0.02:10000:25,chain:5:0.002:0.05:10000:25,chain:6:0.003:0.02:12000:25"
    PAPER_BUDGET+=",star:4:0.01:0.02:10000:25,star:5:0.005:0.02:10000:25,diamond:4:0.02:0.02:8000:25,diamond:4:0.05:0.02:5000:25"
    PAPER_BUDGET+=",complete:4:0.05:0.02:5000:25,triangle:4:0.02:0.02:8000:25,cycle:5:0.005:0.02:10000:25,chain:5:0.005:0.02:8000:100"
    export EIMER_REPRO_BUDGET_WORKLOADS="${WORKLOADS:-$PAPER_BUDGET}"
    SHIFT_N="${SHIFT_N:-10000}"; SHIFT_PARAMS="${SHIFT_PARAMS:-0.0125,0.03,0.25,0.045}"
  else
    export EIMER_REPRO_BUDGET_WORKLOADS="${WORKLOADS:-chain:4:0.05:0.05:3000:5,star:4:0.05:0.05:3000:5,triangle:4:0.05:0.05:3000:5,diamond:4:0.05:0.05:3000:5}"
    export EIMER_REPRO_MR_WALL="${MR_WALL:-300}" EIMER_REPRO_MR_CELL_BOUND="${MR_TOTAL:-900}"
  fi
  export EIMER_REPRO_BS_PARTS="${PARTS:-budget,shift}" EIMER_REPRO_SHIFT_N="${SHIFT_N:-4000}" EIMER_REPRO_SHIFT_PARAMS="${SHIFT_PARAMS:-0.012,0.08,0.16,0.12}"
  DESC="storage budget: $EIMER_REPRO_BUDGET_WORKLOADS; shift: N = $EIMER_REPRO_SHIFT_N in 20 batches, (sigma, type rate) $EIMER_REPRO_SHIFT_PARAMS before / after batch 6 (parts: $EIMER_REPRO_BS_PARTS)"
elif [ "$WHICH" = topology_memory ]; then
  if [ "$PAPER" = paper ]; then
    TOPO_N="${TOPO_N:-8000}"; TOPO_B="${TOPO_B:-25}"; TOPO_RATE="${TOPO_RATE:-0.02}"
    MEM_N="${MEM_N:-400000}"; MEM_B="${MEM_B:-25}"; MEM_SIGMA="${MEM_SIGMA:-0.002}"; MEM_RATE="${MEM_RATE:-0.02}"
  else
    export EIMER_REPRO_MR_WALL="${MR_WALL:-300}" EIMER_REPRO_MR_CELL_BOUND="${MR_TOTAL:-900}"
  fi
  export EIMER_REPRO_TM_PARTS="${PARTS:-topology,memory}" EIMER_REPRO_TOPO_TOPOLOGIES="${TOPOLOGIES:-chain,star,triangle,cycle,diamond,complete}" \
         EIMER_REPRO_TOPO_SIGMAS="${TOPO_SIGMAS:-0.01,0.05,0.1}" EIMER_REPRO_TOPO_N="${TOPO_N:-2500}" EIMER_REPRO_TOPO_B="${TOPO_B:-5}" \
         EIMER_REPRO_TOPO_RATE="${TOPO_RATE:-0.05}" EIMER_REPRO_MEM_KS="${MEM_KS:-3,4,5,6}" EIMER_REPRO_MEM_N="${MEM_N:-5000}" \
         EIMER_REPRO_MEM_B="${MEM_B:-5}" EIMER_REPRO_MEM_SIGMA="${MEM_SIGMA:-0.02}" EIMER_REPRO_MEM_RATE="${MEM_RATE:-0.05}"
  [ -n "${MEM_TOTAL:-}" ] && export EIMER_REPRO_MEM_TOTAL="$MEM_TOTAL"
  DESC="topology: {$EIMER_REPRO_TOPO_TOPOLOGIES} at k = 4, sigma in {$EIMER_REPRO_TOPO_SIGMAS}, N = $EIMER_REPRO_TOPO_N in $EIMER_REPRO_TOPO_B batches, type rate $EIMER_REPRO_TOPO_RATE; memory: chain k in {$EIMER_REPRO_MEM_KS}, N = $EIMER_REPRO_MEM_N in $EIMER_REPRO_MEM_B batches, sigma = $EIMER_REPRO_MEM_SIGMA, type rate $EIMER_REPRO_MEM_RATE (parts: $EIMER_REPRO_TM_PARTS)"
elif [ "$WHICH" = realworld ]; then
  if [ "$PAPER" = paper ]; then RW_N="${RW_N:-100000}"; RW_TOTAL="${RW_TOTAL:-14400}"; else export EIMER_REPRO_WALL="${WALL:-300}"; fi
  export EIMER_REPRO_RW_N="${RW_N:-2000}" EIMER_REPRO_RW_BATCH="${RW_BATCH:-200}" EIMER_REPRO_RW_DATASETS="${DATASETS:-taxi,chicago,ecommerce}" \
         EIMER_REPRO_RW_QUERIES="${QUERIES:-1,2,3,4,5}" EIMER_REPRO_RW_TOTAL="${RW_TOTAL:-120}" EIMER_REPRO_RW_GATE_TOTAL="${RW_GATE_TOTAL:-1800}"
  DESC="tables {$EIMER_REPRO_RW_DATASETS}, queries {$EIMER_REPRO_RW_QUERIES}: the first $EIMER_REPRO_RW_N rows in batches of $EIMER_REPRO_RW_BATCH, every batch queried; limit per approach $EIMER_REPRO_RW_TOTAL s"
elif [ "$WHICH" = kleene ]; then
  if [ "$PAPER" = paper ]; then PAYOFF_N="${PAYOFF_N:-100000}"; SEEDS="${SEEDS:-1,2,3}"; KN="${KN:-100000}"; KB="${KB:-100}"; LS="${LS:-5,10,25,50,100}"; fi
  export EIMER_REPRO_KLEENE_PARTS="${PARTS:-payoff,sweep}" EIMER_REPRO_KLEENE_PAYOFF_N="${PAYOFF_N:-50000}" EIMER_REPRO_KLEENE_PAYOFF_L="${PAYOFF_L:-30}" \
         EIMER_REPRO_KLEENE_SEEDS="${SEEDS:-1,2}" EIMER_REPRO_KLEENE_N="${KN:-10000}" EIMER_REPRO_KLEENE_B="${KB:-10}" EIMER_REPRO_KLEENE_LS="${LS:-5,25,100}"
  DESC="one Kleene view, rescan vs. stitch: N = $EIMER_REPRO_KLEENE_PAYOFF_N in batches of 1,000, L = $EIMER_REPRO_KLEENE_PAYOFF_L, seeds {$EIMER_REPRO_KLEENE_SEEDS}; the Kleene query: N = $EIMER_REPRO_KLEENE_N in $EIMER_REPRO_KLEENE_B batches, L in {$EIMER_REPRO_KLEENE_LS} (parts: $EIMER_REPRO_KLEENE_PARTS)"
elif [ "$WHICH" = fig10 ]; then
  if [ "$PAPER" = paper ]; then NS="${NS:-10000,25000,50000,100000}"; NC="${NC:-25000}"; KS="${KS:-3,4,5,6,7,8}"; SCHED_N="${SCHED_N:-50000}"; SCHED_B="${SCHED_B:-1000}"; PCTS="${PCTS:-100,90,80,70,60,50,40,30,20,10,1}"; SKEW_N="${SKEW_N:-25000}"; fi
  export EIMER_REPRO_FIG10_CLASSES="${CLASSES:-acyclic,cyclic}" EIMER_REPRO_FIG10_NS="${NS:-5000,10000}" EIMER_REPRO_FIG10_NC="${NC:-5000}" \
         EIMER_REPRO_FIG10_KS="${KS:-3,4,5}" EIMER_REPRO_FIG10_SCHED_N="${SCHED_N:-5000}" EIMER_REPRO_FIG10_SCHED_B="${SCHED_B:-100}" \
         EIMER_REPRO_FIG10_PCTS="${PCTS:-100,50,10,1}" EIMER_REPRO_FIG10_SKEW_N="${SKEW_N:-10000}" EIMER_REPRO_FIG10_PARTS="${PARTS:-a,b,s}"
  DESC="a: $EIMER_REPRO_FIG10_CLASSES queries, N in {$EIMER_REPRO_FIG10_NS} (200 events per batch, k = 4), k in {$EIMER_REPRO_FIG10_KS} (N = $EIMER_REPRO_FIG10_NC); b: cyclic query, N = $EIMER_REPRO_FIG10_SCHED_N in $EIMER_REPRO_FIG10_SCHED_B batches, {$EIMER_REPRO_FIG10_PCTS} % of the batches queried; s: skewed type frequencies, N = $EIMER_REPRO_FIG10_SKEW_N (parts: $EIMER_REPRO_FIG10_PARTS)"
else
  if [ "$PAPER" = paper ]; then NW="${NW:-10000}"; SIGMAS="${SIGMAS:-0.05,0.02}"; WS="${WS:-200,500,1000,2000}"; NBS="${NBS:-25000,50000}"; BS="${BS:-1,2,5,10,25}"
    export EIMER_REPRO_MR_CELL_BOUND="${MR_TOTAL:-10800}"; fi
  export EIMER_REPRO_FIG9_NW="${NW:-5000}" EIMER_REPRO_FIG9_SIGMAS="${SIGMAS:-0.05}" EIMER_REPRO_FIG9_WS="${WS:-200,500,1000}" \
         EIMER_REPRO_FIG9_NBS="${NBS:-5000}" EIMER_REPRO_FIG9_BS="${BS:-1,5,25}" EIMER_REPRO_FIG9_BW="${BW:-1000}" \
         EIMER_REPRO_FIG9_FIRST_RATE="${FIRST_RATE:-0.01}" EIMER_REPRO_FIG9_LAST_RATE="${LAST_RATE:-0.01}" EIMER_REPRO_FIG9_WB="${WB:-10}"
  DESC="W in {$EIMER_REPRO_FIG9_WS} ms (N = $EIMER_REPRO_FIG9_NW, B = $EIMER_REPRO_FIG9_WB, sigma in {$EIMER_REPRO_FIG9_SIGMAS}), B in {$EIMER_REPRO_FIG9_BS} (N in {$EIMER_REPRO_FIG9_NBS}, W = $EIMER_REPRO_FIG9_BW ms, sigma = 0.05)"
fi

mkdir -p "$OUT" "$LOGS"
export EIMER_REPRO_SPECS_DIR="$OUT/specs"
log "== Trino experiment $WHICH: $DESC"
python3 - <<'PY' || die "Trino is not reachable at $TRINO_SERVER (catalog $TRINO_CATALOG)"
import os, sys
sys.path.insert(0, "reproducibility/experiments/trino")
import trino_lib as tl
c = tl.connect(60)
c.execute(f"CREATE SCHEMA IF NOT EXISTS {os.environ['TRINO_CATALOG']}.{os.environ['TRINO_SCHEMA']}")
i = tl.engine_info(c)
import numpy, pandas
print(f"   {i['engine']} at {os.environ['TRINO_SERVER']}, catalog {i['catalog']}.{i['schema']}, session {i['session']} | numpy {numpy.__version__} | pandas {pandas.__version__}")
PY

RC=0
CELLS="$(cd "$EXP" && python3 $WHICH.py --list)" || die "$WHICH.py --list failed"
log "== workloads: $(echo $CELLS)"
export EIMER_REPRO_SERVER_WARMUP="${SERVER_WARMUP:-1}"
PENDING="$(for c in $CELLS; do [ -f "$OUT/records/$c.json" ] || echo "$c"; done)"
if [ "$EIMER_REPRO_SERVER_WARMUP" = 1 ] && [ -n "$PENDING" ]; then
  FIRST="$(echo "$CELLS" | head -n 1)"
  log "== server warm-up: $FIRST once, untimed, results discarded"
  rm -rf "$OUT/warmup${RUN_ID:+-$RUN_ID}"
  WARM_ENV=(); [ "$WHICH" = realworld ] && [ "${EIMER_REPRO_RW_TOTAL%.*}" -gt 120 ] && WARM_ENV=(EIMER_REPRO_RW_TOTAL=120)
  ( cd "$EXP" && env "${WARM_ENV[@]}" python3 -u "$WHICH.py" --only "$FIRST" --results-dir "$OUT/warmup${RUN_ID:+-$RUN_ID}" ) > "$LOGS/warmup${RUN_ID:+-$RUN_ID}.log" 2>&1 || log "   warm-up run failed (see $LOGS/warmup${RUN_ID:+-$RUN_ID}.log); continuing"
  rm -rf "$OUT/warmup${RUN_ID:+-$RUN_ID}"
fi
run_stage "$WHICH" "$EXP" "$OUT" "$CELLS" python3 -u "$WHICH.py" --results-dir "$OUT" || RC=1

log "== figure"
( cd "$EXP" && python3 plot_trino.py "$WHICH" "$OUT" "$OUT/figs" ) > "$LOGS/figure.log" 2>&1 && log "   $(ls "$OUT"/figs/*.pdf 2>/dev/null | tr '\n' ' ')" || { log "   figure FAILED (see $LOGS/figure.log)"; tail -n 15 "$LOGS/figure.log" | sed 's/^/        /'; RC=1; }

log "== summary"
EIMER_REPRO_EXPECTED="$(echo $CELLS)" python3 reproducibility/docker/in_container/summarize_trino.py "$WHICH" "$OUT" || RC=1
[ "$RC" = 0 ] && log "== Trino experiment $WHICH: ALL CHECKS PASSED" || log "== Trino experiment $WHICH: CHECKS FAILED (see above)"
exit $RC
