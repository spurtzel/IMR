#!/usr/bin/env bash
# Fig. 9 on Trino: native MATCH_RECOGNIZE, the join-based prefilter and EIMER over the window size W and the number
# of batches B.
#   bash reproducibility/run_trino_fig9.sh                                          the small default
#   NW=10000 NBS=10000,25000 BS=1,5,25,50 bash reproducibility/run_trino_fig9.sh    own axis values (SCALE=paper: the paper's)
# Results: reproducibility/out/trino_fig9/ (records, summary.md, figs/trino_fig9_W_B.pdf). The exit code is 0 if every check passed. Parameters: reproducibility/README.md.
exec "$(dirname "$0")/docker/run_trino_experiment.sh" fig9
