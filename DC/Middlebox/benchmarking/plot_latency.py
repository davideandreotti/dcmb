#!/usr/bin/env python3

from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt


CLIENT_REQUEST_START = "client_request_start"
CLIENT_RESPONSE_DONE = "client_response_done"
CLIENT_REQUEST_ERROR = "client_request_error"
CLIENT_TLS_START = "client_tls_start"
CLIENT_TLS_DONE = "client_tls_done"
CLIENT_REQUEST_SENT = "client_request_sent"
CLIENT_RESPONSE_FIRST = "client_response_first_byte"

SAMPLE_FIELDS = [
    "campaign",
    "run_name",
    "deployment",
    "mode",
    "clients",
    "rate",
    "iteration",
    "request_id",
    "elapsed_s",
    "latency_ns",
    "latency_ms",
    "handshake_ms",
    "request_ms",
    "status",
    "steady_state",
    "in_violin",
]

SUMMARY_FIELDS = [
    "campaign",
    "run_name",
    "deployment",
    "mode",
    "clients",
    "offered_rps",
    "achieved_rps",
    "success",
    "failed",
    "p99_latency_ms",
    "mean_cpu_percent",
    "peak_cpu_percent",
    "startup_process_ready_ms",
    "startup_first_worker_ready_ms",
]

SKIP_CPU_ROLES = {"server", "certserver", "client"}


