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
    "iteration",
    "offered_rps",
    "steady_duration_s",
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
    "mean_latency_ms",
    "p99_latency_ms",
    "mean_handshake_ms",
    "p99_handshake_ms",
    "mean_latency_ms_filtered",
    "mean_cpu_percent",
    "p95_cpu_percent",
    "peak_cpu_percent",
    "median_memory_mib",
    "peak_memory_mib",
    "startup_process_ready_ms",
    "startup_first_worker_ready_ms",
    "startup_worker_ready_internal_ms",
    "point_quality",
    "point_quality_reasons",
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
    has_per_client_priming = False

    with client_csv.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            request_id = row.get("id", "")
            if not request_id:
                continue
            if request_id.startswith("warmup-client-"):
                has_per_client_priming = True
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
    if mode == "resumption" and not has_per_client_priming:
        first_start_by_client: dict[str, int] = {}
        for request_id, event in events_by_id.items():
            start_ts = event.get("start_ts")
            if start_ts is None:
                continue
            client_id = logical_client_id(request_id)
            previous = first_start_by_client.get(client_id)
            if previous is None or start_ts < previous:
                first_start_by_client[client_id] = start_ts

        configured_clients = parse_int(clients)
        if configured_clients and len(first_start_by_client) >= configured_clients:
            priming_end = max(first_start_by_client.values())
            steady_start = max(steady_start or priming_end, priming_end)
        else:
            steady_start = None
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
            or row["tls_resumed"] is True
        )
    )
    resumption_fallbacks = sum(
        1
        for row in records
        if mode == "resumption"
        and row["measurement_window"]
        and row["tls_success"]
        and (has_per_client_priming or not row["_initial_client_request"])
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
        handshake_latencies = [
            float(sample["handshake_ms"])
            for sample in steady_ok
            if sample["handshake_ms"] != ""
            and (
                str(summary["mode"]) != "resumption"
                or sample.get("tls_resumed") == "true"
            )
        ]
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

        row = {
                "campaign": summary["campaign"],
                "run_name": summary["run_name"],
                "deployment": summary["deployment"],
                "mode": summary["mode"],
                "clients": summary["clients"],
                "iteration": summary["iteration"],
                "offered_rps": f"{float(summary['rate']):.6f}" if parse_float(summary["rate"]) is not None else summary["rate"],
                "steady_duration_s": f"{steady_duration_s:.6f}",
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
                "mean_latency_ms": f"{mean(latencies):.6f}" if latencies else "",
                "p99_latency_ms": f"{percentile(latencies, 99):.6f}" if latencies else "",
                "mean_handshake_ms": f"{mean(handshake_latencies):.6f}" if handshake_latencies else "",
                "p99_handshake_ms": f"{percentile(handshake_latencies, 99):.6f}" if handshake_latencies else "",
                "mean_latency_ms_filtered": f"{mean(filtered_latencies):.6f}" if filtered_latencies else "",
                "mean_cpu_percent": f"{mean(cpu_values):.6f}" if cpu_values else "",
                "p95_cpu_percent": f"{percentile(cpu_values, 95):.6f}" if cpu_values else "",
                "peak_cpu_percent": f"{max(cpu_values):.6f}" if cpu_values else "",
                "median_memory_mib": f"{percentile(memory_values, 50):.6f}" if memory_values else "",
                "peak_memory_mib": f"{max(memory_values):.6f}" if memory_values else "",
                "startup_process_ready_ms": startup.get("process_ready_ms", ""),
                "startup_first_worker_ready_ms": startup.get("first_worker_ready_ms", ""),
                "startup_worker_ready_internal_ms": startup.get("worker_ready_internal_ms", ""),
            }
        quality, reasons = point_quality(row)
        row["point_quality"] = quality
        row["point_quality_reasons"] = ";".join(reasons)
        rows.append(row)
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
            elif metric == "worker_ready_internal" and startup_ms:
                metrics.setdefault("worker_ready_internal_ms", startup_ms)
    return metrics


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


def render_violin(
    groups: list[tuple[str, list[float], list[float]]],
    output_path: Path,
    title: str,
    y_label: str,
    log_density: bool = False,
) -> None:
    figure_size = (8.0, 5.0)
    violin_color = "#77aadd"
    mean_color = "#023047"
    percentile_clip = 95

    groups = [(label, values, run_means) for label, values, run_means in groups if values]
    if not groups:
        print(f"[PLOT] no valid samples for {output_path.name}")
        return

    data: list[list[float]] = []
    group_means: list[float] = []
    group_cis: list[float] = []
    for _, original_values, run_means in groups:
        cutoff = percentile(original_values, percentile_clip)
        values = [value for value in original_values if value <= cutoff and value > 0]
        data.append([math.log10(value) for value in values] if log_density else values)
        group_means.append(mean(run_means))
        group_cis.append(confidence_interval_95(run_means) if len(run_means) > 1 else 0.0)

    fig, ax = plt.subplots(figsize=figure_size)
    parts = ax.violinplot(data, showmeans=False, showmedians=False, showextrema=False)
    for body in parts["bodies"]:
        body.set_facecolor(violin_color)
        body.set_edgecolor("none")
        body.set_alpha(0.65)

    ax.set_xticks(range(1, len(groups) + 1))
    ax.set_xticklabels([label for label, _, _ in groups], rotation=12, ha="right")
    ax.set_ylabel(y_label)
    title = f"{title} (violin bodies clipped at p{percentile_clip:g})"
    ax.set_title(title)
    ax.grid(True, axis="y", alpha=0.25)

    if log_density:
        plotted = [value for values in data for value in values]
        low = math.floor(min(plotted))
        high = math.ceil(max(plotted))
        ticks = [float(power) for power in range(low, high + 1)]
        ax.set_yticks(ticks)
        ax.set_yticklabels([f"{10 ** tick:g}" for tick in ticks])
    else:
        ax.set_ylim(bottom=0)

    for index, (mean_latency, ci) in enumerate(zip(group_means, group_cis), start=1):
        mean_position = math.log10(mean_latency) if log_density else mean_latency
        if ci > 0:
            if log_density:
                lower = math.log10(max(mean_latency - ci, mean_latency * 0.01))
                upper = math.log10(mean_latency + ci)
                yerr = [[mean_position - lower], [upper - mean_position]]
            else:
                yerr = ci
            ax.errorbar(
                [index],
                [mean_position],
                yerr=yerr,
                fmt="o",
                color=mean_color,
                capsize=3,
                markersize=4,
                zorder=5,
            )
        else:
            ax.scatter([index], [mean_position], color=mean_color, marker="o", s=20, zorder=5)
        ax.text(
            index + 0.18,
            mean_position,
            f"avg {mean_latency:.2f} ms",
            ha="left",
            va="center",
            color=mean_color,
            fontsize=8,
            clip_on=False,
        )

    fig.tight_layout()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, bbox_inches="tight")
    plt.close(fig)


