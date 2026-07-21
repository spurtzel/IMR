#!/usr/bin/env bash
# ONE-COMMAND reproduction:  ./docker/run_demo.sh
#
# Builds the pinned Trino-481 image + the pipeline image, starts Trino, waits until
# it is ACTIVE, runs the demo suite in the pipeline container, prints the summary,
# and tears everything down. Exit code 0 <=> every gate passed.
#
# Knobs (env):
#   TRINO_MEM=6g        memory limit for the Trino container
#   KEEP_UP=1           leave the Trino container running afterwards
#   TRINO_BASE_IMAGE=trinodb/trino:481   the pinned base image
set -euo pipefail
cd "$(dirname "$0")/.."

NET=eimer-demo-net
TRINO=eimer-demo-trino
PIPE=eimer-demo-pipeline
TRINO_MEM="${TRINO_MEM:-6g}"
BASE="${TRINO_BASE_IMAGE:-trinodb/trino:481}"

echo "== [1/4] build images (base: $BASE)"
docker build --target trino-server -t "$TRINO" --build-arg TRINO_IMAGE="$BASE" -f docker/Dockerfile .
docker build --target pipeline     -t "$PIPE"                                  -f docker/Dockerfile .

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
  -m "$TRINO_MEM" "$TRINO" >/dev/null

echo -n "== waiting for Trino to become ACTIVE "
for i in $(seq 1 90); do
  state=$(docker exec "$TRINO" sh -c \
    'curl -s http://localhost:8080/v1/info 2>/dev/null' | grep -o '"starting":false' || true)
  if [ -n "$state" ]; then echo " OK"; break; fi
  echo -n "."
  sleep 2
  if [ "$i" = 90 ]; then echo " TIMEOUT"; docker logs --tail 30 "$TRINO"; exit 1; fi
done

echo "== [3/4] run the demo suite"
mkdir -p docker/out
set +e
# --user keeps the artifacts in docker/out owned by the invoking host user
docker run --rm --network "$NET" \
  --user "$(id -u):$(id -g)" \
  -v "$PWD/docker/out:/eimer/docker/out" \
  "$PIPE"
rc=$?
set -e

echo "== [4/4] done (results + logs in docker/out/)"
exit "$rc"
