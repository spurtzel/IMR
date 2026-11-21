#!/usr/bin/env bash
# Fig. 8a, Trino series: native MATCH_RECOGNIZE, GDYN and EIMER on Trino over the table size N and the pattern length k.
#   bash reproducibility/run_trino_fig8.sh                                  the small default
#   NS=2000,5000,10000 KS=3,4,5,6 bash reproducibility/run_trino_fig8.sh    own axis values (SCALE=paper: the paper's)
# Results: reproducibility/out/trino_fig8/ (records, summary.md, figs/trino_fig8_N_k.pdf). The exit code is 0 if every check passed. Parameters: reproducibility/README.md.
exec "$(dirname "$0")/docker/run_trino_experiment.sh" fig8