def load_campaign(campaign_dir: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    samples: list[dict[str, Any]] = []
    run_summaries: list[dict[str, Any]] = []

    for run_dir in sorted(path for path in campaign_dir.iterdir() if path.is_dir()):
        metadata_path = run_dir / "metadata.json"
        client_csv = run_dir / "csv" / "client.csv"
        if not metadata_path.exists() or not client_csv.exists():
            continue

        run_samples, run_summary = load_run(campaign_dir, run_dir, metadata_path, client_csv)
        samples.extend(run_samples)
        run_summaries.append(run_summary)

    return samples, run_summaries


def load_run(
    campaign_dir: Path,
    run_dir: Path,
    metadata_path: Path,
    client_csv: Path,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    parameters = metadata.get("parameters", {})
    deployment_info = parameters.get("deployment", {})
    deployment = deployment_info.get("name") or deployment_info.get("kind") or "unknown"
    mode = str(parameters.get("mode", "unknown"))
    clients = parameters.get("clients", "")
    rate = parameters.get("rate", "")
    iteration = parameters.get("iteration", "")
    duration_s = parse_float(parameters.get("duration_s")) or 0.0
    run_name = metadata.get("run_name", run_dir.name)

    samples, counters = extract_client_latencies(
        client_csv=client_csv,
        campaign=campaign_dir.name,
        run_name=run_name,
        deployment=str(deployment),
        mode=mode,
        clients=clients,
        rate=rate,
        iteration=iteration,
        duration_s=duration_s,
    )

    summary = {
        "campaign": campaign_dir.name,
        "run_name": run_name,
        "run_dir": str(run_dir),
        "deployment": str(deployment),
        "mode": mode,
        "clients": clients,
        "rate": rate,
        "iteration": iteration,
        "duration_s": duration_s,
        "window_start_ns": counters["window_start_ns"],
        "window_end_ns": counters["window_end_ns"],
        "steady_duration_s": counters["steady_duration_s"],
        "ok_count": counters["ok"],
        "failed_count": counters["failed"],
        "steady_ok_count": counters["steady_ok"],
        "violin_count": counters["violin"],
    }
    return samples, summary


def extract_client_latencies(
    client_csv: Path,
    campaign: str,
    run_name: str,
    deployment: str,
    mode: str,
    clients: Any,
    rate: Any,
    iteration: Any,
    duration_s: float,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    events_by_id: dict[str, dict[str, Any]] = {}

    with client_csv.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            request_id = row.get("id", "")
            if not request_id:
                continue
            if request_id.startswith("warmup-"):
                continue

            event_name = row.get("event_name", "")
            event = events_by_id.setdefault(request_id, {"errors": 0})
            timestamp = parse_int(row.get("timestamp_ns"))
            arg = parse_int(row.get("arg"))

            if event_name == CLIENT_REQUEST_START:
                event["start_ts"] = timestamp
            elif event_name == CLIENT_RESPONSE_DONE:
                event["done_ts"] = timestamp
                event["response_status"] = arg
            elif event_name == CLIENT_REQUEST_ERROR:
                event["errors"] = event.get("errors", 0) + 1
            elif event_name == CLIENT_TLS_START:
                event["tls_start_ts"] = timestamp
            elif event_name == CLIENT_TLS_DONE:
                event["tls_done_ts"] = timestamp
            elif event_name == CLIENT_REQUEST_SENT:
                event["request_sent_ts"] = timestamp
            elif event_name == CLIENT_RESPONSE_FIRST:
                event["response_first_ts"] = timestamp

    start_times = [event["start_ts"] for event in events_by_id.values() if event.get("start_ts") is not None]
    first_start = min(start_times) if start_times else None
    sorted_starts = sorted(start_times)
    steady_start = sorted_starts[1] if len(sorted_starts) > 1 else None
    window_end = None
    if first_start is not None:
        if duration_s > 0:
            window_end = first_start + int(duration_s * 1_000_000_000)
        elif sorted_starts:
            window_end = max(sorted_starts) + 1

    records: list[dict[str, Any]] = []
    for request_id, event in events_by_id.items():
        start_ts = event.get("start_ts")
        done_ts = event.get("done_ts")
        response_status = event.get("response_status")
        has_error = event.get("errors", 0) > 0
        tls_start_ts = event.get("tls_start_ts")
        tls_done_ts = event.get("tls_done_ts")
        request_sent_ts = event.get("request_sent_ts")
        ok = (
            start_ts is not None
            and done_ts is not None
            and response_status is not None
            and 200 <= response_status < 300
            and not has_error
            and done_ts >= start_ts
        )

        elapsed_s = ""
        if first_start is not None and start_ts is not None:
            elapsed_s = f"{(start_ts - first_start) / 1e9:.9f}"

        latency_ns = ""
        latency_ms = ""
        status = "failed"
        if ok:
            latency_ns_value = done_ts - start_ts
            latency_ns = str(latency_ns_value)
            latency_ms = f"{latency_ns_value / 1e6:.6f}"
            status = "ok"

        handshake_ms = ""
        if ok and start_ts is not None and tls_done_ts is not None and tls_done_ts >= start_ts:
            handshake_ms = f"{(tls_done_ts - start_ts) / 1e6:.6f}"

        request_ms = ""
        if ok and request_sent_ts is not None and done_ts is not None and done_ts >= request_sent_ts:
            request_ms = f"{(done_ts - request_sent_ts) / 1e6:.6f}"

        steady_state = (
            ok
            and steady_start is not None
            and window_end is not None
            and start_ts is not None
            and steady_start <= start_ts < window_end
        )

        records.append(
            {
                "campaign": campaign,
                "run_name": run_name,
                "deployment": deployment,
                "mode": mode,
                "clients": clients,
                "rate": rate,
                "iteration": iteration,
                "request_id": request_id,
                "elapsed_s": elapsed_s,
                "latency_ns": latency_ns,
                "latency_ms": latency_ms,
                "handshake_ms": handshake_ms,
                "request_ms": request_ms,
                "status": status,
                "steady_state": steady_state,
                "in_violin": False,
                "_start_ts": start_ts if start_ts is not None else 0,
            }
        )

    records.sort(key=lambda row: (row["_start_ts"], row["request_id"]))
    mark_violin_samples(records, mode)

    counters = {
        "ok": sum(1 for row in records if row["status"] == "ok"),
        "failed": sum(1 for row in records if row["status"] != "ok"),
        "steady_ok": sum(1 for row in records if row["steady_state"]),
        "violin": sum(1 for row in records if row["in_violin"]),
        "window_start_ns": steady_start or 0,
        "window_end_ns": window_end or 0,
        "steady_duration_s": ((window_end - steady_start) / 1_000_000_000) if steady_start and window_end else 0.0,
    }

    for row in records:
        row["steady_state"] = "true" if row["steady_state"] else "false"
        row["in_violin"] = "true" if row["in_violin"] else "false"
        row.pop("_start_ts", None)

    return records, counters


def mark_violin_samples(records: list[dict[str, Any]], mode: str) -> None:
    for row in records:
        row["in_violin"] = row["steady_state"] is True


def logical_client_id(request_id: str) -> str:
    prefix, sep, suffix = request_id.rpartition("-")
    if sep and suffix.isdigit():
        return prefix
    return request_id


def parse_int(value: Any) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def parse_float(value: Any) -> float | None:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def write_samples_csv(samples: list[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=SAMPLE_FIELDS)
        writer.writeheader()
        for sample in samples:
            writer.writerow({field: sample.get(field, "") for field in SAMPLE_FIELDS})


def write_summary_csv(rows: list[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=SUMMARY_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in SUMMARY_FIELDS})


def build_run_summary_rows(samples: list[dict[str, Any]], run_summaries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    samples_by_run = group_by(samples, "run_name")
    rows: list[dict[str, Any]] = []
    for summary in run_summaries:
        run_samples = samples_by_run.get(str(summary["run_name"]), [])
        steady_ok = [
            sample
            for sample in run_samples
            if sample["status"] == "ok" and sample["steady_state"] == "true" and sample["latency_ms"] != ""
        ]
        steady_duration_s = float(summary.get("steady_duration_s") or 0.0)
        achieved_rps = len(steady_ok) / steady_duration_s if steady_duration_s > 0 else 0.0
        latencies = [float(sample["latency_ms"]) for sample in steady_ok]
        cpu_points = cpu_points_for_run(summary)
        cpu_values = [
            value
            for ts_ns, value in cpu_points
            if in_window(ts_ns, int(summary.get("window_start_ns") or 0), int(summary.get("window_end_ns") or 0))
        ]
        startup = startup_metrics_for_run(summary)

        rows.append(
            {
                "campaign": summary["campaign"],
                "run_name": summary["run_name"],
                "deployment": summary["deployment"],
                "mode": summary["mode"],
                "clients": summary["clients"],
                "offered_rps": f"{float(summary['rate']):.6f}" if parse_float(summary["rate"]) is not None else summary["rate"],
                "achieved_rps": f"{achieved_rps:.6f}",
                "success": len(steady_ok),
                "failed": summary["failed_count"],
                "p99_latency_ms": f"{percentile(latencies, 99):.6f}" if latencies else "",
                "mean_cpu_percent": f"{mean(cpu_values):.6f}" if cpu_values else "",
                "peak_cpu_percent": f"{max(cpu_values):.6f}" if cpu_values else "",
                "startup_process_ready_ms": startup.get("process_ready_ms", ""),
                "startup_first_worker_ready_ms": startup.get("first_worker_ready_ms", ""),
            }
        )
    return rows


def cpu_points_for_run(summary: dict[str, Any]) -> list[tuple[int, float]]:
    run_dir = Path(str(summary["run_dir"]))
    deployment = str(summary["deployment"])
    if deployment == "direct":
        return []

    containers_total = run_dir / "cpu" / "containers_total.csv"
    if containers_total.exists():
        return docker_cpu_points(containers_total)

    processes = run_dir / "cpu" / "processes.csv"
    if processes.exists():
        return process_cpu_points(processes)

    return []


def memory_points_for_run(summary: dict[str, Any]) -> list[tuple[int, float]]:
    run_dir = Path(str(summary["run_dir"]))
    deployment = str(summary["deployment"])
    if deployment == "direct":
        return []

    containers_total = run_dir / "cpu" / "containers_total.csv"
    if containers_total.exists():
        return docker_memory_points(containers_total)

    processes = run_dir / "cpu" / "processes.csv"
    if processes.exists():
        return process_memory_points(processes)

    return []


def docker_cpu_points(path: Path) -> list[tuple[int, float]]:
    points: list[tuple[int, float]] = []
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            ts = parse_int(row.get("ts_ns"))
            value = parse_float(row.get("cpu_perc_sum"))
            if ts is None or value is None:
                continue
            points.append((ts, value))
    return points


def docker_memory_points(path: Path) -> list[tuple[int, float]]:
    points: list[tuple[int, float]] = []
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            ts = parse_int(row.get("ts_ns"))
            value = parse_float(row.get("mem_usage_bytes_sum"))
            if ts is None or value is None:
                continue
            points.append((ts, value))
    return points


def process_cpu_points(path: Path) -> list[tuple[int, float]]:
    rows_by_role: dict[str, list[dict[str, Any]]] = {}
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            role = str(row.get("role", ""))
            if role in SKIP_CPU_ROLES:
                continue
            ts = parse_int(row.get("ts_ns"))
            user = parse_float(row.get("user_time_s"))
            system = parse_float(row.get("system_time_s"))
            if ts is None or user is None or system is None:
                continue
            rows_by_role.setdefault(role, []).append({"ts_ns": ts, "cpu_time_s": user + system})

    points: list[tuple[int, float]] = []
    for rows in rows_by_role.values():
        rows.sort(key=lambda row: row["ts_ns"])
        previous: dict[str, Any] | None = None
        for row in rows:
            if previous is not None:
                dt_s = (row["ts_ns"] - previous["ts_ns"]) / 1_000_000_000
                dcpu_s = row["cpu_time_s"] - previous["cpu_time_s"]
                if dt_s > 0 and dcpu_s >= 0:
                    points.append((row["ts_ns"], (dcpu_s / dt_s) * 100.0))
            previous = row
    points.sort(key=lambda item: item[0])
    return points


def process_memory_points(path: Path) -> list[tuple[int, float]]:
    points: list[tuple[int, float]] = []
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            role = str(row.get("role", ""))
            if role in SKIP_CPU_ROLES:
                continue
            ts = parse_int(row.get("ts_ns"))
            value = parse_float(row.get("rss_tree_bytes")) or parse_float(row.get("rss_bytes"))
            if ts is None or value is None:
                continue
            points.append((ts, value))
    points.sort(key=lambda item: item[0])
    return points


def startup_metrics_for_run(summary: dict[str, Any]) -> dict[str, str]:
    path = Path(str(summary["run_dir"])) / "csv" / "startup.csv"
    if not path.exists():
        return {}

    metrics: dict[str, str] = {}
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            metric = row.get("metric", "")
            role = row.get("role", "")
            startup_ms = row.get("startup_ms", "")
            if metric == "process_ready" and role in {"middlebox", "gateway"} and startup_ms:
                metrics.setdefault("process_ready_ms", startup_ms)
            elif metric == "first_worker_ready" and startup_ms:
                metrics.setdefault("first_worker_ready_ms", startup_ms)
    return metrics


def worker_startup_values(run_summaries: list[dict[str, Any]]) -> dict[str, list[float]]:
    values: dict[str, list[float]] = {}
    for summary in run_summaries:
        path = Path(str(summary["run_dir"])) / "csv" / "startup.csv"
        if not path.exists():
            continue
        deployment = str(summary["deployment"])
        with path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                if row.get("metric") != "worker_ready_internal":
                    continue
                value = parse_float(row.get("startup_ms"))
                if value is not None:
                    values.setdefault(deployment, []).append(value)
    return values


def in_window(ts_ns: int, start_ns: int, end_ns: int) -> bool:
    if start_ns <= 0 or end_ns <= 0:
        return True
    return start_ns <= ts_ns < end_ns


def mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def moving_average_by_time(xs: list[float], ys: list[float], window_s: float) -> tuple[list[float], list[float]]:
    if not xs or window_s <= 0:
        return xs, ys

    ordered = sorted(zip(xs, ys), key=lambda item: item[0])
    ma_xs: list[float] = []
    ma_ys: list[float] = []
    left = 0
    window_sum = 0.0

    for right, (x_value, y_value) in enumerate(ordered):
        window_sum += y_value
        while left <= right and x_value - ordered[left][0] > window_s:
            window_sum -= ordered[left][1]
            left += 1

        count = right - left + 1
        ma_xs.append(x_value)
        ma_ys.append(window_sum / count)

    return ma_xs, ma_ys


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    if p <= 0:
        return min(values)
    if p >= 100:
        return max(values)

    ordered = sorted(values)
    position = (len(ordered) - 1) * (p / 100.0)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[int(position)]

    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def plot_timeseries(samples: list[dict[str, Any]], run_summaries: list[dict[str, Any]], output_path: Path) -> None:
    figure_size_per_subplot = (6.0, 3.2)
    y_label = "End-to-end latency (ms)"
    x_label = "Experiment elapsed time (s)"
    dot_color = "#8ecae6"
    moving_average_color = "#023047"
    moving_average_window_s = 1.0
    dot_size = 7.0
    dot_alpha = 0.55
    moving_average_width = 1.4
    show_failure_annotations = True  # comment this block locally if not desired

    runs = [summary for summary in run_summaries if summary["ok_count"] > 0 or summary["failed_count"] > 0]
    if not runs:
        print("[PLOT] no runs with client samples for time-series plot")
        return

    ncols = 1 if len(runs) <= 2 else 2
    nrows = math.ceil(len(runs) / ncols)
    fig_width = figure_size_per_subplot[0] * ncols
    fig_height = figure_size_per_subplot[1] * nrows
    fig, axes = plt.subplots(nrows, ncols, figsize=(fig_width, fig_height), squeeze=False)
    axes_flat = list(axes.flatten())

    samples_by_run = group_by(samples, "run_name")
    for ax, summary in zip(axes_flat, runs):
        run_samples = [
            sample
            for sample in samples_by_run.get(summary["run_name"], [])
            if sample["status"] == "ok" and sample["latency_ms"] != ""
        ]
        failed_samples = [
            sample
            for sample in samples_by_run.get(summary["run_name"], [])
            if sample["status"] != "ok" and sample["elapsed_s"] != ""
        ]
        xs = [float(sample["elapsed_s"]) for sample in run_samples]
        ys = [float(sample["latency_ms"]) for sample in run_samples]

        ax.scatter(xs, ys, s=dot_size, alpha=dot_alpha, color=dot_color, edgecolors="none")
        ma_xs, ma_ys = moving_average_by_time(xs, ys, moving_average_window_s)
        if ma_xs:
            ax.plot(ma_xs, ma_ys, color=moving_average_color, linewidth=moving_average_width)

        if ys:
            mean_latency = sum(ys) / len(ys)
            ax.axhline(mean_latency, color=moving_average_color, linewidth=0.8, alpha=0.45, linestyle="--")
            ax.text(
                1.01,
                mean_latency,
                f"avg {mean_latency:.2f} ms",
                transform=ax.get_yaxis_transform(),
                ha="left",
                va="center",
                color=moving_average_color,
                fontsize=8,
                clip_on=False,
            )

        ax.set_title(run_label(summary), fontsize=10)
        ax.set_xlabel(x_label)
        ax.set_ylabel(y_label)
        ax.grid(True, alpha=0.25)
        ax.set_ylim(bottom=0)

        if show_failure_annotations and summary["failed_count"] > 0:
            y_top = ax.get_ylim()[1] if ax.get_ylim()[1] > 0 else 1.0
            failed_xs = [float(sample["elapsed_s"]) for sample in failed_samples]
            if failed_xs:
                ax.plot(
                    failed_xs,
                    [y_top * 0.985 for _ in failed_xs],
                    linestyle="",
                    marker="|",
                    markersize=8,
                    markeredgewidth=1.2,
                    color="red",
                )
            ax.text(
                0.98,
                0.92,
                f"failed: {summary['failed_count']}",
                transform=ax.transAxes,
                ha="right",
                va="top",
                color="red",
                fontsize=9,
            )

    for ax in axes_flat[len(runs) :]:
        ax.axis("off")

    fig.tight_layout()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, bbox_inches="tight")
    plt.close(fig)


def plot_violin(samples: list[dict[str, Any]], run_summaries: list[dict[str, Any]], output_path: Path) -> None:
    figure_size = (8.0, 5.0)
    y_label = "End-to-end latency (ms)"
    title = "End-to-end latency by deployment"
    violin_color = "#77aadd"
    mean_color = "#023047"
    percentile_clip = 99  # set to 99 to clip each violin at p99
    show_failure_annotations = True  # comment this block locally if not desired

    group_order: list[tuple[str, str]] = []
    summaries_by_group: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for summary in run_summaries:
        group = (str(summary["deployment"]), str(summary["mode"]))
        if group not in summaries_by_group:
            group_order.append(group)
            summaries_by_group[group] = []
        summaries_by_group[group].append(summary)

    values_by_group: dict[tuple[str, str], list[float]] = {group: [] for group in group_order}
    for sample in samples:
        if sample["status"] != "ok" or sample["in_violin"] != "true" or sample["latency_ms"] == "":
            continue
        group = (str(sample["deployment"]), str(sample["mode"]))
        if group not in values_by_group:
            group_order.append(group)
            values_by_group[group] = []
        values_by_group[group].append(float(sample["latency_ms"]))

    groups_with_data = [group for group in group_order if values_by_group.get(group)]
    if not groups_with_data:
        print("[PLOT] no valid samples for violin plot")
        return

    data: list[list[float]] = []
    group_means: list[float] = []
    for group in groups_with_data:
        values = values_by_group[group]
        if percentile_clip is not None:
            cutoff = percentile(values, percentile_clip)
            values = [value for value in values if value <= cutoff]
        data.append(values)
        group_means.append(sum(values) / len(values))

    labels = [f"{deployment}\n{mode}" for deployment, mode in groups_with_data]

    fig, ax = plt.subplots(figsize=figure_size)
    parts = ax.violinplot(data, showmeans=True, showmedians=True)
    for body in parts["bodies"]:
        body.set_facecolor(violin_color)
        body.set_edgecolor("none")
        body.set_alpha(0.65)
    for key in ("cmeans", "cmedians", "cbars", "cmins", "cmaxes"):
        if key in parts:
            parts[key].set_color(mean_color if key == "cmeans" else "#666666")
            parts[key].set_linewidth(1.0)

    ax.set_xticks(range(1, len(labels) + 1))
    ax.set_xticklabels(labels)
    ax.set_ylabel(y_label)
    if percentile_clip is not None:
        title = f"{title} (violin samples clipped at p{percentile_clip:g})"
    ax.set_title(title)
    ax.grid(True, axis="y", alpha=0.25)

    for index, mean_latency in enumerate(group_means, start=1):
        ax.text(
            index + 0.18,
            mean_latency,
            f"avg {mean_latency:.2f} ms",
            ha="left",
            va="center",
            color=mean_color,
            fontsize=8,
            clip_on=False,
        )

    if show_failure_annotations:
        y_top = ax.get_ylim()[1]
        for index, group in enumerate(groups_with_data, start=1):
            failed = sum(summary["failed_count"] for summary in summaries_by_group.get(group, []))
            if failed > 0:
                ax.text(index, y_top, f"failed: {failed}", ha="center", va="top", color="red", fontsize=9)

    fig.tight_layout()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, bbox_inches="tight")
    plt.close(fig)


def plot_throughput_offered_achieved(summary_rows: list[dict[str, Any]], output_path: Path) -> None:
    figure_size = (7.0, 4.5)
    grouped = aggregate_metric_by_rate(summary_rows, "achieved_rps", include_direct=True)
    if not grouped:
        print("[PLOT] no throughput summary data")
        return

    fig, ax = plt.subplots(figsize=figure_size)
    for group, points in grouped.items():
        xs = [point[0] for point in points]
        ys = [point[1] for point in points]
        ax.plot(xs, ys, marker="o", linewidth=1.4, label=group_label(group))

    ax.set_xlabel("Offered throughput (requests/s)")
    ax.set_ylabel("Achieved throughput (requests/s)")
    ax.set_title("Offered vs achieved throughput")
    ax.grid(True, alpha=0.25)
    ax.legend(fontsize=8)
    fig.tight_layout()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, bbox_inches="tight")
    plt.close(fig)


def plot_latency_vs_offered(summary_rows: list[dict[str, Any]], output_path: Path) -> None:
    figure_size = (7.0, 4.5)
    grouped = aggregate_metric_by_rate(summary_rows, "p99_latency_ms", include_direct=True)
    if not grouped:
        print("[PLOT] no latency summary data")
        return

    fig, ax = plt.subplots(figsize=figure_size)
    for group, points in grouped.items():
        xs = [point[0] for point in points]
        ys = [point[1] for point in points]
        ax.plot(xs, ys, marker="o", linewidth=1.4, label=group_label(group))

    ax.set_xlabel("Offered throughput (requests/s)")
    ax.set_ylabel("p99 end-to-end latency (ms)")
    ax.set_title("Offered throughput vs p99 latency")
    ax.grid(True, alpha=0.25)
    ax.legend(fontsize=8)
    fig.tight_layout()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, bbox_inches="tight")
    plt.close(fig)


def plot_cpu_timeseries(run_summaries: list[dict[str, Any]], output_path: Path) -> None:
    figure_size_per_subplot = (6.0, 3.0)
    runs_with_cpu = [(summary, cpu_points_for_run(summary)) for summary in run_summaries if summary["deployment"] != "direct"]
    runs_with_cpu = [(summary, points) for summary, points in runs_with_cpu if points]
    if not runs_with_cpu:
        print("[PLOT] no CPU samples for CPU time-series plot")
        return

    ncols = 1 if len(runs_with_cpu) <= 2 else 2
    nrows = math.ceil(len(runs_with_cpu) / ncols)
    fig, axes = plt.subplots(
        nrows,
        ncols,
        figsize=(figure_size_per_subplot[0] * ncols, figure_size_per_subplot[1] * nrows),
        squeeze=False,
    )
    axes_flat = list(axes.flatten())

    for ax, (summary, points) in zip(axes_flat, runs_with_cpu):
        first_ts = points[0][0]
        xs = [(ts - first_ts) / 1_000_000_000 for ts, _ in points]
        ys = [value for _, value in points]
        ax.plot(xs, ys, color="#2a9d8f", linewidth=1.1)
        ax.set_title(run_label(summary), fontsize=10)
        ax.set_xlabel("Elapsed time (s)")
        ax.set_ylabel("CPU (%)")
        ax.grid(True, alpha=0.25)
        ax.set_ylim(bottom=0)

    for ax in axes_flat[len(runs_with_cpu) :]:
        ax.axis("off")

    fig.tight_layout()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, bbox_inches="tight")
    plt.close(fig)


