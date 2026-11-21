#!/usr/bin/env bash
# Selection time (§7.3): the time clique-grow and its seed, the edge cover, need to select a strategy, over the pattern
# length k for chain, star, cycle and complete queries. No engine is involved.
#   bash reproducibility/run_selection_time.sh                                 the small default
#   KS=3,4,5,6,7,8,9,10 COMPLETE_KS=3,4,5 bash reproducibility/run_selection_time.sh   own values (SCALE=paper: the paper's)
# Results: reproducibility/out/selection_time/ (records, summary.md, figs/selection_time.pdf). The exit code is 0 if every check passed.
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
echo "== [2/2] run the selection-time experiment"
OUT_NAME="${OUT_NAME:-selection_time}"; PIN=(); [ -n "${CPUSET:-}" ] && PIN=(--cpuset-cpus "$CPUSET")
mkdir -p "reproducibility/out/$OUT_NAME"
read -r -a DKC <<< "$DOCKER"
"${DKC[@]}" run --rm "${PIN[@]}" -v "$PWD/reproducibility/out/$OUT_NAME:/eimer/reproducibility/out/selection_time" "${USERARGS[@]}" -e HOME=/tmp \
  -e KS -e COMPLETE_KS -e REPS -e LIMIT -e SCALE \
  "$IMAGE" bash reproducibility/docker/in_container/reproduce_selection_time.sh
