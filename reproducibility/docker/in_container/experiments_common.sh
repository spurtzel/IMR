#!/usr/bin/env bash
# Helpers of the in-container scripts (sourced): logging, and running the workloads of a stage one after the other
# or PARALLEL at a time.
#   PARALLEL=1     workloads executed at the same time; with PARALLEL > 1 every workload is pinned to its own core
#   CORES="0 1 2"  the cores for PARALLEL > 1 (default: the first PARALLEL cores)
set -uo pipefail

stamp() { date '+%Y-%m-%d %H:%M:%S'; }
log()   { echo "$(stamp) $*"; }
die()   { echo "$(stamp) ERROR: $*" >&2; exit 1; }
export -f stamp

PARALLEL="${PARALLEL:-1}"
if [ "$PARALLEL" -gt 1 ]; then
  CORES="${CORES:-$(seq 0 $((PARALLEL - 1)) | tr '\n' ' ')}"
  command -v taskset >/dev/null || die "taskset missing (needed for PARALLEL > 1)"
fi
export PARALLEL CORES="${CORES:-}"

run_one() {
  local cell="$1" pin=""
  if [ -f "$STAGE_ROOT/records/$cell.json" ]; then echo "$(stamp)    skip  $STAGE_TAG $cell (record exists)"; return 0; fi
  if [ "$PARALLEL" -gt 1 ]; then
    local core; core=$(echo $CORES | cut -d' ' -f$(( ${SLOT:-0} + 1 ))); pin="taskset -c $core"
  fi
  echo "$(stamp)    start $STAGE_TAG $cell${pin:+ ($pin)}"
  if ( cd "$STAGE_WD" && $pin $STAGE_CMD --only "$cell" ) > "$LOGS/$STAGE_TAG.$cell.log" 2>&1; then
    echo "$(stamp)    done  $STAGE_TAG $cell: $(grep -E '^\s+(transpiled|eimer|esper_native|eimer_esper[a-z_]*|mr|gdyn|prefilter|gates|->)' "$LOGS/$STAGE_TAG.$cell.log" | tr -s ' ' | tr '\n' ';' | cut -c1-600)"
  else
    echo "$(stamp)    FAILED $STAGE_TAG $cell (see $LOGS/$STAGE_TAG.$cell.log)"; tail -n 15 "$LOGS/$STAGE_TAG.$cell.log" | sed 's/^/        /'
    return 1
  fi
}
export -f run_one

# run_stage <tag> <working dir> <results root> <cells, one per line> <command...>
run_stage() {
  local tag="$1" wd="$2" root="$3" cells="$4"; shift 4
  export STAGE_TAG="$tag" STAGE_WD="$wd" STAGE_ROOT="$root" STAGE_CMD="$*"
  mkdir -p "$root" "$LOGS"
  log "== stage $tag: $(echo "$cells" | grep -c .) cell(s), $PARALLEL at a time -> $root"
  local rc=0
  echo "$cells" | grep . | xargs -P "$PARALLEL" -n 1 --process-slot-var=SLOT bash -c 'run_one "$1"' _ || rc=1
  log "== stage $tag finished: $( (ls "$root/records" 2>/dev/null || true) | grep -c . ) record(s)"
  return $rc
}