def plot_paper_latency_violins(
    samples: list[dict[str, Any]],
    summary_rows: list[dict[str, Any]],
    output_dir: Path,
) -> list[Path]:
    summary_by_run = {str(row["run_name"]): row for row in summary_rows}
    handshake_values: dict[tuple[str, str], dict[str, list[float]]] = {}
    request_values: dict[str, dict[str, list[float]]] = {}

    for sample in samples:
        if sample.get("status") != "ok":
            continue
        row = summary_by_run.get(str(sample["run_name"]))
        if row is None or not point_is_usable(row) or not is_canonical_paper_variant(row):
            continue
        clients = parse_int(sample.get("clients"))
        rate = parse_float(sample.get("rate"))
        mode = str(sample["mode"])
        strategy = timeseries_group(str(sample["deployment"]))[0]
        run_name = str(sample["run_name"])
        if rate is None:
            continue

        handshake = parse_float(sample.get("handshake_ms"))
        if mode == "fresh" and math.isclose(rate, 1.0) and sample.get("steady_state") == "true" and handshake is not None:
            handshake_values.setdefault((strategy, "full"), {}).setdefault(run_name, []).append(handshake)
        elif (
            mode == "resumption"
            and clients == 1
            and math.isclose(rate, 10.0)
            and sample.get("measurement_window") == "true"
            and sample.get("tls_resumed") == "true"
            and handshake is not None
        ):
            handshake_values.setdefault((strategy, "resumed"), {}).setdefault(run_name, []).append(handshake)

        latency = parse_float(sample.get("latency_ms"))
        if (
            mode == "persistent"
            and clients == 1
            and math.isclose(rate, 10.0)
            and sample.get("steady_state") == "true"
            and latency is not None
        ):
            request_values.setdefault(strategy, {}).setdefault(run_name, []).append(latency)

    handshake_order = [
        (strategy, kind)
        for strategy in STRATEGY_ORDER
        for kind in ("full", "resumed")
        if (strategy, kind) in handshake_values
    ]
    paper_strategies = {"direct", "baremetal", "sgx", "docker", "docker_sgx"}
    complete_handshake = (
        {(strategy, "full") for strategy in paper_strategies}
        | {("docker", "resumed"), ("docker_sgx", "resumed")}
    ).issubset(handshake_values)
    complete_request = paper_strategies.issubset(request_values)

    def violin_group(label: str, by_run: dict[str, list[float]]) -> tuple[str, list[float], list[float]]:
        return (
            label,
            [value for values in by_run.values() for value in values],
            [mean(values) for values in by_run.values() if values],
        )

    handshake_linear_path = output_dir / "P3a-handshake-latency-distribution-linear.pdf"
    handshake_log_path = output_dir / "P3a-handshake-latency-distribution-log.pdf"
    if complete_handshake:
        handshake_groups = [
            violin_group(
                f"{pretty_strategy_label(strategy)}{' (Resumption)' if kind == 'resumed' else ''}",
                handshake_values[(strategy, kind)],
            )
            for strategy, kind in handshake_order
        ]
        render_violin(
            handshake_groups,
            handshake_linear_path,
            "Full and resumed TLS handshake latency",
            "Handshake latency (ms)",
        )
        render_violin(
            handshake_groups,
            handshake_log_path,
            "Full and resumed TLS handshake latency",
            "Handshake latency (ms, logarithmic density)",
            log_density=True,
        )
    else:
        print("[PLOT] incomplete full/resumed handshake matrix; skipping P3a figures")

    request_path = output_dir / "P3b-persistent-request-latency-distribution.pdf"
    if complete_request:
        render_violin(
            [
                violin_group(pretty_strategy_label(strategy), request_values[strategy])
                for strategy in STRATEGY_ORDER
                if strategy in request_values
            ],
            request_path,
            "Persistent request latency (1 client, 10 requests/s)",
            "End-to-end request latency (ms)",
        )
    else:
        print("[PLOT] incomplete persistent request matrix; skipping P3b figure")
    return [
        path
        for path in (handshake_linear_path, handshake_log_path, request_path)
        if path.exists()
    ]


STRATEGY_ORDER = ["direct", "baremetal", "sgx", "sgxgo", "docker", "docker_sgx"]
STRATEGY_COLORS = {
    "direct": "#457b9d",
    "baremetal": "#2a9d8f",
    "sgx": "#e76f51",
    "sgxgo": "#bc6c25",
    "docker": "#f4a261",
    "docker_sgx": "#6a4c93",
}

# Paper-quality filtering knob. Invalid points remain visible as red crosses but
# are not connected to the valid operating curves.
SHOW_RUN_QUALITY_WARNINGS = True
INCLUDE_CLOSED_LOOP_OPERATING_POINTS = False  # enable after every strategy has a comparable closed-loop run


def row_quality_flags(row: dict[str, Any]) -> set[str]:
    return {
        flag.strip()
        for flag in str(row.get("quality_flags", "")).split(";")
        if flag.strip()
    }


def point_quality(row: dict[str, Any]) -> tuple[str, list[str]]:
    invalid: list[str] = []
    warnings: list[str] = []

    def add(target: list[str], reason: str) -> None:
        if reason not in target:
            target.append(reason)

    failed_requests = parse_int(row.get("failed")) or 0
    request_errors = parse_int(row.get("errors")) or 0
    gateway_drops = parse_int(row.get("gateway_drops")) or 0
    tolerated_single_fresh_failure = (
        str(row.get("mode")) == "fresh"
        and not row_failed(row)
        and failed_requests == 1
        and request_errors == 1
        and (parse_int(row.get("non2xx")) or 0) == 0
        and (parse_int(row.get("timeouts")) or 0) == 0
        and gateway_drops <= 1
        and (parse_int(row.get("success")) or 0) >= 10
    )
    if tolerated_single_fresh_failure:
        add(warnings, "single isolated fresh transport failure excluded")

    if row_failed(row):
        reason = str(row.get("failure_reason", "")).strip()
        add(invalid, f"failed run: {reason}" if reason else "failed run")

    for key, label in (
        ("failed", "failed requests"),
        ("non2xx", "non-2xx responses"),
        ("errors", "request errors"),
        ("timeouts", "request timeouts"),
        ("gateway_drops", "gateway drops"),
    ):
        count = parse_int(row.get(key)) or 0
        if count > 0:
            if tolerated_single_fresh_failure and key in {"failed", "errors", "gateway_drops"}:
                continue
            add(invalid, f"{label}={count}")

    fallbacks = parse_int(row.get("resumption_fallbacks")) or 0
    if str(row.get("mode")) == "resumption" and fallbacks > 0:
        add(invalid, f"full-handshake fallbacks={fallbacks}")

    configured = parse_int(row.get("configured_clients"))
    participating = parse_int(row.get("participating_clients"))
    if configured is not None and participating is not None and participating < configured:
        add(invalid, f"clients={participating}/{configured}")

    offered = parse_float(row.get("offered_rps"))
    achieved = parse_float(row.get("achieved_rps"))
    scheduled = parse_float(row.get("scheduled_rps"))
    steady_duration = parse_float(row.get("steady_duration_s")) or 0.0
    if offered is not None and offered > 0:
        boundary_rps = 1.1 / steady_duration if steady_duration > 0 else 0.0
        material_deficit = max(offered * 0.02, boundary_rps)
        if tolerated_single_fresh_failure:
            material_deficit += boundary_rps
        if scheduled is not None and offered - scheduled > material_deficit:
            add(invalid, f"scheduled/offered={scheduled / offered:.3f}")
        elif scheduled is not None and scheduled < offered:
            add(warnings, "one-boundary-slot scheduled deficit")
        if achieved is not None and offered - achieved > material_deficit:
            add(invalid, f"achieved/offered={achieved / offered:.3f}")
        elif achieved is not None and achieved < offered:
            add(warnings, "one-boundary-slot achieved deficit")

    invalid_flags = {
        "partial_client_participation",
        "request_timeouts",
        "serial_client_concurrency_limited",
    }
    warning_flags = {"inflight_limit_hit", "unfinished_at_schedule_end", "offered_load_not_reached"}
    for flag in sorted(row_quality_flags(row)):
        if flag in invalid_flags:
            add(invalid, flag)
        elif flag in warning_flags:
            add(warnings, flag)
        else:
            add(warnings, flag)

    late_slots = parse_int(row.get("late_slots")) or 0
    if late_slots > 0 and not invalid:
        add(warnings, f"late slots={late_slots}")

    if invalid:
        return "invalid", invalid + warnings
    if warnings:
        return "warning", warnings
    return "valid", []


def is_canonical_paper_variant(row: dict[str, Any]) -> bool:
    strategy, _, handler, reuse = timeseries_group(str(row["deployment"]))
    mode = str(row["mode"])
    deployment = str(row["deployment"])
    if strategy == "direct":
        return True
    if handler != "full":
        return False
    if mode == "resumption":
        return strategy in {"docker", "docker_sgx"} and "_resumption" in deployment
    if mode == "persistent":
        if strategy in {"docker", "docker_sgx"}:
            # DC reuse does not affect steady requests on an already persistent
            # worker connection; campaigns contain both labels.
            return reuse in {"reuse", "no reuse"}
        return reuse == "reuse"
    if mode == "fresh":
        if strategy in {"docker", "docker_sgx"}:
            return reuse == "no reuse"
        return reuse == "reuse"
    return False


def point_is_usable(row: dict[str, Any]) -> bool:
    return str(row.get("point_quality") or point_quality(row)[0]) != "invalid"


def draw_quality_marker(ax: Any, row: dict[str, Any], metric: str) -> None:
    quality = str(row.get("point_quality") or point_quality(row)[0])
    if quality == "valid":
        return
    x_value = parse_float(row.get("offered_rps"))
    y_value = parse_float(row.get(metric))
    if x_value is None or x_value <= 0 or y_value is None:
        return
    invalid = quality == "invalid"
    color = "red" if invalid else "#cc7a00"
    marker = "x" if invalid else "^"
    ax.scatter([x_value], [y_value], marker=marker, color=color, s=32, zorder=5)


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
    if strategy == "direct" and not include_direct:
        return False
    if not is_canonical_paper_variant(row):
        return False
    return True


