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
CLIENT_TLS_RESUMED = "client_tls_resumed"
GATEWAY_REQUEST_DROPPED = "gateway_request_dropped"

STEADY_STATE_SKIP_REQUESTS = 10

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
    "measurement_window",
    "tls_success",
    "tls_resumed",
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
    "achieved_handshakes_rps",
    "success",
    "handshake_success",
    "resumption_fallbacks",
    "failed",
    "non2xx",
    "errors",
    "timeouts",
    "late_slots",
    "scheduled_rps",
    "scheduled_ratio",
    "quality_flags",
    "configured_clients",
    "participating_clients",
    "required_clients_p99",
    "peak_in_flight",
    "inflight_limit",
    "unfinished_at_schedule_end",
    "gateway_drops",
    "run_status",
    "client_returncode",
    "failure_reason",
    "p99_latency_ms",
    "mean_latency_ms_filtered",
    "mean_cpu_percent",
    "peak_cpu_percent",
    "median_memory_mib",
    "peak_memory_mib",
    "startup_process_ready_ms",
    "startup_first_worker_ready_ms",
]

SKIP_CPU_ROLES = {"server", "certserver", "client"}


def load_campaign(campaign_dir: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    samples: list[dict[str, Any]] = []
    run_summaries: list[dict[str, Any]] = []
    campaign_status = load_campaign_status(campaign_dir / "summary.csv")

    for run_dir in sorted(path for path in campaign_dir.iterdir() if path.is_dir()):
        metadata_path = run_dir / "metadata.json"
        client_csv = run_dir / "csv" / "client.csv"
        if not metadata_path.exists():
            continue

        run_samples, run_summary = load_run(
            campaign_dir,
            run_dir,
            metadata_path,
            client_csv,
            campaign_status.get(run_dir.name, {}),
        )
        samples.extend(run_samples)
        run_summaries.append(run_summary)

    return samples, run_summaries


def load_run(
    campaign_dir: Path,
    run_dir: Path,
    metadata_path: Path,
    client_csv: Path,
    campaign_status: dict[str, str],
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
    client_report = load_client_report(run_dir / "stdout" / "client.log")
    metadata_client = metadata.get("processes", {}).get("client", {})
    client_returncode = parse_int(campaign_status.get("client_returncode"))
    if client_returncode is None:
        client_returncode = parse_int(metadata_client.get("returncode"))
    controller_status = str(campaign_status.get("status", "unknown")).strip().lower() or "unknown"
    failure_reason = str(campaign_status.get("failure_reason", "")).strip()
    if not failure_reason:
        failure_reason = str(metadata.get("failure_reason", "")).strip()
    run_failed = (
        (mode != "startup" and not client_csv.exists())
        or controller_status not in {"ok", "unknown"}
        or client_returncode not in {None, 0}
    )
    run_status = "failed" if run_failed else controller_status

    if client_csv.exists():
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
    else:
        samples = []
        counters = {
            "ok": 0,
            "failed": 0,
            "steady_ok": 0,
            "steady_handshakes": 0,
            "resumption_fallbacks": 0,
            "violin": 0,
            "window_start_ns": 0,
            "window_end_ns": 0,
            "steady_duration_s": 0.0,
        }

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
        "steady_handshake_count": counters["steady_handshakes"],
        "resumption_fallbacks": counters["resumption_fallbacks"],
        "violin_count": counters["violin"],
        "client_report": client_report,
        "run_status": run_status,
        "run_failed": run_failed,
        "client_returncode": client_returncode,
        "failure_reason": failure_reason,
        "gateway_drops": count_trace_event(run_dir / "csv" / "gateway.csv", GATEWAY_REQUEST_DROPPED),
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
                event["tls_done_arg"] = arg
            elif event_name == CLIENT_TLS_RESUMED:
                event["tls_resume_known"] = True
                event["tls_resumed"] = arg == 1
            elif event_name == CLIENT_REQUEST_SENT:
                event["request_sent_ts"] = timestamp
            elif event_name == CLIENT_RESPONSE_FIRST:
                event["response_first_ts"] = timestamp

    start_times = [event["start_ts"] for event in events_by_id.values() if event.get("start_ts") is not None]
    first_start = min(start_times) if start_times else None
    sorted_starts = sorted(start_times)
    steady_start = (
        sorted_starts[STEADY_STATE_SKIP_REQUESTS]
        if len(sorted_starts) > STEADY_STATE_SKIP_REQUESTS
        else None
    )
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
        tls_success = tls_done_ts is not None and event.get("tls_done_arg") == 0
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

        measurement_window = (
            steady_start is not None
            and window_end is not None
            and start_ts is not None
            and steady_start <= start_ts < window_end
        )
        steady_state = ok and measurement_window

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
                "measurement_window": measurement_window,
                "tls_success": tls_success,
                "tls_resumed": event.get("tls_resumed", "") if event.get("tls_resume_known") else "",
                "status": status,
                "steady_state": steady_state,
                "in_violin": False,
                "_start_ts": start_ts if start_ts is not None else 0,
            }
        )

    records.sort(key=lambda row: (row["_start_ts"], row["request_id"]))
    first_request_by_client: set[str] = set()
    for row in records:
        client_id = logical_client_id(str(row["request_id"]))
        row["_initial_client_request"] = client_id not in first_request_by_client
        first_request_by_client.add(client_id)
    mark_violin_samples(records, mode)

    steady_handshakes = sum(
        1
        for row in records
        if row["measurement_window"]
        and row["tls_success"]
        and (
            mode != "resumption"
            or (not row["_initial_client_request"] and row["tls_resumed"] is True)
        )
    )
    resumption_fallbacks = sum(
        1
        for row in records
        if mode == "resumption"
        and row["measurement_window"]
        and row["tls_success"]
        and not row["_initial_client_request"]
        and row["tls_resumed"] is False
    )

    counters = {
        "ok": sum(1 for row in records if row["status"] == "ok"),
        "failed": sum(1 for row in records if row["status"] != "ok"),
        "steady_ok": sum(1 for row in records if row["steady_state"]),
        "steady_handshakes": steady_handshakes,
        "resumption_fallbacks": resumption_fallbacks,
        "violin": sum(1 for row in records if row["in_violin"]),
        "window_start_ns": steady_start or 0,
        "window_end_ns": window_end or 0,
        "steady_duration_s": ((window_end - steady_start) / 1_000_000_000) if steady_start and window_end else 0.0,
    }

    for row in records:
        row["steady_state"] = "true" if row["steady_state"] else "false"
        row["measurement_window"] = "true" if row["measurement_window"] else "false"
        row["tls_success"] = "true" if row["tls_success"] else "false"
        if isinstance(row["tls_resumed"], bool):
            row["tls_resumed"] = "true" if row["tls_resumed"] else "false"
        row["in_violin"] = "true" if row["in_violin"] else "false"
        row.pop("_start_ts", None)
        row.pop("_initial_client_request", None)

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


def load_campaign_status(path: Path) -> dict[str, dict[str, str]]:
    if not path.exists():
        return {}
    with path.open(newline="", encoding="utf-8") as handle:
        return {
            str(row.get("run_name", "")): dict(row)
            for row in csv.DictReader(handle)
            if row.get("run_name")
        }


def load_client_report(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}

    header: list[str] | None = None
    report: dict[str, str] = {}
    with path.open(encoding="utf-8", errors="replace") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if line.startswith("THROUGHPUT_CSV,"):
                header = next(csv.reader([line]))[1:]
            elif line.startswith("THROUGHPUT_DATA,") and header is not None:
                values = next(csv.reader([line]))[1:]
                report = dict(zip(header, values))
    return report


