#!/usr/bin/env bash
# The Esper experiment (Fig. 8c): EIMER's strategies on Esper vs. Esper's native match_recognize, over the table size
# N and the pattern length k.
# Runs INSIDE the experiments container (reproducibility/run_esper.sh does that), or on a host with the pinned Python
# packages, a Java 21 runtime and the Esper jar (cd backends/esper/java && mvn package). Everything is written under
# $OUT (default reproducibility/out/esper).
# The figure has two panels from two configurations of the same workload (windowed chain, eight event types at rate
# 0.05, sigma 0.05, W = 1 s):
#   N panel   fig8s005:      table size at batches of 200 events (B = N / 200), k = 4
#   k panel   fig8s005n25k:  pattern length at N = 25,000, B = 25
# Stages, per panel:
#   1 runtime   run_cells.py           Esper MR (window also on the gap variable) and EIMER on clique-grow's pick, the edge
#                                      cover, all pattern prefixes and the singletons; one JVM per approach; results
#                                      compared on full tuples and per-batch counts with EIMER on DuckDB
#   2 memory    run_cells.py           the same approaches once more with the JVM's peak live heap measured
#   3 jvmbase   run_jvm_baseline.py    the JVM with the data set loaded and nothing run (subtracted from stage 2)
# then the figure and the summary (table and checks). Parameters and limits come from the environment (reproducibility/README.md).
set -uo pipefail
cd "$(dirname "$0")/../../.."
REPO="$PWD"
source reproducibility/docker/in_container/experiments_common.sh

OUT="${OUT:-$REPO/reproducibility/out/esper}"; LOGS="$OUT/logs"; export LOGS
EXP="$REPO/reproducibility/experiments/esper"
if [ "${SCALE:-small}" = "paper" ]; then NS="${NS:-2000,5000,10000,25000,50000,100000,250000,500000}"; KS="${KS:-3,4,5,6,7,8}"; fi
export EIMER_REPRO_ESPER_NS="${NS:-2000,5000,10000}" EIMER_REPRO_ESPER_KS="${KS:-3,4,5}"
[ -n "${WALL:-}" ] && export EIMER_REPRO_WALL="$WALL"
export JAVA="${JAVA:-java}" ESPER_JAR="${ESPER_JAR:-$REPO/backends/esper/java/target/esper-eimer.jar}"
[ -f "$ESPER_JAR" ] || die "the Esper jar is missing: $ESPER_JAR (cd backends/esper/java && mvn package)"
command -v "$JAVA" >/dev/null || die "no Java runtime ($JAVA)"
# the configuration of the paper's runs
export KAPPA_FILE="$EXP/kappa_esper.json" NATIVE_FORM=bounded ESPER_ARMS=pick,edge,prefix,singletons UPDATE_FORM=chain
unset EIMER_SELECT_ON_DERIVED EIMER_DISABLE_COMPOSE_TREE EIMER_DISABLE_WINDOW_PROPAGATION EIMER_DISABLE_PREFIX_PRUNE \
      EIMER_DISABLE_SCHEMA_MIN EIMER_DEPENDENT_PREDICATES_IN_ON EIMER_RENDER ESPER_GC_SAMPLES ESPER_MEASURE_MEMORY ESPER_SKIP_DNF_FROM RESULTS_DIR

mkdir -p "$OUT" "$LOGS"
export EIMER_REPRO_SPECS_DIR="$OUT/specs"   # generated files go next to the results (the repository may be read-only)
log "== Esper experiment: N in {$EIMER_REPRO_ESPER_NS} (batches of 200 events, k = 4), k in {$EIMER_REPRO_ESPER_KS} (N = 25,000, B = 25)"
log "   java: $("$JAVA" -version 2>&1 | head -1) | $(python3 -c 'import duckdb, numpy, pandas; print("duckdb", duckdb.__version__, "| numpy", numpy.__version__, "| pandas", pandas.__version__)')"
log "   kappa $(python3 -c "import json;print(json.load(open('$KAPPA_FILE'))['kappa'])") | Esper MR: bounded form | EIMER arms: $ESPER_ARMS | view updates: chain form | JVM per arm: -Xmx12g -XX:+UseSerialGC -XX:ActiveProcessorCount=1"

RC=0
for CENTER in fig8s005 fig8s005n25k; do
  case "$CENTER" in fig8s005) R="$OUT/fig8_s005" ;; *) R="$OUT/fig8_s005_n25k" ;; esac
  export ESPER_CENTER="$CENTER"
  CELLS="$(cd "$EXP" && python3 cells.py --list)" || die "cells.py failed"
  log "== center $CENTER: $(echo $CELLS)"
  unset ESPER_MEASURE_MEMORY ESPER_SKIP_DNF_FROM
  run_stage "runtime.$CENTER" "$EXP" "$R" "$CELLS" python3 -u run_cells.py --results-dir "$R" || RC=1
  if [ "${MEMORY:-1}" = "1" ]; then
    [ -x /usr/bin/time ] || die "GNU time missing (/usr/bin/time): the memory stage needs it"
    export ESPER_MEASURE_MEMORY=1 ESPER_SKIP_DNF_FROM="$R"
    run_stage "memory.$CENTER" "$EXP" "${R}_mem" "$CELLS" python3 -u run_cells.py --results-dir "${R}_mem" || RC=1
    unset ESPER_MEASURE_MEMORY ESPER_SKIP_DNF_FROM
    run_stage "jvmbase.$CENTER" "$EXP" "${R}_jvmbase" "$CELLS" python3 -u run_jvm_baseline.py --cells-from "${R}_mem" --out "${R}_jvmbase" --reps 3 || RC=1
  fi
done

log "== figure"
# below the paper's scale the largest values sit under the in-panel legend: keep the panels' upper part free (paper scale: unchanged)
[ "$EIMER_REPRO_ESPER_NS,$EIMER_REPRO_ESPER_KS" = "2000,5000,10000,25000,50000,100000,250000,500000,3,4,5,6,7,8" ] || export FIG_LEGEND_ROOM=1
bash reproducibility/docker/in_container/render_figures.sh esper "$OUT" > "$LOGS/figure.log" 2>&1 && log "   $(ls "$OUT"/figs/*.pdf 2>/dev/null | tr '\n' ' ')" || { log "   figure FAILED (see $LOGS/figure.log)"; tail -n 15 "$LOGS/figure.log" | sed 's/^/        /'; RC=1; }

log "== summary"
python3 reproducibility/docker/in_container/summarize_experiments.py esper "$OUT" || RC=1
[ "$RC" = 0 ] && log "== Esper experiment: ALL CHECKS PASSED" || log "== Esper experiment: CHECKS FAILED (see above)"
exit $RC