def row_failed(row: dict[str, Any]) -> bool:
    return str(row.get("run_status", "unknown")).lower() == "failed" or (
        parse_int(row.get("client_returncode")) not in {None, 0}
    )


def row_diagnostics(row: dict[str, Any]) -> str:
    quality = str(row.get("point_quality", ""))
    reasons = str(row.get("point_quality_reasons", "")).strip(";")
    if quality and reasons:
        return f"{quality}: {reasons.replace(';', ', ')}"
    computed_quality, computed_reasons = point_quality(row)
    return f"{computed_quality}: {', '.join(computed_reasons)}" if computed_reasons else ""


def print_quality_summary(summary_rows: list[dict[str, Any]]) -> None:
    for row in summary_rows:
        quality = str(row.get("point_quality", "valid"))
        if quality == "valid":
            continue
        marker = "X" if quality == "invalid" else "^"
        reasons = str(row.get("point_quality_reasons", "")).replace(";", "; ")
        print(f"[PLOT QUALITY] marker={marker} run={row['run_name']} reasons={reasons}")


def set_plain_log_xaxis(ax: Any, values: list[float]) -> None:
    positive = sorted({value for value in values if value > 0})
    if not positive:
        return
    ax.set_xscale("log")
    preferred = [1.0, 10.0, 100.0, 1000.0, 5000.0]
    ticks = [value for value in preferred if value in positive]
    if positive[0] not in ticks:
        ticks.insert(0, positive[0])
    if positive[-1] not in ticks:
        ticks.append(positive[-1])
    ax.set_xticks(ticks)
    ax.set_xticklabels([f"{value:g}" for value in ticks])


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
        if not include_throughput_row(row, include_direct, modes, clients) or not point_is_usable(row):
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
    clients: int = 1,
    logarithmic_x: bool = False,
) -> None:
    grouped = aggregate_throughput_metric_by_mode(summary_rows, metric, include_direct, modes=modes, clients=clients)
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
                if not include_throughput_row(row, include_direct, (mode,), clients=clients):
                    continue
                draw_quality_marker(ax, row, metric)

        if logarithmic_x:
            set_plain_log_xaxis(
                ax,
                [point[0] for points in mode_data.values() for point in points],
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
    clients: int = 1,
    logarithmic_x: bool = False,
    logarithmic_y: bool = False,
) -> None:
    grouped = aggregate_throughput_metric_by_mode(summary_rows, metric, True, modes=modes, clients=clients)
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
        ideal_start = min(
            point[0]
            for mode in modes
            for points in grouped.get(mode, {}).values()
            for point in points
        ) if logarithmic_x else 0.0
        ax.plot(
            [ideal_start, max_offered],
            [ideal_start, max_offered],
            color="#666666",
            linestyle=":",
            linewidth=1.0,
            label="ideal",
        )

    if show_failure_annotations:
        for row in summary_rows:
            if not include_throughput_row(row, True, modes, clients=clients):
                continue
            draw_quality_marker(ax, row, metric)

    ax.set_xlabel(xlabel)
    ax.set_ylabel(ylabel)
    ax.set_title(title)
    ax.grid(True, alpha=0.25)
    if logarithmic_x:
        set_plain_log_xaxis(
            ax,
            [point[0] for mode in modes for points in grouped.get(mode, {}).values() for point in points],
        )
    else:
        ax.set_xlim(left=0)
    if logarithmic_y:
        ax.set_yscale("log")
    else:
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
        logarithmic_x=True,
    )


HANDSHAKE_CAPACITY_CLIENTS = 10
HANDSHAKE_CAPACITY_EXPECTED = {
    ("direct", "fresh"),
    ("baremetal", "fresh"),
    ("sgx", "fresh"),
    ("docker", "fresh"),
    ("docker_sgx", "fresh"),
    ("docker", "resumption"),
    ("docker_sgx", "resumption"),
}


def handshake_capacity_rows(summary_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        row
        for row in summary_rows
        if include_throughput_row(
            row,
            True,
            ("fresh", "resumption"),
            clients=HANDSHAKE_CAPACITY_CLIENTS,
        )
        and (parse_float(row.get("offered_rps")) or 0) > 0
    ]


def has_complete_handshake_capacity_data(summary_rows: list[dict[str, Any]]) -> bool:
    available = {
        (timeseries_group(str(row["deployment"]))[0], str(row["mode"]))
        for row in handshake_capacity_rows(summary_rows)
    }
    return HANDSHAKE_CAPACITY_EXPECTED.issubset(available)


def plot_handshake_throughput(summary_rows: list[dict[str, Any]], output_path: Path) -> None:
    if not has_complete_handshake_capacity_data(summary_rows):
        print(f"[PLOT] incomplete handshake-capacity matrix; skipping {output_path.name}")
        return
    plot_throughput_family(
        summary_rows,
        output_path,
        modes=("fresh", "resumption"),
        metric="achieved_handshakes_rps",
        xlabel="Offered throughput (handshakes/s)",
        ylabel="Achieved throughput (handshakes/s)",
        title=f"Full and Resumed Handshake Throughput ({HANDSHAKE_CAPACITY_CLIENTS} clients)",
        clients=HANDSHAKE_CAPACITY_CLIENTS,
        logarithmic_x=True,
        logarithmic_y=True,
    )


def plot_latency_vs_offered(summary_rows: list[dict[str, Any]], output_path: Path) -> None:
    plot_metric_vs_offered(
        summary_rows,
        output_path,
        metric="p99_latency_ms",
        ylabel="p99 end-to-end latency (ms)",
        title="Offered Throughput vs p99 End-to-End Latency",
        include_direct=True,
        logarithmic_x=True,
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
        logarithmic_x=True,
    )


def plot_single_session_operating_curve(summary_rows: list[dict[str, Any]], output_path: Path) -> None:
    available: dict[str, set[float]] = {}
    for row in summary_rows:
        if not include_throughput_row(row, True, ("persistent",), clients=1):
            continue
        offered = parse_float(row.get("offered_rps")) or 0
        if offered > 0:
            strategy = timeseries_group(str(row["deployment"]))[0]
            available.setdefault(strategy, set()).add(offered)
    required = {"direct", "baremetal", "sgx", "docker", "docker_sgx"}
    if not required.issubset(available) or any(len(available[strategy]) < 2 for strategy in required):
        print(f"[PLOT] incomplete five-strategy operating matrix; skipping {output_path.name}")
        return

    metrics = [
        ("mean_latency_ms", "Mean end-to-end latency (ms)"),
        ("mean_cpu_percent", "Total CPU usage (%)"),
    ]
    fig, axes = plt.subplots(1, 2, figsize=(11.0, 4.5), squeeze=False)
    handles_by_label: dict[str, Any] = {}

    for ax, (metric, ylabel) in zip(axes.flatten(), metrics):
        grouped = aggregate_throughput_metric_by_mode(
            summary_rows,
            metric,
            include_direct=metric != "mean_cpu_percent",
            modes=("persistent",),
            clients=1,
        ).get("persistent", {})
        for strategy in STRATEGY_ORDER:
            points = grouped.get(strategy)
            if not points:
                continue
            line = ax.errorbar(
                [point[0] for point in points],
                [point[1] for point in points],
                yerr=[point[2] for point in points],
                marker="o",
                linewidth=1.4,
                capsize=3,
                color=STRATEGY_COLORS.get(strategy),
                label=pretty_strategy_label(strategy),
            )
            handles_by_label.setdefault(pretty_strategy_label(strategy), line.lines[0])

        for row in summary_rows:
            if not include_throughput_row(
                row,
                include_direct=metric != "mean_cpu_percent",
                modes=("persistent",),
                clients=1,
            ):
                continue
            draw_quality_marker(ax, row, metric)

        if INCLUDE_CLOSED_LOOP_OPERATING_POINTS:
            for row in summary_rows:
                if not include_throughput_row(
                    row,
                    include_direct=metric != "mean_cpu_percent",
                    modes=("persistent",),
                    clients=1,
                ) or parse_float(row.get("offered_rps")) != 0 or not point_is_usable(row):
                    continue
                x_value = parse_float(row.get("achieved_rps"))
                y_value = parse_float(row.get(metric))
                strategy = timeseries_group(str(row["deployment"]))[0]
                if x_value is not None and x_value > 0 and y_value is not None:
                    ax.scatter(
                        [x_value],
                        [y_value],
                        marker="*",
                        s=70,
                        color=STRATEGY_COLORS.get(strategy),
                        zorder=5,
                    )

        set_plain_log_xaxis(
            ax,
            [point[0] for points in grouped.values() for point in points],
        )
        ax.set_xlabel("Offered throughput (requests/s)")
        ax.set_ylabel(ylabel)
        ax.set_ylim(bottom=0)
        ax.grid(True, alpha=0.25)

    fig.suptitle("Single-client persistent operating curve", fontsize=13)
    if handles_by_label:
        fig.legend(
            list(handles_by_label.values()),
            list(handles_by_label.keys()),
            loc="lower center",
            ncol=min(5, len(handles_by_label)),
            fontsize=8,
        )
    fig.tight_layout(rect=[0, 0.10, 1, 0.95])
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, bbox_inches="tight")
    plt.close(fig)


