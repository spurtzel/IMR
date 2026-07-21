#!/usr/bin/env bash
# ONE-COMMAND small-scale sensitivity analysis:  ./docker/run_small_sensitivity_analysis.sh
#
# Builds the pinned Trino-481 + pipeline images, starts Trino, runs the OAT
# sensitivity suite (clique-grow vs native MATCH_RECOGNIZE; axes: pattern length k,
# table size N <= 10k, batch count B at N=5000 fixed, selectivity sigma) inside the
# container, auto-plots the results, and tears everything down.
#
# Outputs (host): docker/out/sensitivity/{results.json, sensitivity.png, sensitivity.pdf}
# Exit code 0 <=> every executed cell's full-tuple identity gate passed.
#
# Knobs (env):
#   WALL=600            per-query wall in seconds (an arm exceeding it = DNF, plotted as such)
#   BASELINE_K=4 BASELINE_N=5000 BASELINE_B=5 BASELINE_SIGMA=0.05   the shared baseline cell
#   KS=3,4,5  NS=1000,2500,5000,10000  BS=2,5,10  SIGMAS=0.01,0.05,0.20   the axis values
#   TRINO_MEM=6g        memory limit for the Trino container
#   KEEP_UP=1           leave the Trino container running afterwards
#   TRINO_BASE_IMAGE=trinodb/trino:481
#
# Example: a custom sweep:
#   BASELINE_N=8000 NS=2000,4000,8000 KS=3,4 WALL=900 ./docker/run_small_sensitivity_analysis.sh
set -euo pipefail
cd "$(dirname "$0")/.."

NET=eimer-sens-net
TRINO=eimer-sens-trino
PIPE=eimer-demo-pipeline
TRINO_MEM="${TRINO_MEM:-6g}"
WALL="${WALL:-600}"
BASE="${TRINO_BASE_IMAGE:-trinodb/trino:481}"

echo "== [1/4] build images (base: $BASE)"
docker build --target trino-server -t eimer-demo-trino --build-arg TRINO_IMAGE="$BASE" -f docker/Dockerfile .
docker build --target pipeline     -t "$PIPE"                                         -f docker/Dockerfile .

cleanup() {
  if [ "${KEEP_UP:-0}" != "1" ]; then
    docker rm -f "$TRINO" >/dev/null 2>&1 || true
    docker network rm "$NET" >/dev/null 2>&1 || true
  else
    echo "KEEP_UP=1: Trino left running as container '$TRINO' on network '$NET'"
  fi
}
trap cleanup EXIT

echo "== [2/4] start Trino"
docker network inspect "$NET" >/dev/null 2>&1 || docker network create "$NET" >/dev/null
docker rm -f "$TRINO" >/dev/null 2>&1 || true
docker run -d --name "$TRINO" --network "$NET" --network-alias trino \
  -m "$TRINO_MEM" eimer-demo-trino >/dev/null

echo -n "== waiting for Trino to become ACTIVE "
for i in $(seq 1 90); do
  state=$(docker exec "$TRINO" sh -c \
    'curl -s http://localhost:8080/v1/info 2>/dev/null' | grep -o '"starting":false' || true)
  if [ -n "$state" ]; then echo " OK"; break; fi
  echo -n "."
  sleep 2
  if [ "$i" = 90 ]; then echo " TIMEOUT"; docker logs --tail 30 "$TRINO"; exit 1; fi
done

echo "== [3/4] run the sensitivity suite (wall ${WALL}s per query; this can take a while"
echo "==      : hard cells are allowed to run up to the wall and count as DNF data)"
mkdir -p docker/out
set +e
SWEEP_ARGS=(--wall "$WALL" --out-dir docker/out/sensitivity)
[ -n "${BASELINE_K:-}" ]     && SWEEP_ARGS+=(--baseline-k "$BASELINE_K")
[ -n "${BASELINE_N:-}" ]     && SWEEP_ARGS+=(--baseline-n "$BASELINE_N")
[ -n "${BASELINE_B:-}" ]     && SWEEP_ARGS+=(--baseline-batches "$BASELINE_B")
[ -n "${BASELINE_SIGMA:-}" ] && SWEEP_ARGS+=(--baseline-sigma "$BASELINE_SIGMA")
[ -n "${KS:-}" ]             && SWEEP_ARGS+=(--ks "$KS")
[ -n "${NS:-}" ]             && SWEEP_ARGS+=(--ns "$NS")
[ -n "${BS:-}" ]             && SWEEP_ARGS+=(--bs "$BS")
[ -n "${SIGMAS:-}" ]         && SWEEP_ARGS+=(--sigmas "$SIGMAS")
docker run --rm --network "$NET" \
  --user "$(id -u):$(id -g)" \
  -v "$PWD/docker/out:/eimer/docker/out" \
  "$PIPE" \
  python3 demo/run_sensitivity.py "${SWEEP_ARGS[@]}"
rc=$?
set -e

echo "== [4/4] done: results + figure in docker/out/sensitivity/"
ls -l docker/out/sensitivity/ 2>/dev/null || true
exit "$rc"