def count_trace_event(path: Path, event_name: str) -> int:
    if not path.exists():
        return 0
    with path.open(newline="", encoding="utf-8") as handle:
        return sum(1 for row in csv.DictReader(handle) if row.get("event_name") == event_name)


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
        achieved_handshakes_rps = (
            int(summary.get("steady_handshake_count") or 0) / steady_duration_s
            if steady_duration_s > 0
            else 0.0
        )
        latencies = [float(sample["latency_ms"]) for sample in steady_ok]
        filtered_latencies: list[float] = []
        if latencies:
            cutoff = percentile(latencies, 99)
            filtered_latencies = [value for value in latencies if value <= cutoff]
        cpu_points = cpu_points_for_run(summary)
        cpu_window_start_ns = int(summary.get("window_start_ns") or 0)
        if cpu_points:
            cpu_window_start_ns = max(cpu_window_start_ns, cpu_points[0][0] + 2_000_000_000)
        cpu_values = [
            value
            for ts_ns, value in cpu_points
            if in_window(ts_ns, cpu_window_start_ns, int(summary.get("window_end_ns") or 0))
        ]
        memory_points = memory_points_for_run(summary)
        memory_values = [
            value / (1024 * 1024)
            for ts_ns, value in memory_points
            if in_window(ts_ns, cpu_window_start_ns, int(summary.get("window_end_ns") or 0))
        ]
        startup = startup_metrics_for_run(summary)
        client_report = summary.get("client_report", {})

        rows.append(
            {
                "campaign": summary["campaign"],
                "run_name": summary["run_name"],
                "deployment": summary["deployment"],
                "mode": summary["mode"],
                "clients": summary["clients"],
                "offered_rps": f"{float(summary['rate']):.6f}" if parse_float(summary["rate"]) is not None else summary["rate"],
                "achieved_rps": f"{achieved_rps:.6f}",
                "achieved_handshakes_rps": f"{achieved_handshakes_rps:.6f}",
                "success": len(steady_ok),
                "handshake_success": int(summary.get("steady_handshake_count") or 0),
                "resumption_fallbacks": int(summary.get("resumption_fallbacks") or 0),
                "failed": summary["failed_count"],
                "non2xx": parse_int(client_report.get("non2xx")) or 0,
                "errors": parse_int(client_report.get("errors")) or 0,
                "timeouts": parse_int(client_report.get("timeouts")) or 0,
                "late_slots": parse_int(client_report.get("late")) or 0,
                "scheduled_rps": client_report.get("scheduled_rps", ""),
                "scheduled_ratio": client_report.get("scheduled_ratio", ""),
                "quality_flags": client_report.get("quality_flags", ""),
                "configured_clients": client_report.get("configured_clients", ""),
                "participating_clients": client_report.get("participating_clients", ""),
                "required_clients_p99": client_report.get("required_clients_p99", ""),
                "peak_in_flight": client_report.get("peak_in_flight", ""),
                "inflight_limit": client_report.get("inflight_limit", ""),
                "unfinished_at_schedule_end": client_report.get("unfinished_at_schedule_end", ""),
                "gateway_drops": int(summary.get("gateway_drops") or 0),
                "run_status": summary.get("run_status", "unknown"),
                "client_returncode": "" if summary.get("client_returncode") is None else summary["client_returncode"],
                "failure_reason": summary.get("failure_reason", ""),
                "p99_latency_ms": f"{percentile(latencies, 99):.6f}" if latencies else "",
                "mean_latency_ms_filtered": f"{mean(filtered_latencies):.6f}" if filtered_latencies else "",
                "mean_cpu_percent": f"{mean(cpu_values):.6f}" if cpu_values else "",
                "peak_cpu_percent": f"{max(cpu_values):.6f}" if cpu_values else "",
                "median_memory_mib": f"{percentile(memory_values, 50):.6f}" if memory_values else "",
                "peak_memory_mib": f"{max(memory_values):.6f}" if memory_values else "",
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
            user = parse_float(row.get("user_time_tree_s"))
            system = parse_float(row.get("system_time_tree_s"))
            if user is None or system is None:
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


def timeseries_group(deployment: str) -> tuple[str, str, str, str]:
    if deployment == "direct":
        return "direct", "Direct", "full", "direct"
    if deployment.startswith("docker_sgxgo_"):
        strategy = ("docker_sgx", "Docker + SGX")
    elif deployment.startswith("docker_"):
        strategy = ("docker", "Docker")
    elif deployment.startswith("sgxgo_"):
        strategy = ("sgxgo", "SGX-Go")
    elif deployment.startswith("baremetal_"):
        strategy = ("baremetal", "Baremetal")
    elif deployment.startswith("sgx_"):
        strategy = ("sgx", "SGX")
    else:
        strategy = (deployment, deployment)

    handler = "empty" if "_empty_" in deployment else "full"
    reuse = "default"
    if "_noreuse" in deployment:
        reuse = "no reuse"
    elif "_reuse" in deployment:
        reuse = "reuse"
    return strategy[0], strategy[1], handler, reuse


def rate_sort_key(value: Any) -> tuple[int, float, str]:
    rate = parse_float(value)
    if rate is None:
        return (1, 0.0, str(value))
    return (0, rate, "")


def rate_label(value: Any) -> str:
    rate = parse_float(value)
    if rate is not None and rate <= 0:
        return "closed"
    if rate is not None:
        return f"{rate:g}"
    return str(value)


def downsample_rows(rows: list[Any], max_points: int) -> list[Any]:
    if max_points <= 0 or len(rows) <= max_points:
        return rows
    if max_points == 1:
        return [rows[0]]
    last = len(rows) - 1
    indexes = [round(index * last / (max_points - 1)) for index in range(max_points)]
    return [rows[index] for index in indexes]


def plot_timeseries(
    samples: list[dict[str, Any]],
    run_summaries: list[dict[str, Any]],
    output_path: Path,
    max_points_per_subplot: int = 2000,
) -> list[Path]:
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

    runs = [
        summary
        for summary in run_summaries
        if summary["ok_count"] > 0 or summary["failed_count"] > 0 or summary.get("run_failed")
    ]
    if not runs:
        print("[PLOT] no runs with client samples for time-series plot")
        return []

    samples_by_run = group_by(samples, "run_name")
    grouped_runs: dict[tuple[str, str], dict[str, Any]] = {}
    for summary in runs:
        strategy_key, strategy_label, handler, reuse = timeseries_group(str(summary["deployment"]))
        key = (strategy_key, handler)
        group = grouped_runs.setdefault(
            key,
            {"strategy_label": strategy_label, "handler": handler, "runs": []},
        )
        group["runs"].append(summary)

    written_paths: list[Path] = []
    output_path.parent.mkdir(parents=True, exist_ok=True)
    mode_order = [mode for mode in ("fresh", "persistent", "resumption") if any(run["mode"] == mode for run in runs)]
    reuse_order = {"no reuse": 0, "reuse": 1, "default": 2, "direct": 3}

    for (strategy_key, handler), group in grouped_runs.items():
        group_runs = group["runs"]
        cell_runs: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
        row_keys: set[tuple[str, str]] = set()
        for summary in group_runs:
            _, _, _, reuse = timeseries_group(str(summary["deployment"]))
            rate = str(summary["rate"])
            mode = str(summary["mode"])
            row_keys.add((rate, reuse))
            cell_runs.setdefault((rate, reuse, mode), []).append(summary)

        ordered_rows = sorted(
            row_keys,
            key=lambda item: (rate_sort_key(item[0]), reuse_order.get(item[1], 99), item[1]),
        )
        nrows = max(1, len(ordered_rows))
        ncols = len(mode_order)
        fig_width = figure_size_per_subplot[0] * ncols
        fig_height = figure_size_per_subplot[1] * nrows
        fig, axes = plt.subplots(nrows, ncols, figsize=(fig_width, fig_height), squeeze=False)

        for row_index, (rate, reuse) in enumerate(ordered_rows):
            for col_index, mode in enumerate(mode_order):
                ax = axes[row_index][col_index]
                summaries = cell_runs.get((rate, reuse, mode), [])
                if not summaries:
                    ax.axis("off")
                    ax.text(
                        0.5,
                        0.5,
                        f"{mode}\nrate={rate_label(rate)}, {reuse}\nnot available",
                        transform=ax.transAxes,
                        ha="center",
                        va="center",
                        color="#666666",
                        fontsize=9,
                    )
                    continue

                run_samples: list[dict[str, Any]] = []
                failed_samples: list[dict[str, Any]] = []
                failed_count = 0
                late_slots = 0
                non2xx = 0
                errors = 0
                gateway_drops = 0
                resumption_fallbacks = 0
                failed_runs = 0
                for summary in summaries:
                    summary_samples = samples_by_run.get(summary["run_name"], [])
                    run_samples.extend(
                        sample
                        for sample in summary_samples
                        if sample["status"] == "ok" and sample["latency_ms"] != ""
                    )
                    failed_samples.extend(
                        sample
                        for sample in summary_samples
                        if sample["status"] != "ok" and sample["elapsed_s"] != ""
                    )
                    failed_count += int(summary["failed_count"])
                    client_report = summary.get("client_report", {})
                    late_slots += parse_int(client_report.get("late")) or 0
                    non2xx += parse_int(client_report.get("non2xx")) or 0
                    errors += parse_int(client_report.get("errors")) or 0
                    gateway_drops += int(summary.get("gateway_drops") or 0)
                    resumption_fallbacks += int(summary.get("resumption_fallbacks") or 0)
                    failed_runs += int(bool(summary.get("run_failed")))

                xs = [float(sample["elapsed_s"]) for sample in run_samples]
                ys = [float(sample["latency_ms"]) for sample in run_samples]
                avg_values = [
                    float(sample["latency_ms"])
                    for sample in run_samples
                    if sample.get("steady_state") == "true"
                ]
                visible_samples = downsample_rows(run_samples, max_points_per_subplot)
                visible_xs = [float(sample["elapsed_s"]) for sample in visible_samples]
                visible_ys = [float(sample["latency_ms"]) for sample in visible_samples]

                ax.scatter(
                    visible_xs,
                    visible_ys,
                    s=dot_size,
                    alpha=dot_alpha,
                    color=dot_color,
                    edgecolors="none",
                )
                ma_xs, ma_ys = moving_average_by_time(xs, ys, moving_average_window_s)
                if ma_xs:
                    ax.plot(ma_xs, ma_ys, color=moving_average_color, linewidth=moving_average_width)

                if avg_values:
                    mean_latency = sum(avg_values) / len(avg_values)
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

                ax.set_title(f"{mode}\nrate={rate_label(rate)}, {reuse}", fontsize=10)
                ax.set_xlabel(x_label)
                ax.set_ylabel(y_label)
                ax.grid(True, alpha=0.25)
                ax.set_ylim(bottom=0)

                if show_failure_annotations and any(
                    (failed_count, late_slots, non2xx, errors, gateway_drops, resumption_fallbacks, failed_runs)
                ):
                    y_top = ax.get_ylim()[1] if ax.get_ylim()[1] > 0 else 1.0
                    visible_failed = downsample_rows(failed_samples, max_points_per_subplot)
                    failed_xs = [float(sample["elapsed_s"]) for sample in visible_failed]
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
                    diagnostics = []
                    if failed_count:
                        diagnostics.append(f"failed={failed_count}")
                    if non2xx:
                        diagnostics.append(f"non2xx={non2xx}")
                    if errors:
                        diagnostics.append(f"errors={errors}")
                    if late_slots:
                        diagnostics.append(f"late={late_slots}")
                    if gateway_drops:
                        diagnostics.append(f"drops={gateway_drops}")
                    if resumption_fallbacks:
                        diagnostics.append(f"full-handshake fallbacks={resumption_fallbacks}")
                    if failed_runs:
                        diagnostics.append(f"failed runs={failed_runs}")
                    ax.text(
                        0.98,
                        0.92,
                        "\n".join(diagnostics),
                        transform=ax.transAxes,
                        ha="right",
                        va="top",
                        color="red",
                        fontsize=9,
                    )

        handler_label = "full handler" if handler == "full" else "empty handler"
        fig.suptitle(f"{group['strategy_label']} - {handler_label}", fontsize=14)
        fig.tight_layout(rect=[0, 0, 1, 0.98])
        group_output_path = output_path.with_name(f"{output_path.stem}_{strategy_key}_{handler}{output_path.suffix}")
        fig.savefig(group_output_path, bbox_inches="tight")
        plt.close(fig)
        written_paths.append(group_output_path)

    return written_paths


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
    group_quartiles: list[tuple[float, float, float, float]] = []
    for group in groups_with_data:
        values = values_by_group[group]
        group_quartiles.append(
            (
                percentile(values, 25),
                percentile(values, 50),
                percentile(values, 75),
                percentile(values, 99),
            )
        )
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

    for index, (mean_latency, quartiles) in enumerate(zip(group_means, group_quartiles), start=1):
        q25, median, q75, p99 = quartiles
        ax.vlines(index, q25, q75, color=mean_color, linewidth=4.0, zorder=4)
        ax.scatter([index], [median], color="white", edgecolor=mean_color, linewidth=0.8, s=18, zorder=5)
        ax.scatter([index], [p99], color=mean_color, marker="_", s=75, linewidth=1.2, zorder=5)
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


STRATEGY_ORDER = ["direct", "baremetal", "sgx", "sgxgo", "docker", "docker_sgx"]
STRATEGY_COLORS = {
    "direct": "#457b9d",
    "baremetal": "#2a9d8f",
    "sgx": "#e76f51",
    "sgxgo": "#bc6c25",
    "docker": "#f4a261",
    "docker_sgx": "#6a4c93",
}

# Paper-quality filtering knobs. Keep warnings visible by default; switch either
# exclusion to True locally when preparing a figure that must omit weak load runs.
SHOW_RUN_QUALITY_WARNINGS = True
EXCLUDE_PARTIAL_CLIENT_RUNS = False
EXCLUDE_UNREACHED_OFFERED_LOAD = False


def row_quality_flags(row: dict[str, Any]) -> set[str]:
    return {
        flag.strip()
        for flag in str(row.get("quality_flags", "")).split(";")
        if flag.strip()
    }


def include_throughput_row(
    row: dict[str, Any],
    include_direct: bool,
    modes: tuple[str, ...],
    clients: int | None = 1,
) -> bool:
    strategy, _, handler, reuse = timeseries_group(str(row["deployment"]))
    mode = str(row["mode"])
    row_clients = parse_int(row.get("clients"))
    if clients is not None and row_clients != clients:
        return False
    if mode not in modes:
        return False
    if strategy == "direct":
        return include_direct
    if handler != "full":
        return False
    if strategy in {"baremetal", "sgx", "sgxgo"} and reuse == "no reuse":
        return False
    if mode == "resumption" and strategy not in {"docker", "docker_sgx"}:
        return False
    quality_flags = row_quality_flags(row)
    if EXCLUDE_PARTIAL_CLIENT_RUNS and "partial_client_participation" in quality_flags:
        return False
    if EXCLUDE_UNREACHED_OFFERED_LOAD and "offered_load_not_reached" in quality_flags:
        return False
    return True


def row_failed(row: dict[str, Any]) -> bool:
    return str(row.get("run_status", "unknown")).lower() == "failed" or (
        parse_int(row.get("client_returncode")) not in {None, 0}
    )


def row_diagnostics(row: dict[str, Any]) -> str:
    parts: list[str] = []
    if row_failed(row):
        reason = str(row.get("failure_reason", "")).strip()
        parts.append(f"failed: {reason}" if reason else "failed run")
    for key, label in (
        ("non2xx", "non2xx"),
        ("errors", "errors"),
        ("timeouts", "timeouts"),
        ("late_slots", "late"),
        ("gateway_drops", "drops"),
        ("resumption_fallbacks", "full-handshake fallbacks"),
    ):
        value = parse_int(row.get(key)) or 0
        if value:
            parts.append(f"{label}={value}")
    quality_flags = sorted(row_quality_flags(row))
    if quality_flags:
        parts.append("quality=" + "+".join(quality_flags))
    participating = parse_int(row.get("participating_clients"))
    configured = parse_int(row.get("configured_clients"))
    if participating is not None and configured is not None and participating < configured:
        parts.append(f"clients={participating}/{configured}")
    return ", ".join(parts)


def pretty_strategy_label(strategy: str) -> str:
    labels = {
        "direct": "Direct",
        "baremetal": "Baremetal",
        "sgx": "SGX",
        "sgxgo": "SGX-Go",
        "docker": "Docker",
        "docker_sgx": "Docker + SGX",
    }
    return labels.get(strategy, strategy)


def aggregate_throughput_metric_by_mode(
    summary_rows: list[dict[str, Any]],
    metric: str,
    include_direct: bool,
    modes: tuple[str, ...] = ("fresh", "persistent", "resumption"),
    clients: int | None = 1,
) -> dict[str, dict[str, list[tuple[float, float, float]]]]:
    grouped_values: dict[str, dict[str, dict[float, list[float]]]] = {mode: {} for mode in modes}
    for row in summary_rows:
        mode = str(row["mode"])
        if not include_throughput_row(row, include_direct, modes, clients) or row_failed(row):
            continue
        offered = parse_float(row.get("offered_rps"))
        value = parse_float(row.get(metric))
        if offered is None or offered <= 0 or value is None:
            continue
        strategy, _, _, _ = timeseries_group(str(row["deployment"]))
        grouped_values[mode].setdefault(strategy, {}).setdefault(offered, []).append(value)

    result: dict[str, dict[str, list[tuple[float, float, float]]]] = {}
    for mode, values_by_strategy in grouped_values.items():
        mode_result: dict[str, list[tuple[float, float, float]]] = {}
        for strategy, values_by_rate in values_by_strategy.items():
            points = [
                (rate, mean(values), confidence_interval_95(values))
                for rate, values in sorted(values_by_rate.items())
                if values
            ]
            if points:
                mode_result[strategy] = points
        result[mode] = mode_result
    return result


def plot_metric_vs_offered(
    summary_rows: list[dict[str, Any]],
    output_path: Path,
    metric: str,
    ylabel: str,
    title: str,
    include_direct: bool,
    modes: tuple[str, ...] = ("fresh", "persistent", "resumption"),
) -> None:
    grouped = aggregate_throughput_metric_by_mode(summary_rows, metric, include_direct, modes=modes)
    active_modes = [mode for mode in modes if grouped.get(mode)]
    if not active_modes:
        print(f"[PLOT] no data for {output_path.name}")
        return

    show_failure_annotations = SHOW_RUN_QUALITY_WARNINGS
    fig, axes = plt.subplots(1, len(active_modes), figsize=(5.5 * len(active_modes), 4.5), sharex=True, squeeze=False)
    axes_flat = list(axes.flatten())
    handles_by_label: dict[str, Any] = {}

    for ax, mode in zip(axes_flat, active_modes):
        mode_data = grouped.get(mode, {})
        for strategy in STRATEGY_ORDER:
            points = mode_data.get(strategy)
            if not points:
                continue
            xs = [point[0] for point in points]
            ys = [point[1] for point in points]
            label = pretty_strategy_label(strategy)
            errorbar = ax.errorbar(
                xs,
                ys,
                yerr=[point[2] for point in points],
                marker="o",
                linewidth=1.4,
                linestyle="--" if mode == "resumption" else "-",
                label=label,
                color=STRATEGY_COLORS.get(strategy),
                capsize=3,
            )
            handles_by_label.setdefault(label, errorbar.lines[0])

        ax.set_xlabel("Offered throughput (requests/s)")
        ax.set_ylabel(ylabel)
        ax.set_title(mode.capitalize())
        ax.grid(True, alpha=0.25)
        ax.set_ylim(bottom=0)

        if show_failure_annotations:
            for row in summary_rows:
                if not include_throughput_row(row, include_direct, (mode,), clients=1):
                    continue
                diagnostic = row_diagnostics(row)
                if not diagnostic:
                    continue
                x_value = parse_float(row.get("offered_rps"))
                y_value = parse_float(row.get(metric))
                if x_value is None or y_value is None:
                    continue
                ax.scatter([x_value], [y_value], marker="x", color="red", s=30, zorder=5)
                ax.annotate(
                    diagnostic,
                    (x_value, y_value),
                    xytext=(3, 4),
                    textcoords="offset points",
                    color="red",
                    fontsize=6,
                )

    fig.suptitle(title, fontsize=13)
    if handles_by_label:
        fig.legend(
            list(handles_by_label.values()),
            list(handles_by_label.keys()),
            loc="lower center",
            ncol=min(len(handles_by_label), 5),
            fontsize=8,
        )
    fig.tight_layout(rect=[0, 0.12, 1, 0.94])
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, bbox_inches="tight")
    plt.close(fig)


def plot_throughput_family(
    summary_rows: list[dict[str, Any]],
    output_path: Path,
    modes: tuple[str, ...],
    metric: str,
    xlabel: str,
    ylabel: str,
    title: str,
) -> None:
    grouped = aggregate_throughput_metric_by_mode(summary_rows, metric, True, modes=modes)
    if not any(grouped.get(mode) for mode in modes):
        print(f"[PLOT] no data for {output_path.name}")
        return

    show_failure_annotations = SHOW_RUN_QUALITY_WARNINGS
    fig, ax = plt.subplots(figsize=(8.0, 5.0))
    max_offered = 0.0

    for mode in modes:
        for strategy in STRATEGY_ORDER:
            points = grouped.get(mode, {}).get(strategy)
            if not points:
                continue
            xs = [point[0] for point in points]
            ys = [point[1] for point in points]
            max_offered = max(max_offered, max(xs))
            ax.errorbar(
                xs,
                ys,
                yerr=[point[2] for point in points],
                marker="o",
                linewidth=1.4,
                linestyle="--" if mode == "resumption" else "-",
                label=f"{pretty_strategy_label(strategy)} ({mode})",
                color=STRATEGY_COLORS.get(strategy),
                capsize=3,
            )

    if max_offered > 0:
        ax.plot([0, max_offered], [0, max_offered], color="#666666", linestyle=":", linewidth=1.0, label="ideal")

    if show_failure_annotations:
        for row in summary_rows:
            if not include_throughput_row(row, True, modes, clients=1):
                continue
            diagnostic = row_diagnostics(row)
            if not diagnostic:
                continue
            x_value = parse_float(row.get("offered_rps"))
            y_value = parse_float(row.get(metric))
            if x_value is None or y_value is None:
                continue
            ax.scatter([x_value], [y_value], marker="x", color="red", s=32, zorder=5)
            ax.annotate(
                diagnostic,
                (x_value, y_value),
                xytext=(3, 4),
                textcoords="offset points",
                color="red",
                fontsize=6,
            )

    ax.set_xlabel(xlabel)
    ax.set_ylabel(ylabel)
    ax.set_title(title)
    ax.grid(True, alpha=0.25)
    ax.set_xlim(left=0)
    ax.set_ylim(bottom=0)
    ax.legend(fontsize=8)
    fig.tight_layout()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, bbox_inches="tight")
    plt.close(fig)