def plot_handshake_latency_capacity(summary_rows: list[dict[str, Any]], output_path: Path) -> None:
    if not has_complete_handshake_capacity_data(summary_rows):
        print(f"[PLOT] incomplete handshake-capacity matrix; skipping {output_path.name}")
        return
    plot_metric_vs_offered(
        summary_rows,
        output_path,
        metric="mean_handshake_ms",
        ylabel="Mean TLS handshake latency (ms)",
        title=f"Handshake Mean Latency Under Load ({HANDSHAKE_CAPACITY_CLIENTS} clients)",
        include_direct=True,
        modes=("fresh", "resumption"),
        clients=HANDSHAKE_CAPACITY_CLIENTS,
        logarithmic_x=True,
    )


def plot_aggregate_scalability(summary_rows: list[dict[str, Any]], output_path: Path) -> None:
    strategies = ("baremetal", "sgx", "docker", "docker_sgx")
    fixed_rate = 10.0
    saturated: dict[str, dict[int, tuple[float, float]]] = {}
    fixed_latency: dict[str, dict[int, tuple[float, float]]] = {}

    for strategy in strategies:
        rows = [
            row
            for row in summary_rows
            if timeseries_group(str(row["deployment"]))[0] == strategy
            and include_throughput_row(row, True, ("persistent",), clients=None)
        ]
        by_clients_rate: dict[int, dict[float, list[dict[str, Any]]]] = {}
        for row in rows:
            clients = parse_int(row.get("clients"))
            offered = parse_float(row.get("offered_rps"))
            if clients is None or offered is None:
                continue
            by_clients_rate.setdefault(clients, {}).setdefault(offered, []).append(row)

        for clients, by_rate in by_clients_rate.items():
            closed_rows = by_rate.get(0.0, [])
            achieved = [
                value
                for row in closed_rows
                if point_is_usable(row)
                and (value := parse_float(row.get("achieved_rps"))) is not None
            ]
            if achieved:
                saturated.setdefault(strategy, {})[clients] = (
                    mean(achieved),
                    confidence_interval_95(achieved),
                )

            point_rows = by_rate.get(fixed_rate, [])
            latency = [
                value
                for row in point_rows
                if point_is_usable(row)
                and (value := parse_float(row.get("mean_latency_ms"))) is not None
            ]
            if latency:
                fixed_latency.setdefault(strategy, {})[clients] = (
                    mean(latency),
                    confidence_interval_95(latency),
                )

    complete = all(len(saturated.get(strategy, {})) >= 2 and len(fixed_latency.get(strategy, {})) >= 2 for strategy in strategies)
    if not complete:
        print(f"[PLOT] incomplete four-strategy scalability matrix; skipping {output_path.name}")
        return

    fig, axes = plt.subplots(1, 2, figsize=(11.0, 4.5), squeeze=False)
    capacity_ax, latency_ax = axes.flatten()
    for strategy in strategies:
        points = sorted(saturated.get(strategy, {}).items())
        if points:
            capacity_ax.errorbar(
                [clients for clients, _ in points],
                [value[0] for _, value in points],
                yerr=[value[1] for _, value in points],
                marker="o",
                linewidth=1.4,
                capsize=3,
                color=STRATEGY_COLORS.get(strategy),
                label=pretty_strategy_label(strategy),
            )

        points = sorted(fixed_latency.get(strategy, {}).items())
        if points:
            latency_ax.errorbar(
                [clients for clients, _ in points],
                [value[0] for _, value in points],
                yerr=[value[1] for _, value in points],
                marker="o",
                linewidth=1.4,
                capsize=3,
                color=STRATEGY_COLORS.get(strategy),
                label=pretty_strategy_label(strategy),
            )

    capacity_ax.set_ylabel("Maximum throughput at saturation (requests/s)")
    capacity_ax.set_title("Closed-loop saturated throughput")
    capacity_ax.set_yscale("log")
    latency_ax.set_ylabel("Mean end-to-end latency (ms)")
    latency_ax.set_title(f"Fixed aggregate load: {fixed_rate:g} requests/s")
    client_ticks = sorted(
        {
            clients
            for values in (saturated, fixed_latency)
            for by_clients in values.values()
            for clients in by_clients
        }
    )
    for ax in (capacity_ax, latency_ax):
        ax.set_xlabel("Persistent clients")
        ax.set_xscale("log", base=2)
        ax.set_xticks(client_ticks)
        ax.set_xticklabels([str(clients) for clients in client_ticks])
        if ax is not capacity_ax:
            ax.set_ylim(bottom=0)
        ax.grid(True, alpha=0.25)
        ax.legend(fontsize=8)

    fig.suptitle("Middlebox client scalability", fontsize=13)
    fig.tight_layout(rect=[0, 0, 1, 0.95])
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, bbox_inches="tight")
    plt.close(fig)


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
        if not include_throughput_row(row, include_direct=True, modes=(mode,), clients=None) or not point_is_usable(row):
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
                set_plain_log_xaxis(
                    ax,
                    [
                        point[0]
                        for clients in client_counts
                        for point in values_by_clients.get(clients, [])
                    ],
                )
                ax.legend(fontsize=8)

            fig.suptitle(f"{pretty_strategy_label(strategy)} {mode.capitalize()} Scalability", fontsize=13)
            fig.tight_layout(rect=[0, 0, 1, 0.95])
            output_path = output_dir / f"analysis-scalability-{strategy}-{mode}.pdf"
            output_path.parent.mkdir(parents=True, exist_ok=True)
            fig.savefig(output_path, bbox_inches="tight")
            plt.close(fig)
            written.append(output_path)

    return written


def latex_escape(value: Any) -> str:
    text = str(value)
    for old, new in (("\\", r"\textbackslash{}"), ("_", r"\_"), ("%", r"\%"), ("&", r"\&")):
        text = text.replace(old, new)
    return text


def latex_mean_ci(values: list[float], digits: int = 2) -> str:
    if not values:
        return "--"
    average = mean(values)
    if len(values) == 1:
        return f"{average:.{digits}f}"
    return f"{average:.{digits}f} $\\pm$ {confidence_interval_95(values):.{digits}f}"


def write_sustainable_capacity_latex(summary_rows: list[dict[str, Any]], output_path: Path) -> bool:
    grouped: dict[str, dict[float, list[dict[str, Any]]]] = {}
    for row in summary_rows:
        if not include_throughput_row(row, True, ("persistent",), clients=1):
            continue
        offered = parse_float(row.get("offered_rps"))
        if offered is None or offered <= 0:
            continue
        strategy = timeseries_group(str(row["deployment"]))[0]
        grouped.setdefault(strategy, {}).setdefault(offered, []).append(row)

    required = {"direct", "baremetal", "sgx", "docker", "docker_sgx"}
    if not required.issubset(grouped) or any(len(grouped[strategy]) < 2 for strategy in required):
        print(f"[PLOT] incomplete five-strategy capacity matrix; skipping {output_path.name}")
        return False

    selected: list[tuple[str, float, list[dict[str, Any]]]] = []
    for strategy, by_rate in grouped.items():
        sustainable = [
            (rate, rows)
            for rate, rows in by_rate.items()
            if rows and all(point_is_usable(row) for row in rows)
        ]
        if sustainable:
            rate, rows = max(sustainable, key=lambda item: item[0])
            selected.append((strategy, rate, rows))
    if not selected:
        print(f"[PLOT] no sustainable capacity data for {output_path.name}")
        return False

    rank = {strategy: index for index, strategy in enumerate(STRATEGY_ORDER)}
    lines = [
        r"\begin{tabular}{lrrrrr}",
        r"\toprule",
        r"Deployment & Offered & Achieved & Mean latency & Mean CPU & Runs \\",
        r" & (req/s) & (req/s) & (ms) & (\%) & \\",
        r"\midrule",
    ]
    for strategy, offered, rows in sorted(selected, key=lambda item: rank.get(item[0], 99)):
        achieved = [value for row in rows if (value := parse_float(row.get("achieved_rps"))) is not None]
        latency = [value for row in rows if (value := parse_float(row.get("mean_latency_ms"))) is not None]
        cpu = [value for row in rows if (value := parse_float(row.get("mean_cpu_percent"))) is not None]
        lines.append(
            f"{latex_escape(pretty_strategy_label(strategy))} & {offered:g} & {latex_mean_ci(achieved)} & "
            f"{latex_mean_ci(latency)} & {latex_mean_ci(cpu)} & {len(rows)} \\\\"
        )
    lines.extend((r"\bottomrule", r"\end{tabular}"))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return True


