#!/usr/bin/env bash
# Fig. 11 on Trino, storage budget and distribution shift: speed-up over native MATCH_RECOGNIZE while the storage budget
# shrinks, and per-batch latency of native MR, the stale plan and two re-selections around a shift.
#   bash reproducibility/run_trino_budget_shift.sh                                               the small default
#   SHIFT_N=6000 SHIFT_PARAMS=0.008,0.08,0.052,0.12 bash reproducibility/run_trino_budget_shift.sh   own values
# Results: reproducibility/out/trino_budget_shift/ (records, summary.md, figs/trino_fig11_budget_shift.pdf). The exit code is 0 if every check passed. Parameters: reproducibility/README.md.
exec "$(dirname "$0")/docker/run_trino_experiment.sh" budget_shift