def plot_request_throughput(summary_rows: list[dict[str, Any]], output_path: Path) -> None:
    plot_throughput_family(
        summary_rows,
        output_path,
        modes=("persistent", "resumption"),
        metric="achieved_rps",
        xlabel="Offered throughput (requests/s)",
        ylabel="Achieved throughput (requests/s)",
        title="Persistent and Resumed Request Throughput",
    )


def plot_handshake_throughput(summary_rows: list[dict[str, Any]], output_path: Path) -> None:
    plot_throughput_family(
        summary_rows,
        output_path,
        modes=("fresh", "resumption"),
        metric="achieved_handshakes_rps",
        xlabel="Offered throughput (handshakes/s)",
        ylabel="Achieved throughput (handshakes/s)",
        title="Full and Resumed Handshake Throughput",
    )


def plot_latency_vs_offered(summary_rows: list[dict[str, Any]], output_path: Path) -> None:
    plot_metric_vs_offered(
        summary_rows,
        output_path,
        metric="p99_latency_ms",
        ylabel="p99 end-to-end latency (ms)",
        title="Offered Throughput vs p99 End-to-End Latency",
        include_direct=True,
    )


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
    plot_metric_vs_offered(
        summary_rows,
        output_path,
        metric="mean_cpu_percent",
        ylabel="Total CPU usage (%)",
        title="Offered Throughput vs Total CPU Usage",
        include_direct=False,
    )