def write_handshake_capacity_latex(summary_rows: list[dict[str, Any]], output_path: Path) -> bool:
    if not has_complete_handshake_capacity_data(summary_rows):
        print(f"[PLOT] incomplete handshake-capacity matrix; skipping {output_path.name}")
        return False
    grouped: dict[tuple[str, str], dict[float, list[dict[str, Any]]]] = {}
    for row in handshake_capacity_rows(summary_rows):
        mode = str(row["mode"])
        offered = parse_float(row.get("offered_rps"))
        assert offered is not None and offered > 0
        strategy = timeseries_group(str(row["deployment"]))[0]
        grouped.setdefault((strategy, mode), {}).setdefault(offered, []).append(row)
    if not grouped:
        print(f"[PLOT] no handshake capacity data for {output_path.name}")
        return False

    rank = {strategy: index for index, strategy in enumerate(STRATEGY_ORDER)}
    lines = [
        r"\begin{tabular}{llrrrrr}",
        r"\toprule",
        r"Deployment & Handshake & Clients & Sustained bracket & Achieved & Mean latency & Runs \\",
        r" & & & (handshakes/s) & (handshakes/s) & (ms) & \\",
        r"\midrule",
    ]
    for (strategy, mode), by_rate in sorted(
        grouped.items(), key=lambda item: (rank.get(item[0][0], 99), item[0][1])
    ):
        valid_rates = [
            rate for rate, rows in by_rate.items() if rows and all(point_is_usable(row) for row in rows)
        ]
        if not valid_rates:
            continue
        lower = max(valid_rates)
        upper_rates = [
            rate
            for rate, rows in by_rate.items()
            if rate > lower and any(not point_is_usable(row) for row in rows)
        ]
        upper = min(upper_rates) if upper_rates else None
        bracket = f"$\\geq {lower:g}$" if upper is None else f"$[{lower:g}, {upper:g})$"
        rows = by_rate[lower]
        achieved = [
            value for row in rows if (value := parse_float(row.get("achieved_handshakes_rps"))) is not None
        ]
        latency = [value for row in rows if (value := parse_float(row.get("mean_handshake_ms"))) is not None]
        lines.append(
            f"{latex_escape(pretty_strategy_label(strategy))} & "
            f"{'Resumed' if mode == 'resumption' else 'Full'} & {HANDSHAKE_CAPACITY_CLIENTS} & {bracket} & "
            f"{latex_mean_ci(achieved)} & {latex_mean_ci(latency)} & {len(rows)} \\\\"
        )
    lines.extend((r"\bottomrule", r"\end{tabular}"))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return True


def write_handshake_capacity_csv(summary_rows: list[dict[str, Any]], output_path: Path) -> bool:
    rows = handshake_capacity_rows(summary_rows)
    if not has_complete_handshake_capacity_data(summary_rows):
        print(f"[PLOT] incomplete handshake-capacity matrix; skipping {output_path.name}")
        return False
    fields = [
        "deployment",
        "handshake_type",
        "clients",
        "iteration",
        "offered_handshakes_rps",
        "achieved_handshakes_rps",
        "mean_handshake_ms",
        "point_quality",
        "point_quality_reasons",
    ]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in sorted(
            rows,
            key=lambda item: (
                STRATEGY_ORDER.index(timeseries_group(str(item["deployment"]))[0]),
                str(item["mode"]),
                parse_float(item.get("offered_rps")) or 0,
                parse_int(item.get("iteration")) or 0,
            ),
        ):
            writer.writerow(
                {
                    "deployment": pretty_strategy_label(timeseries_group(str(row["deployment"]))[0]),
                    "handshake_type": "Resumed" if row["mode"] == "resumption" else "Full",
                    "clients": row["clients"],
                    "iteration": row["iteration"],
                    "offered_handshakes_rps": row["offered_rps"],
                    "achieved_handshakes_rps": row["achieved_handshakes_rps"],
                    "mean_handshake_ms": row["mean_handshake_ms"],
                    "point_quality": row["point_quality"],
                    "point_quality_reasons": row["point_quality_reasons"],
                }
            )
    return True


def write_selected_load_resources_latex(summary_rows: list[dict[str, Any]], output_path: Path) -> bool:
    selected_rates = (10.0, 100.0)  # use 500 later only after every strategy sustains it cleanly
    grouped: dict[tuple[str, float], list[dict[str, Any]]] = {}
    for row in summary_rows:
        if not include_throughput_row(row, True, ("persistent",), clients=1) or not point_is_usable(row):
            continue
        offered = parse_float(row.get("offered_rps"))
        if offered is None or not any(math.isclose(offered, rate) for rate in selected_rates):
            continue
        strategy = timeseries_group(str(row["deployment"]))[0]
        grouped.setdefault((strategy, offered), []).append(row)
    required = {
        (strategy, rate)
        for strategy in ("direct", "baremetal", "sgx", "docker", "docker_sgx")
        for rate in selected_rates
    }
    if not required.issubset(grouped):
        print(f"[PLOT] incomplete selected-load matrix; skipping {output_path.name}")
        return False

    rank = {strategy: index for index, strategy in enumerate(STRATEGY_ORDER)}
    lines = [
        r"\begin{tabular}{lrrrrrr}",
        r"\toprule",
        r"Deployment & Offered & Mean CPU & p95 CPU & Median RSS & Peak RSS & Runs \\",
        r" & (req/s) & (\%) & (\%) & (MiB) & (MiB) & \\",
        r"\midrule",
    ]
    for (strategy, offered), rows in sorted(grouped.items(), key=lambda item: (rank.get(item[0][0], 99), item[0][1])):
        mean_cpu = [value for row in rows if (value := parse_float(row.get("mean_cpu_percent"))) is not None]
        p95_cpu = [value for row in rows if (value := parse_float(row.get("p95_cpu_percent"))) is not None]
        median_memory = [value for row in rows if (value := parse_float(row.get("median_memory_mib"))) is not None]
        peak_memory = [value for row in rows if (value := parse_float(row.get("peak_memory_mib"))) is not None]
        lines.append(
            f"{latex_escape(pretty_strategy_label(strategy))} & {offered:g} & {latex_mean_ci(mean_cpu)} & "
            f"{latex_mean_ci(p95_cpu)} & {latex_mean_ci(median_memory)} & {latex_mean_ci(peak_memory)} & {len(rows)} \\\\"
        )
    lines.extend((r"\bottomrule", r"\end{tabular}"))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return True


def write_clients_memory_latex(summary_rows: list[dict[str, Any]], output_path: Path) -> bool:
    fixed_rate = 10.0
    grouped: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for row in summary_rows:
        if not include_throughput_row(row, False, ("persistent",), clients=None) or not point_is_usable(row):
            continue
        offered = parse_float(row.get("offered_rps"))
        clients = parse_int(row.get("clients"))
        if offered is None or not math.isclose(offered, fixed_rate) or clients is None:
            continue
        strategy = timeseries_group(str(row["deployment"]))[0]
        grouped.setdefault((strategy, clients), []).append(row)
    required = {
        (strategy, clients)
        for strategy in ("baremetal", "sgx", "docker", "docker_sgx")
        for clients in (1, 5, 10, 50)
    }
    if not required.issubset(grouped):
        print(f"[PLOT] incomplete clients/memory matrix; skipping {output_path.name}")
        return False

    rank = {strategy: index for index, strategy in enumerate(STRATEGY_ORDER)}
    lines = [
        r"\begin{tabular}{lrrrrr}",
        r"\toprule",
        r"Deployment & Clients & Median total RSS & Peak total RSS & RSS/client & Runs \\",
        r" & & (MiB) & (MiB) & (MiB) & \\",
        r"\midrule",
    ]
    for (strategy, clients), rows in sorted(grouped.items(), key=lambda item: (rank.get(item[0][0], 99), item[0][1])):
        medians = [value for row in rows if (value := parse_float(row.get("median_memory_mib"))) is not None]
        peaks = [value for row in rows if (value := parse_float(row.get("peak_memory_mib"))) is not None]
        median_total = mean(medians) if medians else 0.0
        lines.append(
            f"{latex_escape(pretty_strategy_label(strategy))} & {clients} & {latex_mean_ci(medians)} & "
            f"{latex_mean_ci(peaks)} & {median_total / clients:.2f} & {len(rows)} \\\\"
        )
    lines.extend((r"\bottomrule", r"\end{tabular}"))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return True


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


