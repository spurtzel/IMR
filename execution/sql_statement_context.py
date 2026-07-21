from __future__ import annotations

import re
from pathlib import Path
from typing import Iterator

from sql_op_id import extract_op_id, extract_validation_event


def classify(stmt: str) -> str:
    s = re.sub(r"\s+", " ", stmt.strip()).lower()
    if not s:
        return "empty"
    if "diff_strat_minus_base" in s or "diff_base_minus_strat" in s:
        return "correctness"
    if " match_recognize " in f" {s} ":
        return "query"
    if s.startswith("create table generated_events") or s.startswith("create table bench_results"):
        return "dataset"
    if s.startswith("select if(") and "generated_events_meta" in s:
        return "dataset"
    if s.startswith("insert into events"):
        return "load"
    if s.startswith("insert into cache_"):
        return "update"
    if s.startswith("insert into watermark_state select"):
        return "watermark"
    if s.startswith("create table composed as"):
        return "compose"
    if s.startswith("create table result as"):
        return "post_filter"
    if s.startswith("insert into bench_results"):
        return "bookkeeping"
    if s.startswith("select * from bench_results"):
        return "report"
    if s.startswith("drop table"):
        return "cleanup"
    if s.startswith("create table") or s.startswith("insert into watermark_state"):
        return "setup"
    return "other"


def iter_statements_with_context(path: Path) -> Iterator[dict]:
    """
    Split SQL on semicolons not in single quotes.
    Strips ,,-- ...'' line comments so semicolons in comments don't create statements.
    Tracks ,,-- Strategy Sx'' and ,,-- Batch k'' comments as lightweight context labels.
    Tracks ,,-- op_id: ...'' markers and validation events.
    """

    strategy = "DATA"
    batch = 0
    in_single = False
    stmt_buf: list[str] = []
    current_op_id: str | None = None

    def flush_stmt() -> dict | None:
        nonlocal current_op_id
        stmt = "".join(stmt_buf).strip()
        stmt_buf.clear()
        if not stmt:
            return None
        phase = classify(stmt)
        head = re.sub(r"\s+", " ", stmt)[:120]
        item = {
            "strategy": strategy,
            "batch": batch,
            "phase": phase,
            "stmt": stmt,
            "stmt_head": head,
            "op_id": current_op_id,
            "validation_event": None,
        }
        current_op_id = None
        return item

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.rstrip("\n")

        m = re.match(r"^\s*--\s*Strategy\s+(\S+)", line)
        if m:
            strategy = m.group(1)
            batch = 0
            continue

        m = re.match(r"^\s*--\s*Batch\s+(\d+)\b", line)
        if m:
            batch = int(m.group(1))
            continue

        marker_op_id = extract_op_id(line)
        if marker_op_id is not None:
            current_op_id = marker_op_id
            continue

        validation_event = extract_validation_event(line)
        if validation_event is not None:
            yield {
                "strategy": strategy,
                "batch": batch,
                "phase": "validation",
                "stmt": "",
                "stmt_head": "",
                "op_id": None,
                "validation_event": validation_event,
            }
            continue

        i = 0
        while i < len(line):
            ch = line[i]
            nxt = line[i + 1] if i + 1 < len(line) else ""

            if not in_single and ch == "-" and nxt == "-":
                break

            if ch == "'":
                if in_single and nxt == "'":
                    stmt_buf.append("''")
                    i += 2
                    continue
                in_single = not in_single
                stmt_buf.append(ch)
                i += 1
                continue

            if ch == ";" and not in_single:
                item = flush_stmt()
                if item:
                    yield item
                i += 1
                continue

            stmt_buf.append(ch)
            i += 1

        stmt_buf.append("\n")

    tail = flush_stmt()
    if tail:
        yield tail