def aggregate_scalability_metric(
    summary_rows: list[dict[str, Any]],
    strategy: str,
    mode: str,
    metric: str,
) -> dict[int, list[tuple[float, float, float]]]:
    grouped: dict[int, dict[float, list[float]]] = {}
    for row in summary_rows:
        row_strategy, _, _, _ = timeseries_group(str(row["deployment"]))
        if row_strategy != strategy:
            continue
        if not include_throughput_row(row, include_direct=True, modes=(mode,), clients=None) or row_failed(row):
            continue
        clients = parse_int(row.get("clients"))
        offered = parse_float(row.get("offered_rps"))
        value = parse_float(row.get(metric))
        if clients is None or offered is None or offered <= 0 or value is None:
            continue
        grouped.setdefault(clients, {}).setdefault(offered, []).append(value)

    return {
        clients: [
            (rate, mean(values), confidence_interval_95(values))
            for rate, values in sorted(by_rate.items())
            if values
        ]
        for clients, by_rate in grouped.items()
    }


def plot_scalability(summary_rows: list[dict[str, Any]], output_dir: Path) -> list[Path]:
    show_failure_annotations = SHOW_RUN_QUALITY_WARNINGS
    written: list[Path] = []

    for strategy in STRATEGY_ORDER:
        for mode in ("persistent", "resumption"):
            achieved = aggregate_scalability_metric(summary_rows, strategy, mode, "achieved_rps")
            client_counts = sorted(achieved)
            if len(client_counts) < 2:
                continue

            metrics = [
                ("achieved_rps", "Achieved throughput (requests/s)"),
                ("p99_latency_ms", "p99 end-to-end latency (ms)"),
            ]
            if strategy != "direct":
                metrics.append(("mean_cpu_percent", "Total CPU usage (%)"))

            fig, axes = plt.subplots(1, len(metrics), figsize=(5.2 * len(metrics), 4.5), squeeze=False)
            for ax, (metric, ylabel) in zip(axes.flatten(), metrics):
                values_by_clients = aggregate_scalability_metric(summary_rows, strategy, mode, metric)
                for clients in client_counts:
                    points = values_by_clients.get(clients, [])
                    if not points:
                        continue
                    ax.errorbar(
                        [point[0] for point in points],
                        [point[1] for point in points],
                        yerr=[point[2] for point in points],
                        marker="o",
                        linewidth=1.3,
                        label=f"{clients} clients",
                        capsize=3,
                    )

                if show_failure_annotations:
                    for row in summary_rows:
                        row_strategy, _, _, _ = timeseries_group(str(row["deployment"]))
                        if row_strategy != strategy or not include_throughput_row(
                            row, include_direct=True, modes=(mode,), clients=None
                        ):
                            continue
                        diagnostic = row_diagnostics(row)
                        if not diagnostic:
                            continue
                        x_value = parse_float(row.get("offered_rps"))
                        y_value = parse_float(row.get(metric))
                        if x_value is None or y_value is None:
                            continue
                        ax.scatter([x_value], [y_value], marker="x", color="red", s=28, zorder=5)

                ax.set_xlabel("Offered throughput (requests/s)")
                ax.set_ylabel(ylabel)
                ax.grid(True, alpha=0.25)
                ax.set_ylim(bottom=0)
                ax.legend(fontsize=8)

            fig.suptitle(f"{pretty_strategy_label(strategy)} {mode.capitalize()} Scalability", fontsize=13)
            fig.tight_layout(rect=[0, 0, 1, 0.95])
            output_path = output_dir / f"scalability_{strategy}_{mode}.pdf"
            output_path.parent.mkdir(parents=True, exist_ok=True)
            fig.savefig(output_path, bbox_inches="tight")
            plt.close(fig)
            written.append(output_path)

    return written