def first_event_timestamp_ns(
    index: dict[str, dict[str, list[tuple[int, int]]]],
    event_name: str,
    event_id: str,
) -> int | None:
    events = index.get(event_name, {}).get(event_id, [])
    return events[0][0] if events else None


def interval_ms(start: int | None, done: int | None) -> float:
    if start is None or done is None or done < start:
        return 0.0
    return (done - start) / 1_000_000


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
        # QuoteDone carries the DCAP verification result in arg, not a generic
        # zero-on-success reason code.
        ("certserver", "certserver_quote_verify_by_id", "certserver_quote_done_by_id", "Quote verification", False),
        ("certserver", "certserver_generate_by_id", "certserver_generate_done_by_id", "Delegated credential generation", True),
        ("middlebox", "middlebox_validation_start", "middlebox_validation_done", "Request validation", True),
        ("middlebox", "middlebox_upstream_dial_start", "middlebox_upstream_dial_done", "Upstream TCP dial", True),
        ("middlebox", "middlebox_upstream_tls_start", "middlebox_upstream_tls_done", "Upstream TLS", True),
        ("server", "requestserver_request_start", "requestserver_response_start", "Application handler", False),
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

    required_components = {
        "Delegation retrieval",
        "Quote generation",
        "Quote verification",
        "Delegated credential generation",
        "Request validation",
        "Application handler",
        "Response validation",
    }
    available_components = {component for _, component in grouped}
    if not required_components.issubset(available_components):
        print(f"[PLOT] incomplete component trace matrix; skipping {output_path.name}")
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


def fit_timeline_components(total_ms: float, values: list[float], final_index: int) -> list[float]:
    """Fit adjacent intervals to a total and absorb untraced time into the final path."""
    remaining = max(0.0, total_ms)
    fitted = [0.0] * len(values)
    for index, value in enumerate(values):
        if index == final_index:
            continue
        component = min(max(0.0, value), remaining)
        fitted[index] = component
        remaining -= component
    fitted[final_index] = remaining
    return fitted


