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
    "status",
    "in_violin",
]


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
        "ok_count": counters["ok"],
        "failed_count": counters["failed"],
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
) -> tuple[list[dict[str, Any]], dict[str, int]]:
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

    start_times = [event["start_ts"] for event in events_by_id.values() if event.get("start_ts") is not None]
    first_start = min(start_times) if start_times else None

    records: list[dict[str, Any]] = []
    for request_id, event in events_by_id.items():
        start_ts = event.get("start_ts")
        done_ts = event.get("done_ts")
        response_status = event.get("response_status")
        has_error = event.get("errors", 0) > 0
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
                "status": status,
                "in_violin": False,
                "_start_ts": start_ts if start_ts is not None else 0,
            }
        )

    records.sort(key=lambda row: (row["_start_ts"], row["request_id"]))
    mark_violin_samples(records, mode)

    counters = {
        "ok": sum(1 for row in records if row["status"] == "ok"),
        "failed": sum(1 for row in records if row["status"] != "ok"),
        "violin": sum(1 for row in records if row["in_violin"]),
    }

    for row in records:
        row["in_violin"] = "true" if row["in_violin"] else "false"
        row.pop("_start_ts", None)

    return records, counters


def mark_violin_samples(records: list[dict[str, Any]], mode: str) -> None:
    if mode != "persistent":
        for row in records:
            row["in_violin"] = row["status"] == "ok"
        return

    seen_clients: set[str] = set()
    for row in records:
        if row["status"] != "ok":
            row["in_violin"] = False
            continue

        logical_client = logical_client_id(str(row["request_id"]))
        if logical_client in seen_clients:
            row["in_violin"] = True
        else:
            row["in_violin"] = False
            seen_clients.add(logical_client)


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


def write_samples_csv(samples: list[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=SAMPLE_FIELDS)
        writer.writeheader()
        for sample in samples:
            writer.writerow({field: sample.get(field, "") for field in SAMPLE_FIELDS})


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

        if show_failure_annotations and summary["failed_count"] > 0:
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

    samples, run_summaries = load_campaign(campaign_dir)
    if not run_summaries:
        raise SystemExit(f"no runs with metadata/client.csv found under {campaign_dir}")

    write_samples_csv(samples, out_dir / samples_filename)
    plot_timeseries(samples, run_summaries, out_dir / timeseries_filename)
    plot_violin(samples, run_summaries, out_dir / violin_filename)

    print(f"[PLOT] wrote {out_dir / samples_filename}")
    print(f"[PLOT] wrote {out_dir / timeseries_filename}")
    print(f"[PLOT] wrote {out_dir / violin_filename}")


if __name__ == "__main__":
    main()