def plot_closed_loop_scalability(summary_rows: list[dict[str, Any]], output_path: Path) -> None:
    grouped: dict[tuple[str, str], dict[int, list[float]]] = {}
    for row in summary_rows:
        mode = str(row["mode"])
        offered = parse_float(row.get("offered_rps"))
        clients = parse_int(row.get("clients"))
        achieved = parse_float(row.get("achieved_rps"))
        if mode not in {"persistent", "resumption"} or offered != 0 or clients is None or achieved is None:
            continue
        if not include_throughput_row(row, include_direct=True, modes=(mode,), clients=None) or row_failed(row):
            continue
        strategy, _, _, _ = timeseries_group(str(row["deployment"]))
        grouped.setdefault((strategy, mode), {}).setdefault(clients, []).append(achieved)

    if not grouped:
        print(f"[PLOT] no closed-loop data for {output_path.name}")
        return

    fig, ax = plt.subplots(figsize=(8.0, 5.0))
    for strategy in STRATEGY_ORDER:
        for mode in ("persistent", "resumption"):
            by_clients = grouped.get((strategy, mode))
            if not by_clients:
                continue
            points = [
                (clients, mean(values), confidence_interval_95(values))
                for clients, values in sorted(by_clients.items())
            ]
            ax.errorbar(
                [point[0] for point in points],
                [point[1] for point in points],
                yerr=[point[2] for point in points],
                marker="o",
                linewidth=1.4,
                linestyle="--" if mode == "resumption" else "-",
                color=STRATEGY_COLORS.get(strategy),
                capsize=3,
                label=f"{pretty_strategy_label(strategy)} ({mode})",
            )

    ax.set_xlabel("Persistent clients")
    ax.set_ylabel("Achieved throughput (requests/s)")
    ax.set_title("Closed-loop Client Scalability")
    ax.set_xscale("log", base=2)
    ax.set_ylim(bottom=0)
    ax.grid(True, alpha=0.25)
    ax.legend(fontsize=8)
    fig.tight_layout()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, bbox_inches="tight")
    plt.close(fig)


def latex_escape(value: Any) -> str:
    text = str(value)
    for old, new in (("\\", r"\textbackslash{}"), ("_", r"\_"), ("%", r"\%"), ("&", r"\&")):
        text = text.replace(old, new)
    return text


def trace_paths(run_dir: Path, role: str) -> list[Path]:
    csv_dir = run_dir / "csv"
    if role == "middlebox":
        paths = list(csv_dir.glob("dcmb-worker*.csv"))
        middlebox = csv_dir / "middlebox.csv"
        if middlebox.exists():
            paths.append(middlebox)
        return sorted(paths)
    path = csv_dir / f"{role}.csv"
    return [path] if path.exists() else []


def load_trace_index(paths: list[Path]) -> dict[str, dict[str, list[tuple[int, int]]]]:
    index: dict[str, dict[str, list[tuple[int, int]]]] = {}
    for path in paths:
        with path.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                event_name = str(row.get("event_name", ""))
                event_id = str(row.get("id", ""))
                timestamp = parse_int(row.get("timestamp_ns"))
                arg = parse_int(row.get("arg"))
                if not event_name or not event_id or timestamp is None:
                    continue
                index.setdefault(event_name, {}).setdefault(event_id, []).append((timestamp, arg or 0))
    for events_by_id in index.values():
        for events in events_by_id.values():
            events.sort()
    return index


def first_event_duration_ms(
    index: dict[str, dict[str, list[tuple[int, int]]]],
    start_event: str,
    done_event: str,
    event_id: str,
) -> float:
    starts = index.get(start_event, {}).get(event_id, [])
    dones = index.get(done_event, {}).get(event_id, [])
    if not starts or not dones:
        return 0.0
    start = starts[0][0]
    done = next((timestamp for timestamp, _ in dones if timestamp >= start), None)
    return max(0.0, (done - start) / 1_000_000) if done is not None else 0.0


def all_event_durations_ms(
    paths: list[Path],
    start_event: str,
    done_event: str,
    done_must_succeed: bool = True,
) -> list[float]:
    return event_durations_from_index(
        load_trace_index(paths), start_event, done_event, done_must_succeed
    )


def event_durations_from_index(
    index: dict[str, dict[str, list[tuple[int, int]]]],
    start_event: str,
    done_event: str,
    done_must_succeed: bool = True,
) -> list[float]:
    values: list[float] = []
    for event_id, starts in index.get(start_event, {}).items():
        dones = list(index.get(done_event, {}).get(event_id, []))
        done_index = 0
        for start, _ in starts:
            while done_index < len(dones) and dones[done_index][0] < start:
                done_index += 1
            if done_index >= len(dones):
                break
            done, arg = dones[done_index]
            done_index += 1
            if done_must_succeed and arg != 0:
                continue
            values.append((done - start) / 1_000_000)
    return values