def sample_latency_components(
    sample: dict[str, Any],
    client: dict[str, dict[str, list[tuple[int, int]]]],
    middlebox: dict[str, dict[str, list[tuple[int, int]]]],
    certserver: dict[str, dict[str, list[tuple[int, int]]]],
    requestserver: dict[str, dict[str, list[tuple[int, int]]]],
    trace_connections: dict[str, int],
    delegation_by_connection: dict[int, str],
    upstream_by_connection: dict[int, str],
    direct: bool,
) -> tuple[list[float], list[float]]:
    trace_id = str(sample["request_id"])
    connection_key = trace_connections.get(trace_id)
    delegation_id = delegation_by_connection.get(connection_key or 0, "")
    upstream_id = upstream_by_connection.get(connection_key or 0, "")

    handshake_total = parse_float(sample.get("handshake_ms")) or 0.0
    quote_generation = first_event_duration_ms(
        middlebox, "middlebox_attestation_start_by_id", "middlebox_attestation_done_by_id", delegation_id
    )
    quote_verification = first_event_duration_ms(
        certserver, "certserver_quote_verify_by_id", "certserver_quote_done_by_id", delegation_id
    )
    if direct:
        handshake = [handshake_total, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
    else:
        handshake_done = first_event_timestamp_ns(client, "client_tls_done", trace_id)
        dc_start = first_event_timestamp_ns(middlebox, "middlebox_delegation_fetch_by_id", delegation_id)
        dc_done = first_event_timestamp_ns(middlebox, "middlebox_delegation_fetched_by_id", delegation_id)
        quote_start = first_event_timestamp_ns(middlebox, "middlebox_attestation_start_by_id", delegation_id)
        quote_done = first_event_timestamp_ns(middlebox, "middlebox_attestation_done_by_id", delegation_id)
        verify_start = first_event_timestamp_ns(certserver, "certserver_quote_verify_by_id", delegation_id)
        verify_done = first_event_timestamp_ns(certserver, "certserver_quote_done_by_id", delegation_id)
        upstream_start = first_event_timestamp_ns(middlebox, "middlebox_upstream_dial_start", upstream_id)
        upstream_done = first_event_timestamp_ns(middlebox, "middlebox_upstream_tls_done", upstream_id)

        # Upstream setup and delegation retrieval synchronously occur inside the
        # client-observed handshake. Subtract both so the stack does not double-count.
        upstream_tls = min(interval_ms(upstream_start, upstream_done), handshake_total)
        dc_fetch = min(interval_ms(dc_start, dc_done), max(0.0, handshake_total - upstream_tls))
        quote_generation = min(quote_generation, dc_fetch)
        quote_verification = min(quote_verification, max(0.0, dc_fetch - quote_generation))
        dc_remainder = max(0.0, dc_fetch - quote_generation - quote_verification)

        retrieval_gaps = [
            interval_ms(dc_start, quote_start),
            interval_ms(quote_done, verify_start),
            interval_ms(verify_done, dc_done),
        ]
        gap_total = sum(retrieval_gaps)
        if gap_total > 0:
            retrieval_parts = [dc_remainder * gap / gap_total for gap in retrieval_gaps]
        else:
            retrieval_parts = [dc_remainder, 0.0, 0.0]

        downstream_remainder = max(0.0, handshake_total - upstream_tls - dc_fetch)
        downstream_after = min(interval_ms(dc_done, handshake_done), downstream_remainder)
        downstream_before = downstream_remainder - downstream_after
        handshake = [
            0.0,
            downstream_before,
            retrieval_parts[0],
            quote_generation,
            retrieval_parts[1],
            quote_verification,
            retrieval_parts[2],
            downstream_after,
            upstream_tls,
        ]

    client_sent = first_event_timestamp_ns(client, "client_request_sent", trace_id)
    client_done = first_event_timestamp_ns(client, "client_response_done", trace_id)
    middlebox_request_start = first_event_timestamp_ns(middlebox, "middlebox_request_start", trace_id)
    validation_start = first_event_timestamp_ns(middlebox, "middlebox_validation_start", trace_id)
    validation_done = first_event_timestamp_ns(middlebox, "middlebox_validation_done", trace_id)
    response_validation_start = first_event_timestamp_ns(
        middlebox, "middlebox_response_validation_start", trace_id
    )
    response_validation_done = first_event_timestamp_ns(
        middlebox, "middlebox_response_validation_done", trace_id
    )
    server_request_start = first_event_timestamp_ns(
        requestserver, "requestserver_request_start", trace_id
    )
    server_response_start = first_event_timestamp_ns(
        requestserver, "requestserver_response_start", trace_id
    )
    middlebox_downstream_first = first_event_timestamp_ns(
        middlebox, "middlebox_downstream_response_first_byte", trace_id
    )

    request_total = parse_float(sample.get("request_ms")) or 0.0
    if direct:
        request = fit_timeline_components(
            request_total,
            [
                interval_ms(client_sent, server_request_start),
                0.0,
                0.0,
                0.0,
                interval_ms(server_request_start, server_response_start),
                interval_ms(server_response_start, client_done),
                0.0,
                0.0,
                0.0,
                0.0,
            ],
            5,
        )
    else:
        request = fit_timeline_components(
            request_total,
            [
                0.0,
                interval_ms(client_sent, validation_start or middlebox_request_start),
                interval_ms(validation_start, validation_done),
                interval_ms(validation_done, server_request_start),
                interval_ms(server_request_start, server_response_start),
                0.0,
                interval_ms(server_response_start, response_validation_start),
                interval_ms(response_validation_start, response_validation_done),
                interval_ms(response_validation_done, middlebox_downstream_first),
                interval_ms(middlebox_downstream_first, client_done),
            ],
            9,
        )
    return handshake, request


def plot_latency_dissection(
    samples: list[dict[str, Any]],
    run_summaries: list[dict[str, Any]],
    output_dir: Path,
) -> list[Path]:
    rate_by_mode = {"fresh": 1.0, "persistent": 10.0, "resumption": 5.0}
    summaries = {str(summary["run_name"]): summary for summary in run_summaries}
    by_run: dict[str, list[dict[str, Any]]] = {}
    for sample in samples:
        if sample.get("status") != "ok" or sample.get("steady_state") != "true":
            continue
        mode = str(sample.get("mode"))
        rate_filter = rate_by_mode.get(mode)
        if (
            parse_int(sample.get("clients")) != 1
            or rate_filter is None
            or not math.isclose(parse_float(sample.get("rate")) or -1, rate_filter)
        ):
            continue
        by_run.setdefault(str(sample["run_name"]), []).append(sample)

    handshake_labels = [
        "Client-server TLS",
        "Client-middlebox TLS",
        "DC retrieval",
        "Quote generation",
        "DC retrieval",
        "Quote verification",
        "DC retrieval",
        "Client-middlebox TLS",
        "Middlebox-server TLS",
    ]
    handshake_legend_labels = [
        "Client-server TLS",
        "Client-middlebox TLS",
        "DC retrieval",
        "Quote generation",
        "_nolegend_",
        "Quote verification",
        "_nolegend_",
        "_nolegend_",
        "Middlebox-server TLS",
    ]
    request_labels = [
        "Client-server path",
        "Client-middlebox path",
        "Validation",
        "Middlebox-server path",
        "Application handler",
        "Client-server path",
        "Middlebox-server path",
        "Validation",
        "Client-middlebox path",
        "Client-middlebox path",
    ]
    request_legend_labels = [
        "Client-server path",
        "Client-middlebox path",
        "Validation",
        "Middlebox-server path",
        "Application handler",
        "_nolegend_",
        "_nolegend_",
        "_nolegend_",
        "_nolegend_",
        "_nolegend_",
    ]
    handshake_colors = [
        "lightgray",
        "lightskyblue",
        "lightgreen",
        "coral",
        "lightgreen",
        "sandybrown",
        "lightgreen",
        "lightskyblue",
        "mediumseagreen",
    ]
    request_colors = [
        "lightgray",
        "lightskyblue",
        "sandybrown",
        "mediumseagreen",
        "lightgreen",
        "lightgray",
        "mediumseagreen",
        "sandybrown",
        "lightskyblue",
        "lightskyblue",
    ]
    mode_values: dict[str, tuple[dict[str, list[float]], dict[str, list[float]]]] = {}

    for mode in ("fresh", "persistent", "resumption"):
        run_vectors: dict[str, list[tuple[list[float], list[float]]]] = {}
        for run_name, run_samples in by_run.items():
            summary = summaries.get(run_name)
            if not summary or str(summary["mode"]) != mode or summary.get("run_failed"):
                continue
            strategy, _, handler, reuse = timeseries_group(str(summary["deployment"]))
            if strategy != "direct" and handler != "full":
                continue
            if mode == "fresh" and strategy != "direct" and reuse != "no reuse":
                continue
            if mode == "persistent" and strategy != "direct" and reuse == "no reuse":
                continue
            if mode == "resumption" and strategy not in {"docker", "docker_sgx"}:
                continue
            run_dir = Path(str(summary["run_dir"]))
            client = load_trace_index(trace_paths(run_dir, "client"))
            middlebox = load_trace_index(trace_paths(run_dir, "middlebox"))
            certserver = load_trace_index(trace_paths(run_dir, "certserver"))
            requestserver = load_trace_index(trace_paths(run_dir, "server"))
            trace_connections = binding_by_id(middlebox, "middlebox_trace_connection_bind")
            delegation_by_connection = {
                key: event_id
                for event_id, key in binding_by_id(middlebox, "middlebox_delegation_connection_bind").items()
            }
            upstream_by_connection = {
                key: event_id
                for event_id, key in binding_by_id(middlebox, "middlebox_upstream_connection_bind").items()
            }
            vectors = [
                sample_latency_components(
                    sample,
                    client,
                    middlebox,
                    certserver,
                    requestserver,
                    trace_connections,
                    delegation_by_connection,
                    upstream_by_connection,
                    strategy == "direct",
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

        mode_values[mode] = (handshake_values, request_values)

    paper_strategies = {"direct", "baremetal", "sgx", "docker", "docker_sgx"}
    fresh_available = set(mode_values.get("fresh", ({}, {}))[0])
    persistent_available = set(mode_values.get("persistent", ({}, {}))[1])
    resumed_available = set(mode_values.get("resumption", ({}, {}))[0])
    if (
        not paper_strategies.issubset(fresh_available)
        or not paper_strategies.issubset(persistent_available)
        or not {"docker", "docker_sgx"}.issubset(resumed_available)
    ):
        print("[PLOT] incomplete fresh/persistent/resumption trace matrix; skipping P1-P2-latency-dissection.pdf")
        return []

    fresh_handshakes = mode_values["fresh"][0]
    resumed_handshakes = mode_values.get("resumption", ({}, {}))[0]
    persistent_requests = mode_values["persistent"][1]

    # Add future protocols such as TLMSP here after adapting their trace events
    # to the same component vectors.
    handshake_entries: list[tuple[str, list[float]]] = []
    for strategy in STRATEGY_ORDER:
        if strategy in fresh_handshakes:
            handshake_entries.append((pretty_strategy_label(strategy), fresh_handshakes[strategy]))
        if strategy in {"docker", "docker_sgx"} and strategy in resumed_handshakes:
            handshake_entries.append(
                (f"{pretty_strategy_label(strategy)} \n(Resumption)", resumed_handshakes[strategy])
            )
    request_entries = [
        (pretty_strategy_label(strategy), persistent_requests[strategy])
        for strategy in STRATEGY_ORDER
        if strategy in persistent_requests
    ]

    def draw_components(
        ax: Any,
        entries: list[tuple[str, list[float]]],
        labels: list[str],
        colors: list[str],
        title: str,
        legend_labels: list[str] | None = None,
    ) -> tuple[list[Any], list[str]]:
        positions = list(range(len(entries)))
        left = [0.0] * len(entries)
        for index, label in enumerate(labels):
            values = [components[index] for _, components in entries]
            legend_label = legend_labels[index] if legend_labels is not None else label
            ax.barh(
                positions,
                values,
                left=left,
                color=colors[index],
                label=legend_label,
                height=0.8,
            )
            left = [current + value for current, value in zip(left, values)]
        for y, total in enumerate(left):
            ax.text(total, y, f" {total:.2f} ms", va="center")
        ax.set_yticks(positions)
        ax.set_yticklabels([label for label, _ in entries])
        ax.invert_yaxis()
        # ax.set_title(title)
        ax.set_xlabel("Mean latency (ms)")
        ax.set_xlim(left=0, right=max(left) * 1.2)
        ax.margins(y=0.03)
        ax.grid(True, axis="x", alpha=0.25)
        return ax.get_legend_handles_labels()

    fig, axes = plt.subplots(1, 2, figsize=(10.0, 4.0))
    handshake_legend = draw_components(
        axes[0],
        handshake_entries,
        handshake_labels,
        handshake_colors,
        "Connection and TLS handshake",
        handshake_legend_labels,
    )
    request_legend = draw_components(
        axes[1],
        request_entries,
        request_labels,
        request_colors,
        "Request and response",
        request_legend_labels,
    )
    fig.legend(
        handshake_legend[0] + request_legend[0],
        handshake_legend[1] + request_legend[1],
        loc="lower center",
        ncol=5,
        bbox_to_anchor=(0.5, 0.01),
        fontsize="small",
        handlelength=1.4,
        handletextpad=0.5,
        columnspacing=1.2,
    )
    # fig.suptitle("End-to-end latency dissection", y=1.02)
    fig.tight_layout(rect=[0, 0.20, 1, 1], w_pad=1.0)
    combined_path = output_dir / "P1-P2-latency-dissection.pdf"
    # fig.savefig(combined_path, bbox_inches="tight")
    fig.savefig(combined_path)
    plt.close(fig)

    def save_single_panel(
        entries: list[tuple[str, list[float]]],
        labels: list[str],
        colors: list[str],
        title: str,
        path: Path,
        legend_labels: list[str] | None = None,
        legend_ncol: int = 3,
    ) -> None:
        panel_fig, panel_ax = plt.subplots(figsize=(7, 4.0))
        handles, rendered_legend_labels = draw_components(
            panel_ax, entries, labels, colors, title, legend_labels
        )
        panel_fig.legend(
            handles,
            rendered_legend_labels,
            loc="upper right",
            ncol=2,
            bbox_to_anchor=(0.92, 0.99),
            fontsize="small",
            # handlelength=1.4,
            # handletextpad=0.5,
            # columnspacing=1.2,
        )
        legend_rows = math.ceil(len(handles) / legend_ncol)
        legend_bottom = 0.20 + max(0, legend_rows - 3) * 0.045
        panel_fig.subplots_adjust(
            left=0.18,
            right=0.98,
            top=0.98,
            bottom=0.15,
        )
        # panel_fig.tight_layout(rect=[0, legend_bottom, 1, 1])
        # panel_fig.savefig(path, bbox_inches="tight")
        panel_fig.savefig(path)
        plt.close(panel_fig)

    handshake_path = output_dir / "P1-handshake-latency-dissection.pdf"
    request_path = output_dir / "P2-request-latency-dissection.pdf"
    save_single_panel(
        handshake_entries,
        handshake_labels,
        handshake_colors,
        "Connection and TLS handshake",
        handshake_path,
        handshake_legend_labels,
        2,
    )
    save_single_panel(
        request_entries,
        request_labels,
        request_colors,
        "Request and response",
        request_path,
        request_legend_labels,
        2,
    )
    return [combined_path, handshake_path, request_path]


def write_resource_summary_latex(summary_rows: list[dict[str, Any]], output_path: Path) -> bool:
    grouped: dict[tuple[str, str, int, float], list[dict[str, Any]]] = {}
    for row in summary_rows:
        strategy, _, _, _ = timeseries_group(str(row["deployment"]))
        if strategy == "direct" or not point_is_usable(row):
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
    figure_size = (6.0, 4.0)
    categories = [
        ("startup_native", "Baremetal\nProcess", "process_ready_ms"),
        ("startup_gramine_sgx", "SGX\nProcess", "process_ready_ms"),
        ("startup_container", "Container\nWorker", "worker_ready_internal_ms"),
        (
            "startup_container_gramine_sgx",
            "SGX Container\nWorker\n(Standard)",
            "worker_ready_internal_ms",
        ),
        (
            "startup_container_sgxgo",
            "SGX Container\nWorker\n(SGX-Go)",
            "worker_ready_internal_ms",
        ),
    ]
    values_by_deployment: dict[str, list[float]] = {}
    for summary in run_summaries:
        deployment = str(summary["deployment"])
        category = next((item for item in categories if item[0] == deployment), None)
        if category is None:
            continue
        startup = startup_metrics_for_run(summary)
        value = parse_float(startup.get(category[2]))
        if value is not None:
            values_by_deployment.setdefault(deployment, []).append(value)

    active = [category for category in categories if values_by_deployment.get(category[0])]
    if len(active) != len(categories):
        print(f"[PLOT] incomplete five-category startup matrix; skipping {output_path.name}")
        return

    labels = [label for _, label, _ in active]
    means = [mean(values_by_deployment[deployment]) for deployment, _, _ in active]
    errors = [confidence_interval_95(values_by_deployment[deployment]) for deployment, _, _ in active]

    fig, ax = plt.subplots(figsize=figure_size)
    xs = list(range(len(labels)))
    ax.bar(xs, means, yerr=errors, width=0.6, color="dimgray", linewidth=0.8, capsize=4)

    ax.set_xticks(xs)
    ax.set_xticklabels(labels, fontsize=7.5)
    ax.set_ylabel("Startup time (ms)")
    # ax.set_title("Middlebox instance startup time")
    ax.grid(True, axis="y", alpha=0.25)
    ax.set_yscale("log")
    for x, average in zip(xs, means):
        ax.text(x, average * 1.08, f"{average:.1f} ms", ha="center", va="bottom", fontsize=8)
    fig.tight_layout()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, bbox_inches="tight")
    plt.close(fig)


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
    samples_filename = "e2e_latency_samples.csv"
    summary_filename = "run_summary.csv"

    samples, run_summaries = load_campaign(campaign_dir)
    if not run_summaries:
        raise SystemExit(f"no runs with metadata/client.csv found under {campaign_dir}")

    summary_rows = build_run_summary_rows(samples, run_summaries)
    write_samples_csv(samples, out_dir / samples_filename)
    write_summary_csv(summary_rows, out_dir / summary_filename)
    print_quality_summary(summary_rows)
    timeseries_paths = plot_timeseries(samples, run_summaries, out_dir / timeseries_filename)
    paper_violin_paths = plot_paper_latency_violins(samples, summary_rows, out_dir)
    plot_request_throughput(summary_rows, out_dir / "analysis-request-offered-achieved.pdf")
    plot_handshake_throughput(summary_rows, out_dir / "analysis-handshake-offered-achieved.pdf")
    plot_handshake_latency_capacity(summary_rows, out_dir / "analysis-handshake-latency-vs-offered.pdf")
    plot_latency_vs_offered(summary_rows, out_dir / "analysis-latency-vs-offered.pdf")
    plot_cpu_timeseries(run_summaries, out_dir / "cpu_timeseries.pdf")
    plot_memory_timeseries(run_summaries, out_dir / "memory_timeseries.pdf")
    plot_cpu_vs_offered(summary_rows, out_dir / "analysis-cpu-vs-offered.pdf")
    plot_single_session_operating_curve(summary_rows, out_dir / "P5-single-client-throughput-latency-cpu.pdf")
    plot_startup_times(run_summaries, out_dir / "P4-instance-startup-time.pdf")
    scalability_paths = plot_scalability(summary_rows, out_dir)
    plot_aggregate_scalability(summary_rows, out_dir / "P7-client-scalability.pdf")
    resource_table_path = out_dir / "analysis-resource-summary.tex"
    wrote_resource_table = write_resource_summary_latex(summary_rows, resource_table_path)
    component_table_path = out_dir / "T3-component-costs.tex"
    wrote_component_table = write_component_costs_latex(run_summaries, component_table_path)
    sustainable_table_path = out_dir / "P5-single-client-capacity.tex"
    wrote_sustainable_table = write_sustainable_capacity_latex(summary_rows, sustainable_table_path)
    handshake_table_path = out_dir / "T4-handshake-capacity.tex"
    wrote_handshake_table = write_handshake_capacity_latex(summary_rows, handshake_table_path)
    handshake_csv_path = out_dir / "handshake-capacity-summary.csv"
    wrote_handshake_csv = write_handshake_capacity_csv(summary_rows, handshake_csv_path)
    selected_resources_path = out_dir / "T1-selected-load-resources.tex"
    wrote_selected_resources = write_selected_load_resources_latex(summary_rows, selected_resources_path)
    clients_memory_path = out_dir / "T2-clients-memory.tex"
    wrote_clients_memory = write_clients_memory_latex(summary_rows, clients_memory_path)
    dissection_paths = plot_latency_dissection(samples, run_summaries, out_dir)

    print(f"[PLOT] wrote {out_dir / samples_filename}")
    print(f"[PLOT] wrote {out_dir / summary_filename}")
    for path in timeseries_paths:
        print(f"[PLOT] wrote {path}")
    for path in paper_violin_paths:
        print(f"[PLOT] wrote {path}")
    for path in scalability_paths:
        print(f"[PLOT] wrote {path}")
    if wrote_resource_table:
        print(f"[PLOT] wrote {resource_table_path}")
    if wrote_component_table:
        print(f"[PLOT] wrote {component_table_path}")
    for wrote, path in (
        (wrote_sustainable_table, sustainable_table_path),
        (wrote_handshake_table, handshake_table_path),
        (wrote_handshake_csv, handshake_csv_path),
        (wrote_selected_resources, selected_resources_path),
        (wrote_clients_memory, clients_memory_path),
    ):
        if wrote:
            print(f"[PLOT] wrote {path}")
    for path in dissection_paths:
        print(f"[PLOT] wrote {path}")
    for filename in (
        "analysis-request-offered-achieved.pdf",
        "analysis-handshake-offered-achieved.pdf",
        "analysis-handshake-latency-vs-offered.pdf",
        "analysis-latency-vs-offered.pdf",
        "cpu_timeseries.pdf",
        "memory_timeseries.pdf",
        "analysis-cpu-vs-offered.pdf",
        "P5-single-client-throughput-latency-cpu.pdf",
        "P4-instance-startup-time.pdf",
        "P7-client-scalability.pdf",
    ):
        path = out_dir / filename
        if path.exists():
            print(f"[PLOT] wrote {path}")


if __name__ == "__main__":
    main()
