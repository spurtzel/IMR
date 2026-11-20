#!/usr/bin/env bash
# Shared launcher of the Trino experiments (called by reproducibility/run_trino_<experiment>.sh):
#   run_trino_experiment.sh fig8|fig9|fig10|kleene|budget_shift|realworld|topology_memory
# Builds the Trino server image (Trino 479 with an Iceberg and an in-memory catalog) and the experiments image, starts
# the server, runs in_container/reproduce_trino.sh in the experiments container next to it and removes the server.
# Results: reproducibility/out/<OUT_NAME>/. The exit code is 0 if every check passed. Parameters: reproducibility/README.md.
set -euo pipefail
cd "$(dirname "$0")/../.."
WHICH="${1:-}"
case "$WHICH" in fig8|fig9|fig10|kleene|budget_shift|realworld|topology_memory) ;; *) echo "usage: run_trino_experiment.sh fig8|fig9|fig10|kleene|budget_shift|realworld|topology_memory"; exit 2 ;; esac
read -r -a DK <<< "${DOCKER:-docker}"   # the container command, possibly several words ("sudo docker")
PIN=(); [ -n "${CPUSET:-}" ] && PIN=(--cpuset-cpus "$CPUSET")
IMAGE=eimer-experiments; TRINO_IMAGE=eimer-trino
NET="eimer-trino-net-$WHICH${RUN_ID:+-$RUN_ID}"; TRINO="eimer-trino-$WHICH${RUN_ID:+-$RUN_ID}"   # RUN_ID: several runs side by side
HEAP="${TRINO_HEAP_GB:-8}"
EXPERIMENT_CATALOG="${EXPERIMENT_CATALOG:-iceberg}"   # the catalog this experiment runs on unless CATALOG says otherwise
CATALOG="${CATALOG:-$EXPERIMENT_CATALOG}"
case "$CATALOG" in iceberg|memory) ;; *) echo "CATALOG: iceberg or memory"; exit 2 ;; esac
# a run on the other catalog keeps its records apart
if [ "$CATALOG" = "$EXPERIMENT_CATALOG" ]; then DEFAULT_OUT="trino_$WHICH"; else DEFAULT_OUT="trino_${WHICH}_$CATALOG"; fi
# the result files must belong to the calling user
if [ "$(basename "${DK[-1]}")" = podman ]; then USERARGS=(--userns=keep-id); else USERARGS=(--user "$(id -u):$(id -g)"); fi

if [ "${SKIP_BUILD:-0}" = "1" ]; then
  echo "== [1/4] SKIP_BUILD=1: using the existing images ($TRINO_IMAGE, $IMAGE)"
else
  echo "== [1/4] build the images ($TRINO_IMAGE, $IMAGE)"
  "${DK[@]}" build -t "$TRINO_IMAGE" --build-arg TRINO_HEAP_GB="$HEAP" -f reproducibility/docker/Dockerfile.trino .
  "${DK[@]}" build -t "$IMAGE" --build-arg WITH_TEX="${WITH_TEX:-1}" -f reproducibility/docker/Dockerfile.experiments .
fi

# options that change the server's configuration: a derived server image is built on the fly
DERIVED_TAG=""; DERIVED_LINES=()
if [ -n "${TRINO_WORKER_THREADS:-}" ]; then   # the worker-thread cap
  case "$TRINO_WORKER_THREADS" in ''|*[!0-9]*) echo "TRINO_WORKER_THREADS: a positive integer"; exit 2 ;; esac
  DERIVED_LINES+=("RUN printf '\\ntask.max-worker-threads=$TRINO_WORKER_THREADS\\n' >> /etc/trino/config.properties")
  DERIVED_TAG+="-wt$TRINO_WORKER_THREADS"
  echo "== worker threads capped at $TRINO_WORKER_THREADS"
fi
if [ -n "${TRINO_MEMORY_CATALOG_GB:-}" ]; then   # larger in-memory tables, with the heap that holds them
  case "$TRINO_MEMORY_CATALOG_GB" in ''|*[!0-9]*) echo "TRINO_MEMORY_CATALOG_GB: a positive integer"; exit 2 ;; esac
  QUERY_MEM=$((HEAP - 2 - TRINO_MEMORY_CATALOG_GB))
  [ "$QUERY_MEM" -ge 2 ] || { echo "TRINO_HEAP_GB ($HEAP) must be at least TRINO_MEMORY_CATALOG_GB ($TRINO_MEMORY_CATALOG_GB) + 4"; exit 2; }
  DERIVED_LINES+=("RUN sed -i 's/^memory.max-data-per-node=.*/memory.max-data-per-node=${TRINO_MEMORY_CATALOG_GB}GB/' /etc/trino/catalog/memory.properties && sed -i 's/^-Xmx.*/-Xmx${HEAP}G/' /etc/trino/jvm.config && sed -i 's/^query.max-memory=.*/query.max-memory=${QUERY_MEM}GB/; s/^query.max-memory-per-node=.*/query.max-memory-per-node=${QUERY_MEM}GB/' /etc/trino/config.properties")
  DERIVED_TAG+="-mc${TRINO_MEMORY_CATALOG_GB}h${HEAP}"
  echo "== in-memory tables up to $TRINO_MEMORY_CATALOG_GB GB (heap $HEAP GB, per-query memory $QUERY_MEM GB)"