def write_component_costs_latex(run_summaries: list[dict[str, Any]], output_path: Path) -> bool:
    specs = [
        ("gateway", "gateway_worker_select_start", "gateway_worker_select_done", "Worker selection", True),
        ("gateway", "gateway_backend_dial_start", "gateway_backend_dial_done", "Backend TCP dial", True),
        ("gateway", "gateway_container_create", "gateway_container_ready", "Container create to ready", True),
        ("middlebox", "middlebox_delegation_fetch_by_id", "middlebox_delegation_fetched_by_id", "Delegation retrieval", True),
        ("middlebox", "middlebox_attestation_start_by_id", "middlebox_attestation_done_by_id", "Quote generation", False),
        ("certserver", "certserver_quote_verify_by_id", "certserver_quote_done_by_id", "Quote verification", True),
        ("certserver", "certserver_generate_by_id", "certserver_generate_done_by_id", "Delegated credential generation", True),
        ("middlebox", "middlebox_validation_start", "middlebox_validation_done", "Request validation", True),
        ("middlebox", "middlebox_upstream_dial_start", "middlebox_upstream_dial_done", "Upstream TCP dial", True),
        ("middlebox", "middlebox_upstream_tls_start", "middlebox_upstream_tls_done", "Upstream TLS", True),
        ("middlebox", "middlebox_response_validation_start", "middlebox_response_validation_done", "Response validation", True),
    ]
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for summary in run_summaries:
        if summary.get("run_failed"):
            continue
        deployment = str(summary["deployment"])
        run_dir = Path(str(summary["run_dir"]))
        indexes = {
            role: load_trace_index(trace_paths(run_dir, role))
            for role in {spec[0] for spec in specs}
        }
        for role, start, done, label, success_zero in specs:
            values = event_durations_from_index(indexes[role], start, done, success_zero)
            if not values:
                continue
            bucket = grouped.setdefault((deployment, label), {"values": [], "runs": set()})
            bucket["values"].extend(values)
            bucket["runs"].add(str(summary["run_name"]))

    if not grouped:
        print(f"[PLOT] no component trace data for {output_path.name}")
        return False

    lines = [
        r"\begin{tabular}{llrrrr}",
        r"\toprule",
        r"Deployment & Component & Samples & Median (ms) & p95 (ms) & Runs \\",
        r"\midrule",
    ]
    for (deployment, component), data in sorted(grouped.items()):
        values = data["values"]
        lines.append(
            f"{latex_escape(deployment)} & {latex_escape(component)} & {len(values)} & "
            f"{percentile(values, 50):.3f} & {percentile(values, 95):.3f} & {len(data['runs'])} \\\\"
        )
    lines.extend((r"\bottomrule", r"\end{tabular}"))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return True


def binding_by_id(index: dict[str, dict[str, list[tuple[int, int]]]], event_name: str) -> dict[str, int]:
    return {
        event_id: events[-1][1]
        for event_id, events in index.get(event_name, {}).items()
        if events and events[-1][1] != 0
    }


def fit_latency_components(total_ms: float, values: list[float]) -> list[float]:
    remaining = max(0.0, total_ms)
    fitted: list[float] = []
    for value in values:
        component = min(max(0.0, value), remaining)
        fitted.append(component)
        remaining -= component
    fitted.append(remaining)
    return fitted


def sample_latency_components(
    sample: dict[str, Any],
    middlebox: dict[str, dict[str, list[tuple[int, int]]]],
    gateway: dict[str, dict[str, list[tuple[int, int]]]],
    certserver: dict[str, dict[str, list[tuple[int, int]]]],
    trace_connections: dict[str, int],
    gateway_by_connection: dict[int, str],
    delegation_by_connection: dict[int, str],
) -> tuple[list[float], list[float]]:
    trace_id = str(sample["request_id"])
    connection_key = trace_connections.get(trace_id)
    gateway_id = gateway_by_connection.get(connection_key or 0, "")
    delegation_id = delegation_by_connection.get(connection_key or 0, "")

    gateway_setup = first_event_duration_ms(
        gateway, "gateway_worker_select_start", "gateway_backend_dial_done", gateway_id
    )
    quote_generation = first_event_duration_ms(
        middlebox, "middlebox_attestation_start_by_id", "middlebox_attestation_done_by_id", delegation_id
    )
    quote_verification = first_event_duration_ms(
        certserver, "certserver_quote_verify_by_id", "certserver_quote_done_by_id", delegation_id
    )
    dc_generation = first_event_duration_ms(
        certserver, "certserver_generate_by_id", "certserver_generate_done_by_id", delegation_id
    )
    dc_fetch = first_event_duration_ms(
        middlebox, "middlebox_delegation_fetch_by_id", "middlebox_delegation_fetched_by_id", delegation_id
    )
    dc_remainder = max(0.0, dc_fetch - quote_generation - quote_verification - dc_generation)
    handshake = fit_latency_components(
        parse_float(sample.get("handshake_ms")) or 0.0,
        [gateway_setup, quote_generation, quote_verification, dc_generation, dc_remainder],
    )

    request_validation = first_event_duration_ms(
        middlebox, "middlebox_validation_start", "middlebox_validation_done", trace_id
    )
    upstream_tcp = first_event_duration_ms(
        middlebox, "middlebox_upstream_dial_start", "middlebox_upstream_dial_done", trace_id
    )
    upstream_tls = first_event_duration_ms(
        middlebox, "middlebox_upstream_tls_start", "middlebox_upstream_tls_done", trace_id
    )
    response_validation = first_event_duration_ms(
        middlebox, "middlebox_response_validation_start", "middlebox_response_validation_done", trace_id
    )
    request = fit_latency_components(
        parse_float(sample.get("request_ms")) or 0.0,
        [request_validation, upstream_tcp, upstream_tls, response_validation],
    )
    return handshake, request


def plot_latency_dissection(
    samples: list[dict[str, Any]],
    run_summaries: list[dict[str, Any]],
    output_dir: Path,
) -> list[Path]:
    rate_filter = 10.0  # edit locally when dissecting a different offered load
    summaries = {str(summary["run_name"]): summary for summary in run_summaries}
    by_run: dict[str, list[dict[str, Any]]] = {}
    for sample in samples:
        if sample.get("status") != "ok" or sample.get("steady_state") != "true":
            continue
        if parse_int(sample.get("clients")) != 1 or not math.isclose(parse_float(sample.get("rate")) or -1, rate_filter):
            continue
        by_run.setdefault(str(sample["run_name"]), []).append(sample)

    handshake_labels = [
        "Gateway setup", "Quote generation", "Quote verification", "DC generation", "DC transfer/parser", "TLS remainder"
    ]
    request_labels = ["Request validation", "Upstream TCP", "Upstream TLS", "Response validation", "App/network remainder"]
    colors = ["#457b9d", "#e76f51", "#bc6c25", "#f4a261", "#8ecae6", "#adb5bd"]
    written: list[Path] = []

    for mode in ("fresh", "persistent", "resumption"):
        run_vectors: dict[str, list[tuple[list[float], list[float]]]] = {}
        for run_name, run_samples in by_run.items():
            summary = summaries.get(run_name)
            if not summary or str(summary["mode"]) != mode or summary.get("run_failed"):
                continue
            strategy, _, handler, reuse = timeseries_group(str(summary["deployment"]))
            if strategy != "direct" and handler != "full":
                continue
            if strategy in {"baremetal", "sgx", "sgxgo"} and reuse == "no reuse":
                continue
            if mode == "resumption" and strategy not in {"docker", "docker_sgx"}:
                continue
            run_dir = Path(str(summary["run_dir"]))
            middlebox = load_trace_index(trace_paths(run_dir, "middlebox"))
            gateway = load_trace_index(trace_paths(run_dir, "gateway"))
            certserver = load_trace_index(trace_paths(run_dir, "certserver"))
            trace_connections = binding_by_id(middlebox, "middlebox_trace_connection_bind")
            gateway_by_connection = {
                key: event_id
                for event_id, key in binding_by_id(gateway, "gateway_backend_connection_bind").items()
            }
            delegation_by_connection = {
                key: event_id
                for event_id, key in binding_by_id(middlebox, "middlebox_delegation_connection_bind").items()
            }
            vectors = [
                sample_latency_components(
                    sample,
                    middlebox,
                    gateway,
                    certserver,
                    trace_connections,
                    gateway_by_connection,
                    delegation_by_connection,
                )
                for sample in run_samples
            ]
            if vectors:
                run_vectors.setdefault(strategy, []).append(
                    (
                        [mean([vector[0][i] for vector in vectors]) for i in range(len(handshake_labels))],
                        [mean([vector[1][i] for vector in vectors]) for i in range(len(request_labels))],
                    )
                )

        strategies = [strategy for strategy in STRATEGY_ORDER if run_vectors.get(strategy)]
        if not strategies:
            continue
        handshake_values = {
            strategy: [mean([run[0][i] for run in run_vectors[strategy]]) for i in range(len(handshake_labels))]
            for strategy in strategies
        }
        request_values = {
            strategy: [mean([run[1][i] for run in run_vectors[strategy]]) for i in range(len(request_labels))]
            for strategy in strategies
        }

        fig, axes = plt.subplots(1, 2, figsize=(12.0, max(4.0, 0.65 * len(strategies) + 2.0)), sharey=True)
        for ax, labels, values_by_strategy, title in (
            (axes[0], handshake_labels, handshake_values, "Connection and handshake"),
            (axes[1], request_labels, request_values, "Request and response"),
        ):
            left = [0.0] * len(strategies)
            for index, label in enumerate(labels):
                values = [values_by_strategy[strategy][index] for strategy in strategies]
                ax.barh(
                    range(len(strategies)), values, left=left, color=colors[index], label=label, height=0.62
                )
                left = [current + value for current, value in zip(left, values)]
            for y, total in enumerate(left):
                ax.text(total, y, f" {total:.2f} ms", va="center", fontsize=8)
            ax.set_title(title)
            ax.set_xlabel("Latency (ms)")
            ax.grid(True, axis="x", alpha=0.25)
            ax.set_xlim(left=0)
        axes[0].set_yticks(range(len(strategies)))
        axes[0].set_yticklabels([pretty_strategy_label(strategy) for strategy in strategies])
        handles = axes[0].get_legend_handles_labels()
        request_handles = axes[1].get_legend_handles_labels()
        fig.legend(
            handles[0] + request_handles[0],
            handles[1] + request_handles[1],
            loc="upper center",
            ncol=4,
            fontsize=8,
        )
        fig.suptitle(f"Latency dissection ({mode}, rate={rate_filter:g})", y=1.02)
        fig.tight_layout(rect=[0, 0, 1, 0.88])
        path = output_dir / f"latency_dissection_{mode}.pdf"
        fig.savefig(path, bbox_inches="tight")
        plt.close(fig)
        written.append(path)
    return written


