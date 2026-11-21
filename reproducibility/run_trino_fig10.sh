#!/usr/bin/env bash
# Fig. 10 on Trino: EIMER and GDYN on acyclic and cyclic queries over N and k (a), the cyclic query with only a
# fraction of the batches queried (b), and both queries on skewed type frequencies (s; §7.4, no figure in the paper).
#   bash reproducibility/run_trino_fig10.sh                                         the small default
#   NS=10000,25000 NC=10000 KS=3,4,5,6 bash reproducibility/run_trino_fig10.sh      own axis values (SCALE=paper: the paper's)
# Results: reproducibility/out/trino_fig10/ (records, summary.md, figs/trino_fig10a_N_k.pdf, figs/trino_fig10b_schedule.pdf,
# figs/trino_fig10s_skew.pdf).
# The exit code is 0 if every check passed. Parameters: reproducibility/README.md.
exec "$(dirname "$0")/docker/run_trino_experiment.sh" fig10