def plot_memory_timeseries(run_summaries: list[dict[str, Any]], output_path: Path) -> None:
    figure_size_per_subplot = (6.0, 3.0)
    runs_with_memory = [
        (summary, memory_points_for_run(summary)) for summary in run_summaries if summary["deployment"] != "direct"
    ]
    runs_with_memory = [(summary, points) for summary, points in runs_with_memory if points]
    if not runs_with_memory:
        print("[PLOT] no memory samples for memory time-series plot")
        return

    ncols = 1 if len(runs_with_memory) <= 2 else 2
    nrows = math.ceil(len(runs_with_memory) / ncols)
    fig, axes = plt.subplots(
        nrows,
        ncols,
        figsize=(figure_size_per_subplot[0] * ncols, figure_size_per_subplot[1] * nrows),
        squeeze=False,
    )
    axes_flat = list(axes.flatten())

    for ax, (summary, points) in zip(axes_flat, runs_with_memory):
        first_ts = points[0][0]
        xs = [(ts - first_ts) / 1_000_000_000 for ts, _ in points]
        ys = [value / (1024 * 1024) for _, value in points]
        ax.plot(xs, ys, color="#6a4c93", linewidth=1.1)
        ax.set_title(run_label(summary), fontsize=10)
        ax.set_xlabel("Elapsed time (s)")
        ax.set_ylabel("Memory (MiB)")
        ax.grid(True, alpha=0.25)
        ax.set_ylim(bottom=0)

    for ax in axes_flat[len(runs_with_memory) :]:
        ax.axis("off")

    fig.tight_layout()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, bbox_inches="tight")
    plt.close(fig)