def write_resource_summary_latex(summary_rows: list[dict[str, Any]], output_path: Path) -> bool:
    grouped: dict[tuple[str, str, int, float], list[dict[str, Any]]] = {}
    for row in summary_rows:
        strategy, _, _, _ = timeseries_group(str(row["deployment"]))
        if strategy == "direct" or row_failed(row):
            continue
        if not include_throughput_row(
            row,
            include_direct=False,
            modes=("fresh", "persistent", "resumption"),
            clients=None,
        ):
            continue
        clients = parse_int(row.get("clients"))
        offered = parse_float(row.get("offered_rps"))
        memory_median = parse_float(row.get("median_memory_mib"))
        memory_peak = parse_float(row.get("peak_memory_mib"))
        if clients is None or offered is None or memory_median is None or memory_peak is None:
            continue
        grouped.setdefault((strategy, str(row["mode"]), clients, offered), []).append(row)

    if not grouped:
        print(f"[PLOT] no memory data for {output_path.name}")
        return False

    strategy_rank = {strategy: index for index, strategy in enumerate(STRATEGY_ORDER)}
    lines = [
        r"\begin{tabular}{llrrrrrr}",
        r"\toprule",
        r"Deployment & Mode & Clients & Offered & CPU (\%) & Median memory & Peak memory & Runs \\",
        r" & & & (req/s) & & (MiB) & (MiB) & \\",
        r"\midrule",
    ]

    for key in sorted(grouped, key=lambda item: (strategy_rank.get(item[0], 99), item[1], item[2], item[3])):
        strategy, mode, clients, offered = key
        rows = grouped[key]
        cpu_values = [value for row in rows if (value := parse_float(row.get("mean_cpu_percent"))) is not None]
        memory_medians = [float(row["median_memory_mib"]) for row in rows]
        memory_peaks = [float(row["peak_memory_mib"]) for row in rows]
        cpu_text = f"{percentile(cpu_values, 50):.2f}" if cpu_values else "--"
        lines.append(
            f"{latex_escape(pretty_strategy_label(strategy))} & {latex_escape(mode.capitalize())} & "
            f"{clients} & {offered:g} & {cpu_text} & {percentile(memory_medians, 50):.2f} & "
            f"{percentile(memory_peaks, 50):.2f} & {len(rows)} \\\\"
        )

    lines.extend((r"\bottomrule", r"\end{tabular}"))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return True


def plot_startup_times(run_summaries: list[dict[str, Any]], output_path: Path) -> None:
    figure_size = (10.5, 4.8)
    log_scale = True  # set to False locally for a linear startup-time axis
    startup_labels = {
        "startup_native": "Native process",
        "startup_gramine_sgx": "Gramine SGX",
        "startup_sgxgo": "SGX-Go in Gramine",
        "startup_container": "Container",
        "startup_container_gramine_sgx": "Container + Gramine SGX",
        "startup_container_sgxgo": "Container + SGX-Go",
    }
    values_by_group: dict[tuple[str, str], list[float]] = {}
    for summary in run_summaries:
        deployment = str(summary["deployment"])
        if deployment == "direct":
            continue
        startup = startup_metrics_for_run(summary)
        process_ready = parse_float(startup.get("process_ready_ms"))
        first_worker = parse_float(startup.get("first_worker_ready_ms"))
        if process_ready is not None:
            strategy = timeseries_group(deployment)[0]
            milestone = "Gateway ready" if strategy in {"docker", "docker_sgx"} else "Process ready"
            values_by_group.setdefault((deployment, milestone), []).append(process_ready)
        if first_worker is not None:
            values_by_group.setdefault((deployment, "First worker ready"), []).append(first_worker)

    if not values_by_group:
        print("[PLOT] no startup data")
        return

    groups = list(values_by_group)
    strategies = [timeseries_group(deployment)[0] for deployment, _ in groups]
    strategy_counts = {strategy: strategies.count(strategy) for strategy in set(strategies)}

    labels: list[str] = []
    for deployment, milestone in groups:
        strategy, strategy_label, handler, reuse = timeseries_group(deployment)
        strategy_label = startup_labels.get(deployment, strategy_label)
        if strategy_counts[strategy] > sum(1 for group in groups if group[0] == deployment):
            qualifiers = ["empty" if handler == "empty" else "full"]
            if reuse == "reuse":
                qualifiers.append("DC reuse")
            elif reuse == "no reuse":
                qualifiers.append("no DC reuse")
            strategy_label += f" ({', '.join(qualifiers)})"
        labels.append(f"{strategy_label}\n{milestone}")

    means = [mean(values_by_group[group]) for group in groups]
    errors = [confidence_interval_95(values_by_group[group]) for group in groups]

    fig, ax = plt.subplots(figsize=figure_size)
    xs = list(range(len(labels)))
    ax.bar(xs, means, yerr=errors, color="#8ecae6", edgecolor="#023047", linewidth=0.8, capsize=4)

    ax.set_xticks(xs)
    ax.set_xticklabels(labels, rotation=15, ha="right", fontsize=8.5)
    ax.set_ylabel("Startup time (ms)")
    ax.set_title("Middlebox startup time")
    ax.grid(True, axis="y", alpha=0.25)
    if log_scale:
        ax.set_yscale("log")
    else:
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
    rate_filter: float | None = 10.0,
) -> None:
    figure_size_per_mode = (8.0, 4.0)
    modes = ordered_unique(str(summary["mode"]) for summary in run_summaries)
    mode_data: dict[str, dict[str, dict[str, list[float]]]] = {}

    for sample in samples:
        if sample["status"] != "ok":
            continue
        if rate_filter is not None:
            rate = parse_float(sample.get("rate"))
            if rate is None or not math.isclose(rate, rate_filter):
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
        title = f"Handshake and request duration ({mode})"
        if rate_filter is not None:
            title += f", rate={rate_filter:g}"
        ax.set_title(title)
        ax.grid(True, axis="y", alpha=0.25)
        ax.set_ylim(bottom=0)
        ax.legend(fontsize=8)

    fig.tight_layout()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, bbox_inches="tight")
    plt.close(fig)


def add_run_mean(
    buckets: dict[tuple[str, str], dict[str, list[float]]],
    bucket: tuple[str, str],
    run_name: str,
    value: float,
) -> None:
    buckets.setdefault(bucket, {}).setdefault(run_name, []).append(value)


def finalize_run_means(
    buckets: dict[tuple[str, str], dict[str, list[float]]],
) -> dict[tuple[str, str], list[float]]:
    result: dict[tuple[str, str], list[float]] = {}
    for bucket, runs in buckets.items():
        means = [mean(values) for values in runs.values() if values]
        if means:
            result[bucket] = means
    return result


