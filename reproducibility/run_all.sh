#!/usr/bin/env bash
# All experiments, one after the other:  bash reproducibility/run_all.sh
#   SCALE=paper bash reproducibility/run_all.sh                the paper's parameters
#   ONLY="duckdb trino_fig9" bash reproducibility/run_all.sh   only these experiments (ids: --list)
# The default is the small configuration of every experiment. Results go to reproducibility/out/<experiment>/,
# this script's log to reproducibility/out/run_all.log. The exit code is 0 if every experiment passed all its checks.
set -uo pipefail
cd "$(dirname "$0")/.."
SCALE="${SCALE:-small}"
export DOCKER="${DOCKER:-docker}"
OUT=reproducibility/out
die() { echo "ERROR: $*" >&2; exit 2; }
case "$SCALE" in small|paper) ;; *) die "SCALE: small or paper" ;; esac

JOBS=(
  "trino_fig8|run_trino_fig8.sh"
  "duckdb|run_duckdb.sh"
  "esper|run_esper.sh"
  "trino_fig9|run_trino_fig9.sh"
  "selection_time|run_selection_time.sh"
  "trino_fig10|run_trino_fig10.sh"
  "trino_kleene|run_trino_kleene.sh"
  "trino_budget_shift|run_trino_budget_shift.sh"
  "trino_topology_memory|run_trino_topology_memory.sh"
  "trino_realworld|run_trino_realworld.sh"
)
field() { echo "$1" | cut -d'|' -f"$2"; }
selected() { [ -z "${ONLY:-}" ] && return 0; case " $ONLY " in *" $1 "*) return 0 ;; *) return 1 ;; esac; }

if [ "${1:-}" = "--list" ]; then
  for j in "${JOBS[@]}"; do printf '%-20s SCALE=%s bash reproducibility/%s\n' "$(field "$j" 1)" "$SCALE" "$(field "$j" 2)"; done
  exit 0
fi
for id in ${ONLY:-}; do
  known=0; for j in "${JOBS[@]}"; do [ "$(field "$j" 1)" = "$id" ] && known=1; done
  [ "$known" = 1 ] || die "ONLY: unknown experiment '$id' (see --list)"
done

mkdir -p "$OUT"
log() { echo "$(date '+%F %T') $*" | tee -a "$OUT/run_all.log"; }
log "== run_all: SCALE=$SCALE${ONLY:+, only: $ONLY}"
if [ "${SKIP_BUILD:-0}" != 1 ]; then
  log "build the images (log: $OUT/build.log)"
  read -r -a DK <<< "$DOCKER"
  "${DK[@]}" build -t eimer-trino --build-arg TRINO_HEAP_GB="${TRINO_HEAP_GB:-8}" -f reproducibility/docker/Dockerfile.trino . > "$OUT/build.log" 2>&1 || die "image build failed (see $OUT/build.log)"
  "${DK[@]}" build -t eimer-experiments --build-arg WITH_TEX="${WITH_TEX:-1}" -f reproducibility/docker/Dockerfile.experiments . >> "$OUT/build.log" 2>&1 || die "image build failed (see $OUT/build.log)"
fi

FAILED=""; N=0
for j in "${JOBS[@]}"; do
  id="$(field "$j" 1)"; selected "$id" || continue
  t0=$(date +%s); N=$((N + 1))
  log "start $id: SCALE=$SCALE bash reproducibility/$(field "$j" 2)"
  ( SCALE="$SCALE" OUT_NAME="$id" SKIP_BUILD=1 bash "reproducibility/$(field "$j" 2)" ) > "$OUT/$id.log" 2>&1
  rc=$?
  log "end   $id rc=$rc after $(( ($(date +%s) - t0) / 60 )) min (log $OUT/$id.log, results $OUT/$id/)"
  [ "$rc" = 0 ] || FAILED+="$id "
done
if [ -z "$FAILED" ]; then log "== ALL EXPERIMENTS PASSED ($N)"; exit 0; fi
log "== FAILED: $FAILED"; exit 1
