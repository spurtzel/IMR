#!/usr/bin/env python3
"""Reference results of the two Kleene queries of the real-world experiment, computed directly from the table in
Python, without SQL. The derivation follows the preferment order of MATCH_RECOGNIZE with ONE ROW PER MATCH and
AFTER MATCH SKIP TO NEXT ROW; rows are taken in id order (= ORDER BY time).

taxi Q2       PATTERN (B+ Z* C), no condition between variables, B and C are disjoint row classes.
              A match starts at every B row i. B+ is greedy: the maximal run i..e(i) of B rows. Z* is greedy: C is the
              last C row of the table, L. There is a match iff i < L.
              Tuple (b_count, b_first_id, b_last_id, c_id) = (e(i) - i + 1, i, e(i), L).
ecommerce Q4  PATTERN (A Z*? B+ Z* C), C depends on A only (same user and product, at most 30 minutes later); A, B and
              C are disjoint row classes. For an A row i: Z*? is reluctant, so B+ starts at the first B row j after i
              for which the rest matches; B+ is greedy (the maximal run j..e(j)); Z* is greedy, so C is the last row
              c(i) that satisfies C's conditions against i. With j0 the first B row after i, the rest matches iff
              j0 < c(i).
              Tuple (a_id, b_count, b_first_id, b_last_id, c_id) = (i, e(j0) - j0 + 1, j0, e(j0), c(i)).
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def _run_end(is_b: np.ndarray) -> np.ndarray:
    """e[i] = position of the last row of the maximal run of True values containing i (-1 where is_b is False)."""
    e = np.full(len(is_b), -1, dtype=np.int64)
    cur = -1
    for i in range(len(is_b) - 1, -1, -1):
        if is_b[i]:
            cur = i if cur == -1 else cur
            e[i] = cur
        else:
            cur = -1
    return e


def taxi_q2(df: pd.DataFrame) -> set:
    ids = df["id"].to_numpy()
    fee, tot = df["cbd_congestion_fee"].to_numpy(dtype=float), df["total_amount"].to_numpy(dtype=float)
    is_b, is_c = (fee > 0) & (tot <= 80), (fee > 0) & (tot > 80)
    assert not (is_b & is_c).any()
    if not is_c.any():
        return set()
    last_c = int(np.flatnonzero(is_c)[-1])
    e = _run_end(is_b)
    return {(int(e[i] - i + 1), int(ids[i]), int(ids[e[i]]), int(ids[last_c])) for i in np.flatnonzero(is_b) if i < last_c}


def ecommerce_q4(df: pd.DataFrame) -> set:
    ids, et, rt = df["id"].to_numpy(), df["event_type"].to_numpy(), df["real_time"].to_numpy()
    is_a, is_b, is_c = et == "cart", et == "view", et == "purchase"
    user, prod = df["user_id"].tolist(), df["product_id"].tolist()
    e = _run_end(is_b)
    nxt_b = np.full(len(df) + 1, -1, dtype=np.int64)
    for i in range(len(df) - 1, -1, -1):
        nxt_b[i] = i if is_b[i] else nxt_b[i + 1]
    by_key: dict = {}
    for c in np.flatnonzero(is_c):
        if not (pd.isna(user[c]) or pd.isna(prod[c])):
            by_key.setdefault((int(user[c]), int(prod[c])), []).append(int(c))
    out, window = set(), np.timedelta64(30, "m")
    for i in np.flatnonzero(is_a):
        if pd.isna(user[i]) or pd.isna(prod[i]):
            continue
        cands = [c for c in by_key.get((int(user[i]), int(prod[i])), ()) if c > i and rt[i] <= rt[c] <= rt[i] + window]
        if not cands:
            continue
        c, j0 = max(cands), int(nxt_b[i + 1])
        if j0 == -1 or j0 > c:
            continue
        out.add((int(ids[i]), int(e[j0] - j0 + 1), int(ids[j0]), int(ids[e[j0]]), int(ids[c])))
    return out


REFERENCES = {("taxi", 2): (taxi_q2, ["b_count", "b_first_id", "b_last_id", "c_id"]),
              ("ecommerce", 4): (ecommerce_q4, ["a_id", "b_count", "b_first_id", "b_last_id", "c_id"])}