def plot_cpu_vs_offered(summary_rows: list[dict[str, Any]], output_path: Path) -> None:
    figure_size = (7.0, 4.5)
    grouped = aggregate_metric_by_rate(summary_rows, "mean_cpu_percent", include_direct=False)
    if not grouped:
        print("[PLOT] no CPU summary data")
        return

    fig, ax = plt.subplots(figsize=figure_size)
    for group, points in grouped.items():
        xs = [point[0] for point in points]
        ys = [point[1] for point in points]
        ax.plot(xs, ys, marker="o", linewidth=1.4, label=group_label(group))

    ax.set_xlabel("Offered throughput (requests/s)")
    ax.set_ylabel("Mean middlebox CPU (%)")
    ax.set_title("Offered throughput vs middlebox CPU")
    ax.grid(True, alpha=0.25)
    ax.legend(fontsize=8)
    fig.tight_layout()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, bbox_inches="tight")
    plt.close(fig)


def plot_startup_times(run_summaries: list[dict[str, Any]], output_path: Path) -> None:
    figure_size = (8.0, 4.8)
    values_by_label: dict[str, list[float]] = {}
    for summary in run_summaries:
        deployment = str(summary["deployment"])
        if deployment == "direct":
            continue
        startup = startup_metrics_for_run(summary)
        process_ready = parse_float(startup.get("process_ready_ms"))
        first_worker = parse_float(startup.get("first_worker_ready_ms"))
        if process_ready is not None:
            label = f"{deployment}\nready"
            values_by_label.setdefault(label, []).append(process_ready)
        if first_worker is not None:
            label = f"{deployment}\nfirst worker"
            values_by_label.setdefault(label, []).append(first_worker)

    if not values_by_label:
        print("[PLOT] no startup data")
        return

    labels = list(values_by_label)
    means = [mean(values_by_label[label]) for label in labels]
    errors = [confidence_interval_95(values_by_label[label]) for label in labels]

    fig, ax = plt.subplots(figsize=figure_size)
    xs = list(range(len(labels)))
    ax.bar(xs, means, yerr=errors, color="#8ecae6", edgecolor="#023047", linewidth=0.8, capsize=4)
    for x, label in zip(xs, labels):
        values = values_by_label[label]
        jitter_step = 0.06
        start_offset = -jitter_step * (len(values) - 1) / 2
        for index, value in enumerate(values):
            ax.plot(x + start_offset + index * jitter_step, value, marker="o", color="#023047", markersize=3)

    ax.set_xticks(xs)
    ax.set_xticklabels(labels)
    ax.set_ylabel("Startup time (ms)")
    ax.set_title("Middlebox startup time")
    ax.grid(True, axis="y", alpha=0.25)
    ax.set_ylim(bottom=0)
    fig.tight_layout()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, bbox_inches="tight")
    plt.close(fig)