fi
if [ -n "$DERIVED_TAG" ]; then
  CAPDIR="$PWD/reproducibility/out/.trino-derived${RUN_ID:+-$RUN_ID}"; mkdir -p "$CAPDIR"
  { echo "FROM $TRINO_IMAGE"; printf '%s\n' "${DERIVED_LINES[@]}"; } > "$CAPDIR/Dockerfile"
  "${DK[@]}" build -q -t "$TRINO_IMAGE$DERIVED_TAG" "$CAPDIR" >/dev/null
  TRINO_IMAGE="$TRINO_IMAGE$DERIVED_TAG"
  echo "== server image $TRINO_IMAGE"
fi

cleanup() {
  if [ "${KEEP_UP:-0}" = "1" ]; then
    echo "KEEP_UP=1: Trino left running as container '$TRINO' on network '$NET'"
  else
    "${DK[@]}" rm -f "$TRINO" >/dev/null 2>&1 || true
    "${DK[@]}" network rm "$NET" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

echo "== [2/4] start Trino ($TRINO, heap ${HEAP} GB)"
"${DK[@]}" network inspect "$NET" >/dev/null 2>&1 || "${DK[@]}" network create "$NET" >/dev/null
"${DK[@]}" rm -f "$TRINO" >/dev/null 2>&1 || true
"${DK[@]}" run -d --name "$TRINO" --network "$NET" --network-alias trino -m "${TRINO_CONTAINER_GB:-$((HEAP + 2))}g" "${PIN[@]}" "$TRINO_IMAGE" >/dev/null

echo -n "== [3/4] waiting for Trino "
for i in $(seq 1 120); do
  if "${DK[@]}" exec "$TRINO" sh -c 'curl -s http://localhost:8080/v1/info 2>/dev/null' | grep -q '"starting":false'; then echo " ready"; break; fi
  echo -n "."; sleep 2
  if [ "$i" = 120 ]; then echo " TIMEOUT"; "${DK[@]}" logs --tail 30 "$TRINO"; exit 1; fi
done

echo "== [4/4] run the Trino experiment $WHICH (catalog: $CATALOG)"
OUT_NAME="${OUT_NAME:-$DEFAULT_OUT}"
mkdir -p "reproducibility/out/$OUT_NAME"
set +e
"${DK[@]}" run --rm --network "$NET" "${PIN[@]}" -v "$PWD/reproducibility/out/$OUT_NAME:/eimer/reproducibility/out/trino_$WHICH" "${USERARGS[@]}" -e HOME=/tmp \
  -e TRINO_SERVER=trino:8080 -e TRINO_CATALOG="$CATALOG" -e TRINO_SCHEMA=default -e TRINO_USER=eimer \
  -e NS -e KS -e NW -e SIGMAS -e WS -e NBS -e BS -e BW -e FIRST_RATE -e LAST_RATE -e WB -e CLASSES -e NC -e SCHED_N -e SCHED_B -e PCTS -e SKEW_N -e PARTS -e TOPOLOGIES -e TOPO_SIGMAS -e TOPO_N -e TOPO_B -e TOPO_RATE -e MEM_KS -e MEM_N -e MEM_B -e MEM_SIGMA -e MEM_RATE -e MEM_TOTAL -e PAYOFF_N -e PAYOFF_L -e SEEDS -e KN -e KB -e LS -e WORKLOADS -e SHIFT_N -e SHIFT_PARAMS -e RW_N -e RW_BATCH -e DATASETS -e QUERIES -e RW_TOTAL -e RW_GATE_TOTAL -e SCALE -e WALL -e MR_TOTAL -e MR_WALL -e SERVER_WARMUP -e EIMER_REPRO_TRINO_SESSION -e ARMS -e TRINO_WORKER_THREADS -e RUN_ID -e PANELS \
  "$IMAGE" bash reproducibility/docker/in_container/reproduce_trino.sh "$WHICH"
rc=$?
set -e
echo "== done (results, logs and figure in reproducibility/out/$OUT_NAME/)"
exit "$rc"
