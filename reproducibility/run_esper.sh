#!/usr/bin/env bash
# Fig. 8c: EIMER's strategies on Esper vs. Esper's native match_recognize, over the table size N and the pattern
# length k.
#   bash reproducibility/run_esper.sh                                          the small default
#   NS=2000,5000,10000,25000 KS=3,4,5,6 bash reproducibility/run_esper.sh      own axis values (SCALE=paper: the paper's)
# Results: reproducibility/out/esper/ (records, summary.md, figs/esper_N_k.pdf). The exit code is 0 if every check passed.
# Parameters: reproducibility/README.md.
set -euo pipefail
cd "$(dirname "$0")/.."
IMAGE=eimer-experiments
DOCKER="${DOCKER:-docker}"
if [ "$(basename "$DOCKER")" = podman ]; then USERARGS=(--userns=keep-id); else USERARGS=(--user "$(id -u):$(id -g)"); fi
if [ "${SKIP_BUILD:-0}" = "1" ]; then
  echo "== [1/2] SKIP_BUILD=1: using the existing image ($IMAGE)"
else
  echo "== [1/2] build the image ($IMAGE)"
  read -r -a DKC <<< "$DOCKER"; "${DKC[@]}" build -t "$IMAGE" --build-arg WITH_TEX="${WITH_TEX:-1}" -f reproducibility/docker/Dockerfile.experiments .
fi
echo "== [2/2] run the esper experiment"
OUT_NAME="${OUT_NAME:-esper}"; PIN=(); [ -n "${CPUSET:-}" ] && PIN=(--cpuset-cpus "$CPUSET")
mkdir -p "reproducibility/out/$OUT_NAME"
read -r -a DKC <<< "$DOCKER"
"${DKC[@]}" run --rm "${PIN[@]}" -v "$PWD/reproducibility/out/$OUT_NAME:/eimer/reproducibility/out/esper" "${USERARGS[@]}" -e HOME=/tmp \
  -e NS -e KS -e SCALE -e WALL -e MEMORY -e PARALLEL -e CORES \
  "$IMAGE" bash reproducibility/docker/in_container/reproduce_esper.sh