def plot_worker_startup_distribution(run_summaries: list[dict[str, Any]], output_path: Path) -> None:
    values_by_deployment = worker_startup_values(run_summaries)
    groups = [deployment for deployment, values in values_by_deployment.items() if values]
    if not groups:
        print("[PLOT] no Docker worker startup data")
        return

    data = [values_by_deployment[group] for group in groups]
    fig, ax = plt.subplots(figsize=(7.0, 4.5))
    parts = ax.violinplot(data, showmeans=True, showmedians=True)
    for body in parts["bodies"]:
        body.set_facecolor("#90be6d")
        body.set_edgecolor("none")
        body.set_alpha(0.65)
    for key in ("cmeans", "cmedians", "cbars", "cmins", "cmaxes"):
        if key in parts:
            parts[key].set_color("#31572c")
            parts[key].set_linewidth(1.0)

    ax.set_xticks(range(1, len(groups) + 1))
    ax.set_xticklabels(groups)
    ax.set_ylabel("Worker startup time (ms)")
    ax.set_title("Docker worker startup distribution")
    ax.grid(True, axis="y", alpha=0.25)
    ax.set_ylim(bottom=0)
    fig.tight_layout()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, bbox_inches="tight")
    plt.close(fig)


def plot_handshake_request_duration(
    samples: list[dict[str, Any]],
    run_summaries: list[dict[str, Any]],
    output_path: Path,
) -> None:
    figure_size_per_mode = (8.0, 4.0)
    modes = ordered_unique(str(summary["mode"]) for summary in run_summaries)
    mode_data: dict[str, dict[str, dict[str, list[float]]]] = {}

    for sample in samples:
        if sample["status"] != "ok":
            continue
        deployment = str(sample["deployment"])
        mode = str(sample["mode"])
        bucket = mode_data.setdefault(mode, {}).setdefault(deployment, {"handshake": [], "request": []})
        handshake = parse_float(sample.get("handshake_ms"))
        request = parse_float(sample.get("request_ms"))
        if handshake is not None:
            bucket["handshake"].append(handshake)
        if sample.get("steady_state") == "true" and request is not None:
            bucket["request"].append(request)

    mode_data = {mode: data for mode, data in mode_data.items() if data}
    if not mode_data:
        print("[PLOT] no handshake/request duration data")
        return

    fig, axes = plt.subplots(
        len(mode_data),
        1,
        figsize=(figure_size_per_mode[0], figure_size_per_mode[1] * len(mode_data)),
        squeeze=False,
    )
    axes_flat = list(axes.flatten())

    for ax, mode in zip(axes_flat, mode_data):
        deployments = list(mode_data[mode])
        xs = list(range(len(deployments)))
        width = 0.34
        handshake_means = [mean(mode_data[mode][deployment]["handshake"]) for deployment in deployments]
        request_means = [mean(mode_data[mode][deployment]["request"]) for deployment in deployments]
        ax.bar([x - width / 2 for x in xs], handshake_means, width=width, label="handshake", color="#f4a261")
        ax.bar([x + width / 2 for x in xs], request_means, width=width, label="request", color="#2a9d8f")
        ax.set_xticks(xs)
        ax.set_xticklabels(deployments)
        ax.set_ylabel("Duration (ms)")
        ax.set_title(f"Handshake and request duration ({mode})")
        ax.grid(True, axis="y", alpha=0.25)
        ax.set_ylim(bottom=0)
        ax.legend(fontsize=8)

    fig.tight_layout()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, bbox_inches="tight")
    plt.close(fig)


