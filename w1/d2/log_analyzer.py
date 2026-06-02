#!/usr/bin/env python
"""
Mini log analyzer for W1 Day-2 Phase 4.

Usage:
    python log_analyzer.py <logfile>
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

import pandas as pd
from drain3 import TemplateMiner
from drain3.template_miner_config import TemplateMinerConfig


HDFS_TS_RE = re.compile(r"^(\d{6})\s+(\d{6})\b")
BGL_TS_RE = re.compile(r"\b(\d{4})\.(\d{4})\.(\d{2})\.(\d{2})\.(\d{2})\b")
BGL_DASH_TS_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})-(\d{2})\.(\d{2})\.(\d{2})(?:\.\d+)?\b")
SPARK_TS_RE = re.compile(r"^(\d{2}/\d{2}/\d{2})\s+(\d{2}:\d{2}:\d{2})\b")
ISO_TS_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})")


def parse_timestamp(line: str) -> pd.Timestamp:
    """Parse common Loghub timestamps. Return NaT if the line has no known timestamp."""
    m = HDFS_TS_RE.search(line)
    if m:
        return pd.to_datetime(m.group(1) + " " + m.group(2), format="%y%m%d %H%M%S", errors="coerce")

    m = BGL_TS_RE.search(line)
    if m:
        year, month_day, hour, minute, second = m.groups()
        month = month_day[:2]
        day = month_day[2:]
        return pd.to_datetime(
            f"{year}-{month}-{day} {hour}:{minute}:{second}",
            errors="coerce",
        )

    m = BGL_DASH_TS_RE.search(line)
    if m:
        date, hour, minute, second = m.groups()
        return pd.to_datetime(f"{date} {hour}:{minute}:{second}", errors="coerce")

    m = SPARK_TS_RE.search(line)
    if m:
        return pd.to_datetime(m.group(1) + " " + m.group(2), format="%y/%m/%d %H:%M:%S", errors="coerce")

    m = ISO_TS_RE.search(line)
    if m:
        return pd.to_datetime(m.group(1) + " " + m.group(2), errors="coerce")

    return pd.NaT


def get_clusters(miner: TemplateMiner):
    clusters = miner.drain.clusters
    if isinstance(clusters, dict):
        return list(clusters.values())
    return list(clusters)


def cluster_template(cluster) -> str:
    if hasattr(cluster, "get_template"):
        return cluster.get_template()
    return " ".join(cluster.log_template)


def iter_lines(path: Path):
    with path.open("r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            yield line.rstrip("\r\n")


def mine_templates(logfile: Path, sim_th: float, depth: int):
    config = TemplateMinerConfig()
    config.drain_sim_th = sim_th
    config.drain_depth = depth
    config.profiling_enabled = False

    miner = TemplateMiner(config=config)
    parsed_rows = []

    for line_id, line in enumerate(iter_lines(logfile)):
        result = miner.add_log_message(line)
        parsed_rows.append(
            {
                "line_id": line_id,
                "timestamp": parse_timestamp(line),
                "template_id": result["cluster_id"],
                "raw_log": line,
            }
        )

    parsed_df = pd.DataFrame(parsed_rows)
    if parsed_df.empty:
        templates_df = pd.DataFrame(columns=["template_id", "template", "count"])
        return miner, parsed_df, templates_df

    parsed_df["timestamp"] = pd.to_datetime(parsed_df["timestamp"])

    templates_df = pd.DataFrame(
        [
            {
                "template_id": cluster.cluster_id,
                "template": cluster_template(cluster),
                "count": cluster.size,
            }
            for cluster in get_clusters(miner)
        ]
    ).sort_values(["count", "template_id"], ascending=[False, True])

    return miner, parsed_df, templates_df


def detect_last_hour_spikes(parsed_df: pd.DataFrame, templates_df: pd.DataFrame):
    valid = parsed_df.dropna(subset=["timestamp"]).copy()
    if valid.empty:
        return pd.DataFrame(), pd.NaT, pd.NaT

    latest_ts = valid["timestamp"].max()
    last_hour_start = latest_ts.floor("h")

    ts = (
        valid.groupby([pd.Grouper(key="timestamp", freq="1h"), "template_id"])
        .size()
        .unstack(fill_value=0)
        .sort_index()
    )

    if last_hour_start not in ts.index:
        last_hour_start = ts.index.max()

    last_counts = ts.loc[last_hour_start]
    history = ts.loc[ts.index < last_hour_start]

    rows = []
    for template_id, last_count in last_counts.items():
        if last_count <= 0:
            continue

        if history.empty:
            avg = 0.0
            std = 0.0
        else:
            avg = float(history[template_id].mean())
            std = float(history[template_id].std(ddof=0))

        if std == 0:
            is_spike = last_count > max(avg * 2, avg + 2)
            z_score = None
        else:
            z_score = (last_count - avg) / std
            is_spike = z_score >= 3.0 and last_count >= 2

        if is_spike:
            rows.append(
                {
                    "template_id": int(template_id),
                    "last_hour_count": int(last_count),
                    "avg_previous_hours": avg,
                    "std_previous_hours": std,
                    "z_score": z_score,
                }
            )

    spikes_df = pd.DataFrame(rows)
    if spikes_df.empty:
        return spikes_df, last_hour_start, latest_ts

    spikes_df = spikes_df.merge(templates_df[["template_id", "template"]], on="template_id", how="left")
    spikes_df = spikes_df.sort_values(["z_score", "last_hour_count"], ascending=[False, False], na_position="last")
    return spikes_df, last_hour_start, latest_ts


def find_new_templates_last_hour(parsed_df: pd.DataFrame, templates_df: pd.DataFrame, last_hour_start):
    valid = parsed_df.dropna(subset=["timestamp"]).copy()
    if valid.empty or pd.isna(last_hour_start):
        return pd.DataFrame()

    first_seen = valid.groupby("template_id", as_index=False).agg(first_seen=("timestamp", "min"))
    new_df = first_seen[first_seen["first_seen"] >= last_hour_start].copy()
    if new_df.empty:
        return new_df

    new_df = new_df.merge(templates_df[["template_id", "template", "count"]], on="template_id", how="left")
    return new_df.sort_values("first_seen")


def print_report(logfile: Path, parsed_df: pd.DataFrame, templates_df: pd.DataFrame):
    total_lines = len(parsed_df)
    unique_templates = len(templates_df)

    print("=" * 80)
    print("Mini Log Analyzer")
    print("=" * 80)
    print(f"Log file: {logfile}")
    print(f"Total lines: {total_lines:,}")
    print(f"Unique templates: {unique_templates:,}")
    print()

    print("Top-5 templates:")
    if templates_df.empty:
        print("  No templates found.")
    else:
        for _, row in templates_df.head(5).iterrows():
            pct = (row["count"] / total_lines * 100) if total_lines else 0
            print(f"  [{int(row['template_id'])}] count={int(row['count']):,} ({pct:.2f}%)")
            print(f"      {row['template']}")
    print()

    spikes_df, last_hour_start, latest_ts = detect_last_hour_spikes(parsed_df, templates_df)
    if pd.isna(last_hour_start):
        print("Last-hour spike detection:")
        print("  Skipped: no recognizable timestamps in this log format.")
        print()
        print("New templates in last hour:")
        print("  Skipped: no recognizable timestamps in this log format.")
        return

    print("Last-hour spike detection:")
    print(f"  Latest timestamp: {latest_ts}")
    print(f"  Last hour window: {last_hour_start} -> {last_hour_start + pd.Timedelta(hours=1)}")
    if spikes_df.empty:
        print("  No template spike detected in the last hour.")
    else:
        for _, row in spikes_df.head(10).iterrows():
            z_text = "n/a" if row["z_score"] is None or pd.isna(row["z_score"]) else f"{row['z_score']:.2f}"
            print(
                f"  [{int(row['template_id'])}] last_hour={int(row['last_hour_count'])}, "
                f"avg_prev={row['avg_previous_hours']:.2f}, z={z_text}"
            )
            print(f"      {row['template']}")
    print()

    new_df = find_new_templates_last_hour(parsed_df, templates_df, last_hour_start)
    print("New templates in last hour:")
    if new_df.empty:
        print("  No new template first appeared in the last hour.")
    else:
        for _, row in new_df.iterrows():
            print(f"  [{int(row['template_id'])}] first_seen={row['first_seen']}, count={int(row['count'])}")
            print(f"      {row['template']}")


def main():
    parser = argparse.ArgumentParser(description="Mini log analyzer using Drain3.")
    parser.add_argument("logfile", type=Path, help="Path to log file")
    parser.add_argument("--sim-th", type=float, default=0.5, help="Drain similarity threshold")
    parser.add_argument("--depth", type=int, default=4, help="Drain tree depth")
    args = parser.parse_args()

    if not args.logfile.exists():
        raise FileNotFoundError(f"Log file not found: {args.logfile}")

    _, parsed_df, templates_df = mine_templates(args.logfile, sim_th=args.sim_th, depth=args.depth)
    print_report(args.logfile, parsed_df, templates_df)


if __name__ == "__main__":
    main()
