#!/usr/bin/env bash
# The selection-time experiment (Section 7.3): the time clique-grow and the edge cover need to select a strategy, over
# the pattern length k for four topologies. No engine is involved.
# Runs INSIDE the experiments container (reproducibility/run_selection_time.sh does that), or on a host with the pinned
# Python packages. Everything is written under $OUT (default reproducibility/out/selection_time).
# Stages: selection_time.py (records, summary and checks), plot_selection_time.py (the figure).
# Parameters come from the environment (reproducibility/README.md).
set -uo pipefail
cd "$(dirname "$0")/../../.."
REPO="$PWD"
OUT="${OUT:-$REPO/reproducibility/out/selection_time}"; LOGS="$OUT/logs"
EXP="$REPO/reproducibility/experiments/selection_time"
if [ "${SCALE:-small}" = "paper" ]; then KS="${KS:-3,4,5,6,7,8,9,10,11,12,13}"; COMPLETE_KS="${COMPLETE_KS:-3,4,5,6}"; fi
export EIMER_REPRO_SEL_KS="${KS:-3,4,5,6,7,8}" EIMER_REPRO_SEL_COMPLETE_KS="${COMPLETE_KS:-3,4,5}"
[ -n "${REPS:-}" ] && export EIMER_REPRO_SEL_REPS="$REPS"
[ -n "${LIMIT:-}" ] && export EIMER_REPRO_SEL_LIMIT="$LIMIT"
mkdir -p "$OUT" "$LOGS"
log() { echo "$(date '+%F %T') $*" | tee -a "$LOGS/run.log"; }
log "== selection-time experiment: chain, star, cycle at k in {$EIMER_REPRO_SEL_KS}, complete at k in {$EIMER_REPRO_SEL_COMPLETE_KS}"
RC=0
python3 -u "$EXP/selection_time.py" --results-dir "$OUT" 2>&1 | tee -a "$LOGS/run.log"; [ "${PIPESTATUS[0]}" = 0 ] || RC=1
log "== figure"
python3 "$EXP/plot_selection_time.py" "$OUT" "$OUT/figs" > "$LOGS/figure.log" 2>&1 && log "   $(ls "$OUT"/figs/*.pdf 2>/dev/null | tr '\n' ' ')" || { log "   figure FAILED (see $LOGS/figure.log)"; RC=1; }
[ "$RC" = 0 ] && log "== selection-time experiment: ALL CHECKS PASSED" || log "== selection-time experiment: CHECKS FAILED (see above)"
exit $RC