def aggregate_metric_by_rate(
    summary_rows: list[dict[str, Any]],
    metric: str,
    include_direct: bool,
) -> dict[tuple[str, str], list[tuple[float, float]]]:
    grouped_values: dict[tuple[str, str], dict[float, list[float]]] = {}
    for row in summary_rows:
        deployment = str(row["deployment"])
        if not include_direct and deployment == "direct":
            continue
        offered = parse_float(row.get("offered_rps"))
        value = parse_float(row.get(metric))
        if offered is None or offered <= 0 or value is None:
            continue
        group = (deployment, str(row["mode"]))
        grouped_values.setdefault(group, {}).setdefault(offered, []).append(value)

    result: dict[tuple[str, str], list[tuple[float, float]]] = {}
    for group, values_by_rate in grouped_values.items():
        result[group] = [(rate, mean(values)) for rate, values in sorted(values_by_rate.items()) if values]
    return {group: points for group, points in result.items() if points}


def confidence_interval_95(values: list[float]) -> float:
    if len(values) <= 1:
        return 0.0
    avg = mean(values)
    variance = sum((value - avg) ** 2 for value in values) / (len(values) - 1)
    return 1.96 * math.sqrt(variance) / math.sqrt(len(values))


def ordered_unique(values: Any) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value)
        if text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def group_label(group: tuple[str, str]) -> str:
    deployment, mode = group
    return f"{deployment} | {mode}"