def plot_grouped_bar_comparison(
    values_by_bucket: dict[tuple[str, str], list[float]],
    output_path: Path,
    title: str,
    ylabel: str,
    bar_order: list[tuple[str, str, str]],
) -> None:
    strategy_order = [
        ("direct", "Direct"),
        ("baremetal", "Baremetal"),
        ("sgx", "SGX"),
        ("docker", "Docker"),
        ("docker_sgx", "Docker + SGX"),
    ]
    groups = [
        (strategy, label)
        for strategy, label in strategy_order
        if any((strategy, bar_key) in values_by_bucket for bar_key, _, _ in bar_order)
    ]
    if not groups:
        print(f"[PLOT] no data for {output_path.name}")
        return

    width = 0.22
    fig, ax = plt.subplots(figsize=(8.0, 4.5))
    legend_seen: set[str] = set()

    for group_index, (strategy, _) in enumerate(groups):
        available = [
            (bar_key, bar_label, color)
            for bar_key, bar_label, color in bar_order
            if (strategy, bar_key) in values_by_bucket
        ]
        for bar_index, (bar_key, bar_label, color) in enumerate(available):
            run_means = values_by_bucket[(strategy, bar_key)]
            x = group_index + (bar_index - (len(available) - 1) / 2) * width
            label = bar_label if bar_label not in legend_seen else None
            ax.bar(
                x,
                mean(run_means),
                width=width,
                yerr=confidence_interval_95(run_means) if len(run_means) > 1 else None,
                capsize=4 if len(run_means) > 1 else 0,
                label=label,
                color=color,
            )
            legend_seen.add(bar_label)

    ax.set_xticks(range(len(groups)))
    ax.set_xticklabels([label for _, label in groups])
    ax.set_ylabel(ylabel)
    ax.set_title(title)
    ax.grid(True, axis="y", alpha=0.25)
    ax.set_ylim(bottom=0)
    if legend_seen:
        ax.legend(fontsize=8)
    fig.tight_layout()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, bbox_inches="tight")
    plt.close(fig)


def plot_dc_reuse_handshake_latency(samples: list[dict[str, Any]], output_path: Path) -> None:
    buckets: dict[tuple[str, str], dict[str, list[float]]] = {}
    for sample in samples:
        if sample["status"] != "ok" or str(sample["mode"]) != "fresh":
            continue
        rate = parse_float(sample.get("rate"))
        value = parse_float(sample.get("handshake_ms"))
        if rate is None or not math.isclose(rate, 5.0) or value is None:
            continue

        strategy, _, handler, reuse = timeseries_group(str(sample["deployment"]))
        if strategy == "direct":
            add_run_mean(buckets, (strategy, "direct"), str(sample["run_name"]), value)
            continue
        if handler != "full":
            continue
        add_run_mean(buckets, (strategy, reuse), str(sample["run_name"]), value)

    plot_grouped_bar_comparison(
        finalize_run_means(buckets),
        output_path,
        "Delegated Credential Reuse - Handshake Latency (fresh, rate=5)",
        "Average handshake time (ms)",
        [
            ("direct", "Direct", "#8ecae6"),
            ("no reuse", "No reuse", "#f4a261"),
            ("reuse", "Reuse", "#2a9d8f"),
        ],
    )


def plot_handler_request_latency(samples: list[dict[str, Any]], output_path: Path) -> None:
    buckets: dict[tuple[str, str], dict[str, list[float]]] = {}
    for sample in samples:
        if sample["status"] != "ok" or str(sample["mode"]) != "persistent":
            continue
        if sample.get("steady_state") != "true":
            continue
        rate = parse_float(sample.get("rate"))
        value = parse_float(sample.get("request_ms"))
        if rate is None or not math.isclose(rate, 5.0) or value is None:
            continue

        strategy, _, handler, reuse = timeseries_group(str(sample["deployment"]))
        if strategy == "direct":
            add_run_mean(buckets, (strategy, "direct"), str(sample["run_name"]), value)
            continue
        if reuse == "reuse":
            continue
        add_run_mean(buckets, (strategy, handler), str(sample["run_name"]), value)

    plot_grouped_bar_comparison(
        finalize_run_means(buckets),
        output_path,
        "Handler Cost - Request Latency (persistent, rate=5)",
        "Average request time excluding handshake (ms)",
        [
            ("direct", "Direct", "#8ecae6"),
            ("full", "Full", "#2a9d8f"),
            ("empty", "Empty", "#f4a261"),
        ],
    )


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
    # Two-sided Student-t critical values for 95% confidence. Runs, not packets,
    # are the independent observations.
    t_critical = (
        12.706, 4.303, 3.182, 2.776, 2.571, 2.447, 2.365, 2.306, 2.262, 2.228,
        2.201, 2.179, 2.160, 2.145, 2.131, 2.120, 2.110, 2.101, 2.093, 2.086,
        2.080, 2.074, 2.069, 2.064, 2.060, 2.056, 2.052, 2.048, 2.045, 2.042,
    )
    degrees_of_freedom = len(values) - 1
    critical = t_critical[degrees_of_freedom - 1] if degrees_of_freedom <= len(t_critical) else 1.96
    return critical * math.sqrt(variance) / math.sqrt(len(values))


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
    parser.add_argument(
        "--handshake-request-rate",
        type=float,
        default=10.0,
        help="offered rate to use for handshake_request_duration.pdf; default: 10",
    )
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
    timeseries_paths = plot_timeseries(samples, run_summaries, out_dir / timeseries_filename)
    plot_violin(samples, run_summaries, out_dir / violin_filename)
    plot_request_throughput(summary_rows, out_dir / "throughput_requests_offered_achieved.pdf")
    plot_handshake_throughput(summary_rows, out_dir / "throughput_handshakes_offered_achieved.pdf")
    plot_latency_vs_offered(summary_rows, out_dir / "latency_vs_offered.pdf")
    plot_cpu_timeseries(run_summaries, out_dir / "cpu_timeseries.pdf")
    plot_memory_timeseries(run_summaries, out_dir / "memory_timeseries.pdf")
    plot_cpu_vs_offered(summary_rows, out_dir / "cpu_vs_offered.pdf")
    plot_startup_times(run_summaries, out_dir / "startup_times.pdf")
    plot_worker_startup_distribution(run_summaries, out_dir / "worker_startup_distribution.pdf")
    plot_handshake_request_duration(
        samples,
        run_summaries,
        out_dir / "handshake_request_duration.pdf",
        rate_filter=args.handshake_request_rate,
    )
    plot_dc_reuse_handshake_latency(samples, out_dir / "dc_reuse_handshake_latency.pdf")
    plot_handler_request_latency(samples, out_dir / "handler_request_latency.pdf")
    scalability_paths = plot_scalability(summary_rows, out_dir)
    plot_closed_loop_scalability(summary_rows, out_dir / "scalability_closed_loop.pdf")
    resource_table_path = out_dir / "resource_summary.tex"
    wrote_resource_table = write_resource_summary_latex(summary_rows, resource_table_path)
    component_table_path = out_dir / "component_costs.tex"
    wrote_component_table = write_component_costs_latex(run_summaries, component_table_path)
    dissection_paths = plot_latency_dissection(samples, run_summaries, out_dir)

    print(f"[PLOT] wrote {out_dir / samples_filename}")
    print(f"[PLOT] wrote {out_dir / summary_filename}")
    for path in timeseries_paths:
        print(f"[PLOT] wrote {path}")
    print(f"[PLOT] wrote {out_dir / violin_filename}")
    for path in scalability_paths:
        print(f"[PLOT] wrote {path}")
    if wrote_resource_table:
        print(f"[PLOT] wrote {resource_table_path}")
    if wrote_component_table:
        print(f"[PLOT] wrote {component_table_path}")
    for path in dissection_paths:
        print(f"[PLOT] wrote {path}")
    for filename in (
        "throughput_requests_offered_achieved.pdf",
        "throughput_handshakes_offered_achieved.pdf",
        "latency_vs_offered.pdf",
        "cpu_timeseries.pdf",
        "memory_timeseries.pdf",
        "cpu_vs_offered.pdf",
        "startup_times.pdf",
        "worker_startup_distribution.pdf",
        "handshake_request_duration.pdf",
        "dc_reuse_handshake_latency.pdf",
        "handler_request_latency.pdf",
        "scalability_closed_loop.pdf",
    ):
        path = out_dir / filename
        if path.exists():
            print(f"[PLOT] wrote {path}")


if __name__ == "__main__":
    main()