def group_by(rows: list[dict[str, Any]], key: str) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row[key]), []).append(row)
    return grouped


def run_label(summary: dict[str, Any]) -> str:
    return (
        f"{summary['deployment']} | {summary['mode']} | "
        f"clients={summary['clients']} | rate={summary['rate']} | run={summary['iteration']}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Plot end-to-end latency from a benchmark campaign")
    parser.add_argument("campaign_dir", type=Path, help="campaign directory created by benchmarking/run.py")
    parser.add_argument("--out-dir", type=Path, default=None, help="output directory; default: <campaign>/plots")
    args = parser.parse_args()

    campaign_dir = args.campaign_dir.resolve()
    out_dir = args.out_dir.resolve() if args.out_dir else campaign_dir / "plots"
    timeseries_filename = "e2e_timeseries.pdf"
    violin_filename = "e2e_violin.pdf"
    samples_filename = "e2e_latency_samples.csv"
    summary_filename = "run_summary.csv"

    samples, run_summaries = load_campaign(campaign_dir)
    if not run_summaries:
        raise SystemExit(f"no runs with metadata/client.csv found under {campaign_dir}")

    summary_rows = build_run_summary_rows(samples, run_summaries)
    write_samples_csv(samples, out_dir / samples_filename)
    write_summary_csv(summary_rows, out_dir / summary_filename)
    plot_timeseries(samples, run_summaries, out_dir / timeseries_filename)
    plot_violin(samples, run_summaries, out_dir / violin_filename)
    plot_throughput_offered_achieved(summary_rows, out_dir / "throughput_offered_achieved.pdf")
    plot_latency_vs_offered(summary_rows, out_dir / "latency_vs_offered.pdf")
    plot_cpu_timeseries(run_summaries, out_dir / "cpu_timeseries.pdf")
    plot_memory_timeseries(run_summaries, out_dir / "memory_timeseries.pdf")
    plot_cpu_vs_offered(summary_rows, out_dir / "cpu_vs_offered.pdf")
    plot_startup_times(run_summaries, out_dir / "startup_times.pdf")
    plot_worker_startup_distribution(run_summaries, out_dir / "worker_startup_distribution.pdf")
    plot_handshake_request_duration(samples, run_summaries, out_dir / "handshake_request_duration.pdf")

    print(f"[PLOT] wrote {out_dir / samples_filename}")
    print(f"[PLOT] wrote {out_dir / summary_filename}")
    print(f"[PLOT] wrote {out_dir / timeseries_filename}")
    print(f"[PLOT] wrote {out_dir / violin_filename}")


if __name__ == "__main__":
    main()
