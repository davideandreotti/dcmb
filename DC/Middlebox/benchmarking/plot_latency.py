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
from matplotlib.ticker import FuncFormatter, LogLocator, NullLocator, StrMethodFormatter
from matplotlib.transforms import ScaledTranslation


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
    "protocol",
    "tlmsp_profile",
    "tlmsp_experiment",
    "path_before_request_validation_ms",
    "request_handler_ms",
    "path_to_application_ms",
    "application_ms",
    "path_to_response_validation_ms",
    "response_handler_ms",
    "path_after_response_validation_ms",
    "dissection_ok",
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
    "mean_cpu_percent_p99_filtered",
    "p95_cpu_percent",
    "peak_cpu_percent",
    "median_memory_mib",
    "p99_memory_mib",
    "peak_memory_mib",
    "startup_process_ready_ms",
    "startup_first_worker_ready_ms",
    "startup_worker_ready_internal_ms",
    "point_quality",
    "point_quality_reasons",
    "protocol",
    "tlmsp_profile",
    "tlmsp_experiment",
]

SKIP_CPU_ROLES = {"server", "certserver", "client"}
RESOURCE_SAMPLE_PERCENTILE = 99


def load_campaign(campaign_dir: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    samples: list[dict[str, Any]] = []
    run_summaries: list[dict[str, Any]] = []
    campaign_status = load_campaign_status(campaign_dir / "summary.csv")
    # Imported runs retain their status without rewriting the original campaign.
    imported_status = load_campaign_status(campaign_dir / "summary-imported.csv")
    overlap = campaign_status.keys() & imported_status.keys()
    if overlap:
        raise ValueError(f"duplicate imported run status: {sorted(overlap)}")
    campaign_status.update(imported_status)

    for run_dir in sorted(path for path in campaign_dir.iterdir() if path.is_dir()):
        if run_dir.name == "tlmsp":
            continue
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


def load_tlmsp_campaign(root: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not root.is_dir():
        return [], []

    samples: list[dict[str, Any]] = []
    summaries: list[dict[str, Any]] = []
    seen_runs: set[str] = set()
    for summary_path in sorted(root.rglob("summary.csv")):
        campaign_dir = summary_path.parent
        with summary_path.open(newline="", encoding="utf-8") as handle:
            summary_rows = list(csv.DictReader(handle))
        for source_summary in summary_rows:
            relative_samples = source_summary.get("samples_csv", "")
            sample_path = campaign_dir / relative_samples
            if not relative_samples or not sample_path.is_file():
                continue
            experiment = source_summary.get("experiment", campaign_dir.name)
            profile = source_summary.get("profile", "full")
            mode = source_summary.get("mode", "unknown")
            rate = parse_float(source_summary.get("rate")) or 0.0
            iteration = parse_int(source_summary.get("run")) or 1
            run_name = (
                f"tlmsp-{experiment}-{profile}-{mode}-"
                f"rate{rate:g}-run{iteration:02d}"
            )
            if run_name in seen_runs:
                raise ValueError(f"duplicate TLMSP run {run_name} below {root}")
            seen_runs.add(run_name)

            with sample_path.open(newline="", encoding="utf-8") as handle:
                source_samples = list(csv.DictReader(handle))
            source_samples.sort(key=lambda row: parse_int(row.get("sample_id")) or 0)
            first_start = next(
                (parse_int(row.get("transfer_start_ns")) for row in source_samples
                 if parse_int(row.get("transfer_start_ns")) is not None),
                None,
            )
            run_samples: list[dict[str, Any]] = []
            for source_index, source in enumerate(source_samples):
                if source.get("success") != "1":
                    continue
                transfer_start = parse_int(source.get("transfer_start_ns"))
                persistent = mode == "persistent"
                latency = parse_float(
                    source.get("request_ms") if persistent else source.get("end_to_end_ms"))
                if latency is None:
                    continue
                steady = source_index >= STEADY_STATE_SKIP_REQUESTS
                sample = {
                    "campaign": campaign_dir.name,
                    "run_name": run_name,
                    "deployment": f"tlmsp_{profile}",
                    "mode": mode,
                    "clients": 1,
                    "rate": rate,
                    "iteration": iteration,
                    "request_id": f"tlmsp-{source.get('transfer_id', source.get('sample_id', ''))}",
                    "elapsed_s": (
                        "" if transfer_start is None or first_start is None
                        else f"{(transfer_start - first_start) / 1e9:.9f}"
                    ),
                    "latency_ns": f"{latency * 1e6:.0f}",
                    "latency_ms": f"{latency:.6f}",
                    "handshake_ms": source.get("handshake_ms", ""),
                    "request_ms": source.get("request_ms", ""),
                    "measurement_window": "true",
                    "tls_success": "true",
                    "tls_resumed": "false",
                    "status": "ok",
                    "steady_state": "true" if steady else "false",
                    "in_violin": "true" if steady else "false",
                    "protocol": "tlmsp",
                    "tlmsp_profile": profile,
                    "tlmsp_experiment": experiment,
                }
                for field in SAMPLE_FIELDS:
                    if field not in sample and field in source:
                        sample[field] = source[field]
                run_samples.append(sample)
            samples.extend(run_samples)

            steady_samples = [row for row in run_samples if row["steady_state"] == "true"]
            latencies = [float(row["latency_ms"]) for row in steady_samples]
            handshakes = [
                float(row["handshake_ms"])
                for row in steady_samples if row.get("handshake_ms", "") != ""
            ]
            duration = parse_float(source_summary.get("duration_s")) or 0.0
            achieved = parse_float(source_summary.get("achieved_rps")) or 0.0
            errors = parse_int(source_summary.get("errors")) or 0
            valid = source_summary.get("valid") == "1"
            summaries.append({
                "campaign": campaign_dir.name,
                "run_name": run_name,
                "deployment": f"tlmsp_{profile}",
                "mode": mode,
                "clients": 1,
                "iteration": iteration,
                "offered_rps": f"{rate:.6f}",
                "steady_duration_s": f"{duration:.6f}",
                "achieved_rps": f"{achieved:.6f}",
                "achieved_handshakes_rps": (
                    f"{achieved:.6f}" if experiment == "handshake" else "0.000000"
                ),
                "success": source_summary.get("successes", len(run_samples)),
                "handshake_success": (
                    source_summary.get("successes", len(run_samples))
                    if experiment == "handshake" else len(handshakes)
                ),
                "resumption_fallbacks": 0,
                "failed": errors,
                "non2xx": 0,
                "errors": errors,
                "timeouts": 0,
                "late_slots": source_summary.get("missed", 0),
                "scheduled_rps": (
                    "" if duration <= 0 else
                    f"{(parse_int(source_summary.get('scheduled')) or 0) / duration:.6f}"
                ),
                "scheduled_ratio": "",
                "quality_flags": "",
                "configured_clients": 1,
                "participating_clients": 1,
                "required_clients_p99": "",
                "peak_in_flight": source_summary.get("max_in_flight", ""),
                "inflight_limit": "",
                "unfinished_at_schedule_end": 0,
                "gateway_drops": 0,
                "run_status": "ok",
                "client_returncode": 0,
                "failure_reason": "",
                "mean_latency_ms": f"{mean(latencies):.6f}" if latencies else "",
                "p99_latency_ms": f"{percentile(latencies, 99):.6f}" if latencies else "",
                "mean_handshake_ms": f"{mean(handshakes):.6f}" if handshakes else "",
                "p99_handshake_ms": f"{percentile(handshakes, 99):.6f}" if handshakes else "",
                "mean_latency_ms_filtered": (
                    f"{mean([value for value in latencies if value <= percentile(latencies, 99)]):.6f}"
                    if latencies else ""
                ),
                "mean_cpu_percent": source_summary.get("middlebox_tree_cpu_percent", ""),
                "mean_cpu_percent_p99_filtered": source_summary.get(
                    "middlebox_tree_cpu_percent", ""),
                "p95_cpu_percent": "",
                "peak_cpu_percent": "",
                "median_memory_mib": "",
                "p99_memory_mib": "",
                "peak_memory_mib": "",
                "startup_process_ready_ms": "",
                "startup_first_worker_ready_ms": "",
                "startup_worker_ready_internal_ms": "",
                "point_quality": "valid" if valid else "invalid",
                "point_quality_reasons": "" if valid else "TLMSP campaign marked point invalid",
                "protocol": "tlmsp",
                "tlmsp_profile": profile,
                "tlmsp_experiment": experiment,
            })
    return samples, summaries


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
        filtered_cpu_values: list[float] = []
        if cpu_values:
            cpu_cutoff = percentile(cpu_values, RESOURCE_SAMPLE_PERCENTILE)
            filtered_cpu_values = [value for value in cpu_values if value <= cpu_cutoff]
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
                "mean_cpu_percent_p99_filtered": (
                    f"{mean(filtered_cpu_values):.6f}" if filtered_cpu_values else ""
                ),
                "p95_cpu_percent": f"{percentile(cpu_values, 95):.6f}" if cpu_values else "",
                "peak_cpu_percent": f"{max(cpu_values):.6f}" if cpu_values else "",
                "median_memory_mib": f"{percentile(memory_values, 50):.6f}" if memory_values else "",
                "p99_memory_mib": (
                    f"{percentile(memory_values, RESOURCE_SAMPLE_PERCENTILE):.6f}"
                    if memory_values
                    else ""
                ),
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

    strategy = timeseries_group(deployment)[0]
    if strategy == "sgx":
        go_memory = run_dir / "cpu" / "middlebox_memory.csv"
        return go_retained_memory_points(go_memory) if go_memory.exists() else []

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


def go_retained_memory_points(path: Path) -> list[tuple[int, float]]:
    points: list[tuple[int, float]] = []
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            ts = parse_int(row.get("ts_ns"))
            value = parse_float(row.get("go_retained_bytes"))
            if ts is None or value is None:
                continue
            points.append((ts, value))
    points.sort(key=lambda item: item[0])
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
    elif deployment.startswith("tlmsp_"):
        strategy = ("tlmsp", "TLMSP")
    else:
        strategy = (deployment, deployment)

    handler = (
        "empty" if "_empty_" in deployment or "_no_handler" in deployment
        else "full"
    )
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


def latency_upper_fence(values: list[float]) -> float:
    positive = [value for value in values if value > 0]
    if not positive:
        return 0.0
    q1, q3 = percentile(positive, 25), percentile(positive, 75)
    iqr = q3 - q1
    return q3 + 3 * iqr if iqr > 0 else max(positive)


PAPER_FIGURE_SIZE = (7.0, 5.5)
PAPER_FONT_CONTEXT = {
    "font.size": 13.5,
    "axes.labelsize": 14.5,
    "xtick.labelsize": 13,
    "ytick.labelsize": 13.5,
}
PAPER_PANEL_ADJUST = {"left": 0.12, "right": 0.97, "top": 0.97, "bottom": 0.20}


@plt.rc_context(PAPER_FONT_CONTEXT)
def render_violin(
    groups: list[tuple[str, list[float], list[list[float]]]],
    output_path: Path,
    title: str,
    y_label: str,
    log_density: bool = False,
) -> None:
    violin_color = "#77aadd"
    mean_color = "#023047"
    percentile_levels = (50, 95, 99)

    groups = [(label, values, runs) for label, values, runs in groups if values]
    if not groups:
        print(f"[PLOT] no valid samples for {output_path.name}")
        return

    data: list[list[float]] = []
    group_means: list[float] = []
    group_cis: list[float] = []
    group_percentiles: list[list[float]] = []
    for label, original_values, runs in groups:
        positive_values = [value for value in original_values if value > 0]
        upper_fence = latency_upper_fence(positive_values)
        values = [value for value in positive_values if value <= upper_fence]
        removed = len(positive_values) - len(values)
        print(
            f"[PLOT] {output_path.name}: {label.replace(chr(10), ' ')} "
            f"extreme_fence_ms={upper_fence:.6f} removed={removed}/{len(positive_values)}"
        )

        run_means = []
        for run_values in runs:
            mean_values = [
                value for value in run_values if 0 < value <= upper_fence
            ]
            if mean_values:
                run_means.append(mean(mean_values))

        data.append([math.log10(value) for value in values] if log_density else values)
        group_means.append(mean(run_means))
        group_cis.append(
            confidence_interval_95(run_means)
            if len(run_means) > 1
            else 0.0
        )
        group_percentiles.append([percentile(values, p) for p in percentile_levels])

    fig, ax = plt.subplots(figsize=PAPER_FIGURE_SIZE)
    parts = ax.violinplot(data, widths=0.65, showmeans=False, showmedians=False, showextrema=False)
    for body in parts["bodies"]:
        body.set_facecolor(violin_color)
        body.set_edgecolor("none")
        body.set_alpha(0.65)

    ax.set_xticks(range(1, len(groups) + 1))
    ax.set_xticklabels(
        [label for label, _, _ in groups],
        rotation=22,
        ha="right",
    )
    label_shift = ScaledTranslation(4 / 72, 0, fig.dpi_scale_trans)
    for label in ax.get_xticklabels():
        label.set_transform(label.get_transform() + label_shift)
    ax.set_ylabel(y_label)
    # ax.set_title(title)
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

    for index, values in enumerate(group_percentiles, start=1):
        for value in values:
            position = math.log10(value) if log_density else value
            ax.hlines(
                position,
                index - 0.16,
                index + 0.16,
                color="black",
                linewidth=0.8,
                zorder=5,
            )

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
            index + 0.22,
            mean_position,
            f"{mean_latency:.2f} ms",
            ha="left",
            va="center",
            color=mean_color,
            fontsize=13.5,
            clip_on=False,
        )

    # Fixed geometry for both violins, with room for the rightmost mean label.
    ax.set_xlim(0.4, len(groups) + 1.0)
    fig.subplots_adjust(**PAPER_PANEL_ADJUST)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path)
    plt.close(fig)


def plot_paper_latency_violins(
    samples: list[dict[str, Any]],
    summary_rows: list[dict[str, Any]],
    output_dir: Path,
) -> list[Path]:
    paper_strategy_labels = {
        "direct": "Direct",
        "baremetal": "Shared",
        "sgx": "Shared SGX",
        "docker": "Container",
        "docker_sgx": "Container+SGX",
    }
    summary_by_run = {str(row["run_name"]): row for row in summary_rows}
    handshake_values: dict[tuple[str, str], dict[str, list[float]]] = {}
    request_values: dict[str, dict[str, list[float]]] = {}

    for sample in samples:
        if sample.get("status") != "ok":
            continue
        row = summary_by_run.get(str(sample["run_name"]))
        if row is None or not point_is_usable(row) or not is_canonical_paper_variant(row):
            continue
        if row.get("protocol") == "tlmsp" and row.get("tlmsp_experiment") != "latency":
            continue
        clients = parse_int(sample.get("clients"))
        rate = parse_float(sample.get("rate"))
        mode = str(sample["mode"])
        strategy = timeseries_group(str(sample["deployment"]))[0]
        run_name = str(sample["run_name"])
        if rate is None:
            continue

        handshake = parse_float(sample.get("handshake_ms"))
        if (
            mode == "fresh"
            and clients == 1
            and strategy not in {"docker", "docker_sgx"}
            and math.isclose(rate, 1.0)
            and sample.get("steady_state") == "true"
            and handshake is not None
        ):
            handshake_values.setdefault((strategy, "full"), {}).setdefault(run_name, []).append(handshake)
        elif (
            mode == "resumption"
            and clients == 1
            and math.isclose(rate, 5.0)
            and sample.get("steady_state") == "true"
            and sample.get("tls_resumed") == "true"
            and handshake is not None
        ):
            handshake_values.setdefault((strategy, "resumed"), {}).setdefault(run_name, []).append(handshake)

        latency = parse_float(sample.get("request_ms"))
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
        if (strategy, kind) in handshake_values and strategy != "tlmsp"
    ]
    paper_strategies = {"direct", "baremetal", "sgx", "docker", "docker_sgx"}
    complete_handshake = (
        {(strategy, "full") for strategy in {"direct", "baremetal", "sgx"}}
        | {("docker", "resumed"), ("docker_sgx", "resumed")}
    ).issubset(handshake_values)
    complete_request = paper_strategies.issubset(request_values)

    def violin_group(
        label: str,
        by_run: dict[str, list[float]],
    ) -> tuple[str, list[float], list[list[float]]]:
        return (
            label,
            [value for values in by_run.values() for value in values],
            [values for values in by_run.values() if values],
        )

    handshake_linear_path = output_dir / "P3a-handshake-latency-distribution-linear.pdf"
    handshake_log_path = output_dir / "P3a-handshake-latency-distribution-log.pdf"
    if complete_handshake:
        handshake_groups = []
        for strategy, kind in handshake_order:
            label = paper_strategy_labels.get(strategy, pretty_strategy_label(strategy))
            if kind == "resumed":
                label += "\n(Resumption)"
            handshake_groups.append(
                violin_group(label, handshake_values[(strategy, kind)])
            )
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
            "Handshake latency (ms, log scale)",
            log_density=True,
        )
    else:
        print("[PLOT] incomplete full/resumed handshake matrix; skipping P3a figures")

    request_path = output_dir / "P3b-persistent-request-latency-distribution.pdf"
    if complete_request:
        render_violin(
            [
                violin_group(
                    paper_strategy_labels.get(strategy, pretty_strategy_label(strategy)),
                    request_values[strategy],
                )
                for strategy in STRATEGY_ORDER
                if strategy in request_values and strategy != "tlmsp"
            ],
            request_path,
            "Persistent request latency (1 client, 10 requests/s)",
            "Request latency (ms)",
        )
    else:
        print("[PLOT] incomplete persistent request matrix; skipping P3b figure")
    return [
        path
        for path in (handshake_linear_path, handshake_log_path, request_path)
        if path.exists()
    ]


STRATEGY_ORDER = [
    "direct", "baremetal", "sgx", "sgxgo", "docker", "docker_sgx",
    "tlmsp", "tlmsp_no_handler",
]
STRATEGY_COLORS = {
    "direct": "#457b9d",
    "baremetal": "#2a9d8f",
    "sgx": "#e76f51",
    "sgxgo": "#bc6c25",
    "docker": "#f4a261",
    "docker_sgx": "#6a4c93",
    "tlmsp": "#495057",
    "tlmsp_no_handler": "#868e96",
}

# Paper-quality filtering knob. Set this to False for the final export to hide
# excluded saturated points and warning markers; they never join valid curves.
SHOW_RUN_QUALITY_WARNINGS = True
INCLUDE_CLOSED_LOOP_OPERATING_POINTS = False  # enable after every strategy has a comparable closed-loop run
INCLUDE_TLMSP_IN_P5A = True


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
            add(warnings, f"scheduled/offered={scheduled / offered:.3f}")
        elif scheduled is not None and scheduled < offered:
            add(warnings, "one-boundary-slot scheduled deficit")
        if achieved is not None and offered - achieved > material_deficit:
            add(invalid, f"achieved/offered={achieved / offered:.3f}")
        elif achieved is not None and achieved < offered:
            add(warnings, "one-boundary-slot achieved deficit")

    invalid_flags = {
        "partial_client_participation",
        "request_timeouts",
    }
    warning_flags = {
        "inflight_limit_hit",
        "unfinished_at_schedule_end",
        "offered_load_not_reached",
        "serial_client_concurrency_limited",
    }
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
    if strategy == "tlmsp":
        return row.get("tlmsp_profile") == "full" or (
            row.get("tlmsp_experiment") in {"handshake", "throughput"}
            and row.get("tlmsp_profile") == "no_handler"
        )
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
    tlmsp_open_loop_handshake = (
        strategy == "tlmsp" and row.get("tlmsp_experiment") == "handshake"
    )
    if clients is not None and row_clients != clients and not tlmsp_open_loop_handshake:
        return False
    if mode not in modes:
        return False
    if strategy == "tlmsp":
        expected_experiment = "handshake" if mode == "fresh" else "throughput"
        if row.get("tlmsp_experiment") != expected_experiment:
            return False
    if strategy == "direct" and not include_direct:
        return False
    if not is_canonical_paper_variant(row):
        return False
    return True


def throughput_strategy(row: dict[str, Any]) -> str:
    strategy = timeseries_group(str(row["deployment"]))[0]
    if (
        strategy == "tlmsp"
        and row.get("tlmsp_experiment") == "throughput"
        and row.get("tlmsp_profile") == "no_handler"
    ):
        return "tlmsp_no_handler"
    return strategy


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
        "tlmsp": "TLMSP",
        "tlmsp_no_handler": "TLMSP No-handler",
    }
    return labels.get(strategy, strategy)


def aggregate_throughput_metric_by_mode(
    summary_rows: list[dict[str, Any]],
    metric: str,
    include_direct: bool,
    modes: tuple[str, ...] = ("fresh", "persistent", "resumption"),
    clients: int | None = 1,
    include_invalid: bool = False,
) -> dict[str, dict[str, list[tuple[float, float, float]]]]:
    grouped_values: dict[str, dict[str, dict[float, list[float]]]] = {mode: {} for mode in modes}
    for row in summary_rows:
        mode = str(row["mode"])
        if not include_throughput_row(row, include_direct, modes, clients):
            continue
        if not include_invalid and not point_is_usable(row):
            continue
        offered = parse_float(row.get("offered_rps"))
        value = parse_float(row.get(metric))
        if offered is None or offered <= 0 or value is None:
            continue
        strategy = throughput_strategy(row)
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
HANDSHAKE_CAPACITY_WARMUP_S = 5.0
HANDSHAKE_CAPACITY_DURATION_S = 60.0
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
        title="Full and Resumed Handshake Throughput",
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
            strategy = throughput_strategy(row)
            available.setdefault(strategy, set()).add(offered)
    required = {"direct", "baremetal", "sgx", "docker", "docker_sgx"}
    if not required.issubset(available) or any(len(available[strategy]) < 2 for strategy in required):
        print(f"[PLOT] incomplete five-strategy operating matrix; skipping {output_path.name}")
        return

    paper_labels = {
        "direct": "Direct",
        "baremetal": "Shared",
        "sgx": "Shared SGX",
        "docker": "Container",
        "docker_sgx": "Container+SGX",
        "tlmsp": "TLMSP Full",
        "tlmsp_no_handler": "TLMSP No-handler",
    }
    metrics = [
        ("mean_latency_ms_filtered", "Mean end-to-end latency (ms)", True),
        ("mean_cpu_percent", "Total CPU usage (%)", False),
    ]

    def draw_panel(
        ax: Any,
        metric: str,
        ylabel: str,
        include_direct: bool,
        final_export: bool = False,
    ) -> dict[str, Any]:
        handles_by_label: dict[str, Any] = {}
        grouped = aggregate_throughput_metric_by_mode(
            summary_rows,
            metric,
            include_direct=include_direct,
            modes=("persistent",),
            clients=1,
            include_invalid=not final_export,
        ).get("persistent", {})
        excluded_rates: dict[str, set[float]] = {}
        if final_export:
            for row in summary_rows:
                if (
                    include_throughput_row(
                        row, include_direct, ("persistent",), clients=1
                    )
                    and not point_is_usable(row)
                    and parse_float(row.get(metric)) is not None
                ):
                    strategy = throughput_strategy(row)
                    offered = parse_float(row.get("offered_rps"))
                    if offered is not None and offered > 0:
                        excluded_rates.setdefault(strategy, set()).add(offered)

        plotted_rates: list[float] = []
        for strategy in STRATEGY_ORDER:
            if final_export and strategy in {"tlmsp", "tlmsp_no_handler"}:
                continue
            if strategy in {"tlmsp", "tlmsp_no_handler"} and (
                    metric != "mean_latency_ms_filtered"
                    or not INCLUDE_TLMSP_IN_P5A):
                continue
            points = [
                point for point in grouped.get(strategy, [])
                if point[0] not in excluded_rates.get(strategy, set())
            ]
            if not points:
                continue
            plotted_rates.extend(point[0] for point in points)
            label = paper_labels.get(strategy, pretty_strategy_label(strategy))
            line = ax.errorbar(
                [point[0] for point in points],
                [point[1] for point in points],
                yerr=[point[2] for point in points],
                marker="o",
                linewidth=1.4,
                capsize=3,
                color=STRATEGY_COLORS.get(strategy),
                label=label,
            )
            handles_by_label.setdefault(label, line.lines[0])

        if not final_export:
            for row in summary_rows:
                if not include_throughput_row(
                    row,
                    include_direct=include_direct,
                    modes=("persistent",),
                    clients=1,
                ):
                    continue
                if throughput_strategy(row) in {"tlmsp", "tlmsp_no_handler"} and (
                        metric != "mean_latency_ms_filtered"
                        or not INCLUDE_TLMSP_IN_P5A):
                    continue
                draw_quality_marker(ax, row, metric)

        if INCLUDE_CLOSED_LOOP_OPERATING_POINTS:
            for row in summary_rows:
                if not include_throughput_row(
                    row,
                    include_direct=include_direct,
                    modes=("persistent",),
                    clients=1,
                ) or parse_float(row.get("offered_rps")) != 0 or not point_is_usable(row):
                    continue
                x_value = parse_float(row.get("achieved_rps"))
                y_value = parse_float(row.get(metric))
                strategy = throughput_strategy(row)
                if x_value is not None and x_value > 0 and y_value is not None:
                    ax.scatter(
                        [x_value],
                        [y_value],
                        marker="*",
                        s=70,
                        color=STRATEGY_COLORS.get(strategy),
                        zorder=5,
                    )

        set_plain_log_xaxis(ax, plotted_rates)
        ax.set_xlabel("Offered throughput (requests/s)")
        ax.set_ylabel(ylabel)
        ax.set_ylim(bottom=0)
        ax.grid(True, alpha=0.25)
        return handles_by_label

    fig, axes = plt.subplots(1, 2, figsize=(11.0, 4.5), squeeze=False)
    combined_handles: dict[str, Any] = {}
    for ax, (metric, ylabel, include_direct) in zip(axes.flatten(), metrics):
        combined_handles.update(draw_panel(ax, metric, ylabel, include_direct))

    fig.suptitle("Single-client persistent operating curve", fontsize=13)
    if combined_handles:
        fig.legend(
            list(combined_handles.values()),
            list(combined_handles.keys()),
            loc="lower center",
            ncol=min(5, len(combined_handles)),
            fontsize=8,
        )
    fig.tight_layout(rect=[0, 0.10, 1, 0.95])
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, bbox_inches="tight")
    plt.close(fig)

    split_outputs = [
        output_path.parent / "P5a-single-client-throughput-latency.pdf",
        output_path.parent / "P5b-single-client-throughput-cpu.pdf",
    ]
    with plt.rc_context(PAPER_FONT_CONTEXT):
        for (metric, ylabel, include_direct), split_path in zip(metrics, split_outputs):
            split_fig, split_ax = plt.subplots(figsize=PAPER_FIGURE_SIZE)
            handles = draw_panel(
                split_ax, metric, ylabel, include_direct, final_export=True
            )
            if handles:
                split_fig.legend(
                    list(handles.values()),
                    list(handles.keys()),
                    loc="lower center",
                    bbox_to_anchor=(0.5, 0.01),
                    ncol=len(handles),
                    handlelength=1.0,
                    handletextpad=0.3,
                    columnspacing=0.6,
                )
            split_fig.subplots_adjust(**PAPER_PANEL_ADJUST)
            split_fig.savefig(split_path)
            plt.close(split_fig)


def plot_handshake_latency_capacity(summary_rows: list[dict[str, Any]], output_path: Path) -> None:
    if not has_complete_handshake_capacity_data(summary_rows):
        print(f"[PLOT] incomplete handshake-capacity matrix; skipping {output_path.name}")
        return
    plot_metric_vs_offered(
        summary_rows,
        output_path,
        metric="mean_handshake_ms",
        ylabel="Mean TLS handshake latency (ms)",
        title="Handshake Mean Latency Under Load",
        include_direct=True,
        modes=("fresh", "resumption"),
        clients=HANDSHAKE_CAPACITY_CLIENTS,
        logarithmic_x=True,
    )


def plot_aggregate_scalability(summary_rows: list[dict[str, Any]], output_path: Path) -> list[Path]:
    strategies = ("baremetal", "sgx", "docker", "docker_sgx")
    strategy_labels = {
        "baremetal": "Shared",
        "sgx": "Shared SGX",
        "docker": "Container",
        "docker_sgx": "Container+SGX",
    }
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
            if clients is None or clients > 40 or offered is None:
                continue
            by_clients_rate.setdefault(clients, {}).setdefault(offered, []).append(row)

        for clients, by_rate in by_clients_rate.items():
            closed_rows = by_rate.get(0.0, [])
            achieved = [
                value
                for row in closed_rows
                if (point_is_usable(row) or (strategy == "docker_sgx" and clients == 40))
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
        return []

    client_ticks = sorted(
        {
            clients
            for values in (saturated, fixed_latency)
            for by_clients in values.values()
            for clients in by_clients
        }
    )

    def draw_panel(
        ax: Any, values: dict[str, dict[int, tuple[float, float]]], metric: str,
        paper: bool = False,
    ) -> None:
        for strategy in strategies:
            points = sorted(values.get(strategy, {}).items())
            if not points:
                continue
            ax.errorbar(
                [clients for clients, _ in points],
                [value[0] for _, value in points],
                yerr=[value[1] for _, value in points],
                marker="o",
                linewidth=1.4,
                capsize=3,
                color=STRATEGY_COLORS.get(strategy),
                label=strategy_labels[strategy],
            )
        ax.set_xlabel("Persistent clients")
        ax.set_xticks(client_ticks)
        ax.set_xticklabels([str(clients) for clients in client_ticks])
        ax.set_xlim(0, max(client_ticks) + 2)
        if metric == "capacity":
            ax.set_ylabel("Achieved throughput (requests/s)")
            ax.set_yscale("log")
            ax.yaxis.set_major_locator(LogLocator(base=10, subs=(1.0, 2.0, 5.0)))
            ax.yaxis.set_major_formatter(StrMethodFormatter("{x:,.0f}"))
            ax.yaxis.set_minor_locator(NullLocator())
        else:
            ax.set_ylabel("Mean end-to-end latency (ms)")
            ax.set_ylim(bottom=0)
        ax.grid(True, alpha=0.25)
        if not paper:
            ax.legend(fontsize=8)

    fig, axes = plt.subplots(1, 2, figsize=(11.0, 4.5), squeeze=False)
    capacity_ax, latency_ax = axes.flatten()
    draw_panel(capacity_ax, saturated, "capacity")
    draw_panel(latency_ax, fixed_latency, "latency")
    capacity_ax.set_title("Closed-loop achieved throughput")
    latency_ax.set_title(f"Fixed aggregate load: {fixed_rate:g} requests/s")
    fig.suptitle("Middlebox client scalability", fontsize=13)
    fig.tight_layout(rect=[0, 0, 1, 0.95])
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, bbox_inches="tight")
    plt.close(fig)

    panel_paths = [
        output_path.with_name("P7a-client-scalability-throughput.pdf"),
        output_path.with_name("P7b-client-scalability-latency.pdf"),
    ]
    for values, metric, path in (
        (saturated, "capacity", panel_paths[0]),
        (fixed_latency, "latency", panel_paths[1]),
    ):
        with plt.rc_context(PAPER_FONT_CONTEXT):
            panel_fig, panel_ax = plt.subplots(figsize=PAPER_FIGURE_SIZE)
            draw_panel(panel_ax, values, metric, paper=True)
            handles, labels = panel_ax.get_legend_handles_labels()
            panel_fig.legend(
                handles, labels, loc="lower center", bbox_to_anchor=(0.5, 0.01),
                ncol=len(handles), handlelength=1.0, handletextpad=0.3,
                columnspacing=0.6,
            )
            panel_fig.subplots_adjust(**PAPER_PANEL_ADJUST)
            panel_fig.savefig(path)
            plt.close(panel_fig)

    return [output_path, *panel_paths]


def epc_platform(run_name: str) -> str | None:
    name = run_name.lower()
    if "docker" not in name:
        return None
    if "sgx2" in name:
        return "SGX2"
    if "sgx" in name:
        return "SGX1"
    return None


def epc_paging_operations(run_summary: dict[str, Any]) -> float | None:
    run_dir = Path(str(run_summary["run_dir"]))
    metadata_path = run_dir / "metadata.json"
    epc_path = run_dir / "epc" / "sgx_epc.csv"
    if not metadata_path.is_file() or not epc_path.is_file():
        return None

    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    monitor = metadata.get("sgx_epc_monitor", {})
    offset_ns = parse_int(monitor.get("monotonic_to_realtime_offset_ns"))
    client_start_ns = parse_int(
        metadata.get("processes", {}).get("client", {}).get("started_ns")
    )
    duration_s = parse_float(metadata.get("parameters", {}).get("duration_s"))
    if offset_ns is None or client_start_ns is None or not duration_s:
        return None

    samples: list[tuple[int, int, int]] = []
    with epc_path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            timestamp_ns = parse_int(row.get("timestamp_monotonic_ns"))
            ewb = parse_int(row.get("ewb_success_total"))
            eldu = parse_int(row.get("eldu_success_total"))
            if timestamp_ns is not None and ewb is not None and eldu is not None:
                samples.append((timestamp_ns + offset_ns, ewb, eldu))

    client_end_ns = client_start_ns + round(duration_s * 1e9)
    start = next((sample for sample in reversed(samples) if sample[0] <= client_start_ns), None)
    end = next((sample for sample in reversed(samples) if sample[0] <= client_end_ns), None)
    if start is None or end is None or end[0] <= start[0]:
        return None

    ewb_delta = end[1] - start[1]
    eldu_delta = end[2] - start[2]
    if ewb_delta < 0 or eldu_delta < 0:
        return None
    return float(ewb_delta + eldu_delta)


def plot_epc_scalability(
    run_summaries: list[dict[str, Any]], output_dir: Path,
) -> list[Path]:
    clients = (1, 5, 10, 20, 30, 40)
    platforms = ("SGX1", "SGX2")
    workloads = {
        "closed": (0.0, "P7c-client-scalability-epc-closed.pdf"),
        "fixed": (10.0, "P7d-client-scalability-epc-fixed.pdf"),
    }
    values: dict[tuple[str, str, int], list[float]] = {}

    for summary in run_summaries:
        platform = epc_platform(Path(str(summary.get("run_dir", ""))).name)
        client_count = parse_int(summary.get("clients"))
        rate = parse_float(summary.get("rate"))
        if (
            platform is None
            or client_count not in clients
            or rate is None
            or bool(summary.get("run_failed"))
        ):
            continue
        workload = next(
            (name for name, (target, _) in workloads.items() if math.isclose(rate, target)),
            None,
        )
        if workload is None:
            continue
        paging = epc_paging_operations(summary)
        if paging is not None:
            values.setdefault((workload, platform, client_count), []).append(paging)

    if not values:
        return []

    output_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    bar_width = 0.9075
    offsets = {"SGX1": -0.45, "SGX2": 0.45}
    styles = {
        "SGX1": {"color": STRATEGY_COLORS["docker_sgx"], "hatch": None},
        "SGX2": {"color": "white", "hatch": "///"},
    }
    maximum = max(
        mean(point) + confidence_interval_95(point)
        for point in values.values()
        if point
    )

    for workload, (_, filename) in workloads.items():
        path = output_dir / filename
        with plt.rc_context(PAPER_FONT_CONTEXT):
            fig, ax = plt.subplots(figsize=PAPER_FIGURE_SIZE)
            for platform in platforms:
                platform_values = [values.get((workload, platform, client), []) for client in clients]
                heights = [mean(point) if point else 0.0 for point in platform_values]
                errors = [confidence_interval_95(point) if point else 0.0 for point in platform_values]
                xs = [client + offsets[platform] for client in clients]
                style = styles[platform]
                ax.bar(
                    xs,
                    heights,
                    width=bar_width,
                    yerr=errors,
                    color=style["color"],
                    edgecolor=STRATEGY_COLORS["docker_sgx"],
                    hatch=style["hatch"],
                    linewidth=0.9,
                    capsize=3,
                    label=platform,
                )
                for x, point, height in zip(xs, platform_values, heights):
                    if not point or height == 0:
                        ax.text(
                            x,
                            0.015,
                            "N/A" if not point else "0",
                            transform=ax.get_xaxis_transform(),
                            ha="center",
                            va="bottom",
                            fontsize=8,
                            color="dimgray",
                            rotation=90,
                        )

            ax.set_xlabel("Persistent connections")
            ax.set_xticks(clients)
            ax.set_xticklabels([str(client) for client in clients])
            ax.set_xlim(0, max(clients) + 2)
            ax.set_ylabel("EPC page evictions and reloads")
            ax.set_ylim(0, max(4.5e6, maximum * 1.05))
            ax.yaxis.set_major_formatter(
                FuncFormatter(lambda value, _: "0" if value == 0 else f"{value / 1e6:g}M")
            )
            ax.grid(True, axis="y", alpha=0.25)
            ax.legend(
                loc="upper right",
                ncol=1,
                handlelength=1.0,
                handletextpad=0.3,
            )
            fig.subplots_adjust(**PAPER_PANEL_ADJUST)
            ax.set_position(
                [
                    PAPER_PANEL_ADJUST["left"],
                    PAPER_PANEL_ADJUST["bottom"],
                    PAPER_PANEL_ADJUST["right"] - PAPER_PANEL_ADJUST["left"],
                    PAPER_PANEL_ADJUST["top"] - PAPER_PANEL_ADJUST["bottom"],
                ]
            )
            fig.savefig(path)
            plt.close(fig)
        paths.append(path)

    return paths


def aggregate_scalability_metric(
    summary_rows: list[dict[str, Any]],
    strategy: str,
    mode: str,
    metric: str,
) -> dict[int, list[tuple[float, float, float]]]:
    grouped: dict[int, dict[float, list[float]]] = {}
    for row in summary_rows:
        row_strategy = throughput_strategy(row)
        if row_strategy != strategy:
            continue
        if not include_throughput_row(row, include_direct=True, modes=(mode,), clients=None) or not point_is_usable(row):
            continue
        clients = parse_int(row.get("clients"))
        offered = parse_float(row.get("offered_rps"))
        value = parse_float(row.get(metric))
        if clients is None or clients > 40 or offered is None or offered <= 0 or value is None:
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
                        row_strategy = throughput_strategy(row)
                        if row_strategy != strategy or not include_throughput_row(
                            row, include_direct=True, modes=(mode,), clients=None
                        ):
                            continue
                        diagnostic = row_diagnostics(row)
                        if not diagnostic:
                            continue
                        x_value = parse_float(row.get("offered_rps"))
                        y_value = parse_float(row.get(metric))
                        clients = parse_int(row.get("clients"))
                        if x_value is None or y_value is None or clients is None or clients > 40:
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
        strategy = throughput_strategy(row)
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
        r"\begin{tabular}{llrrrr}",
        r"\toprule",
        r"Deployment & Handshake & Clients & Maximum achieved & Mean latency & Runs \\",
        r" & & & (handshakes/s) & (ms) & \\",
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
        rows = by_rate[lower]
        clients = "Open-loop" if strategy == "tlmsp" else str(HANDSHAKE_CAPACITY_CLIENTS)
        achieved = [
            value for row in rows if (value := parse_float(row.get("achieved_handshakes_rps"))) is not None
        ]
        latency = [value for row in rows if (value := parse_float(row.get("mean_handshake_ms"))) is not None]
        lines.append(
            f"{latex_escape(pretty_strategy_label(strategy))} & "
            f"{'Resumed' if mode == 'resumption' else 'Full'} & {clients} & "
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
                    "clients": (
                        "open-loop"
                        if timeseries_group(str(row["deployment"]))[0] == "tlmsp"
                        else row["clients"]
                    ),
                    "iteration": row["iteration"],
                    "offered_handshakes_rps": row["offered_rps"],
                    "achieved_handshakes_rps": row["achieved_handshakes_rps"],
                    "mean_handshake_ms": row["mean_handshake_ms"],
                    "point_quality": row["point_quality"],
                    "point_quality_reasons": row["point_quality_reasons"],
                }
            )
    return True


def handshake_capacity_windowed_rows(
    campaign_dir: Path, summary_rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    rows = [dict(row) for row in handshake_capacity_rows(summary_rows)]
    for row in rows:
        if str(row.get("protocol")) == "tlmsp":
            continue
        report = load_client_report(campaign_dir / str(row["run_name"]) / "stdout" / "client.log")
        if not report:
            raise ValueError(f"missing client report for {row['run_name']}")
        for field in (
            "achieved_rps", "scheduled_rps", "errors", "timeouts", "non2xx", "success", "quality_flags"
        ):
            row[field] = report.get(field, "")
        row["late_slots"] = report.get("late", "")
        row["failed"] = (parse_int(row["errors"]) or 0) + (parse_int(row["non2xx"]) or 0)
        quality, reasons = point_quality(row)
        row["point_quality"] = quality
        row["point_quality_reasons"] = "; ".join(reasons)

    grouped: dict[tuple[str, str], dict[float, list[dict[str, Any]]]] = {}
    for row in rows:
        strategy = timeseries_group(str(row["deployment"]))[0]
        rate = parse_float(row.get("offered_rps"))
        assert rate is not None
        grouped.setdefault((strategy, str(row["mode"])), {}).setdefault(rate, []).append(row)

    selected: list[dict[str, Any]] = []
    for by_rate in grouped.values():
        valid_rates = [
            rate for rate, runs in by_rate.items() if all(point_is_usable(run) for run in runs)
        ]
        if valid_rates:
            selected.extend(by_rate[max(valid_rates)])

    tlmsp_paths: dict[str, Path] = {}
    for summary_path in (campaign_dir / "tlmsp").rglob("summary.csv"):
        with summary_path.open(newline="", encoding="utf-8") as handle:
            for source in csv.DictReader(handle):
                rate = parse_float(source.get("rate")) or 0.0
                iteration = parse_int(source.get("run")) or 1
                name = (
                    f"tlmsp-{source.get('experiment', summary_path.parent.name)}-"
                    f"{source.get('profile', 'full')}-{source.get('mode', 'unknown')}-"
                    f"rate{rate:g}-run{iteration:02d}"
                )
                tlmsp_paths[name] = summary_path.parent / source["samples_csv"]

    duration = HANDSHAKE_CAPACITY_DURATION_S - HANDSHAKE_CAPACITY_WARMUP_S
    campaign_status = load_campaign_status(campaign_dir / "summary.csv")
    campaign_status.update(load_campaign_status(campaign_dir / "summary-imported.csv"))
    for row in selected:
        run_name = str(row["run_name"])
        if str(row.get("protocol")) == "tlmsp":
            sample_path = tlmsp_paths[run_name]
            with sample_path.open(newline="", encoding="utf-8") as handle:
                first_start = next(
                    (
                        start for sample in csv.DictReader(handle)
                        if (start := parse_int(sample.get("transfer_start_ns"))) is not None
                    ),
                    None,
                )
            if first_start is None:
                raise ValueError(f"missing TLMSP start timestamp for {run_name}")
            count = 0
            latencies: list[float] = []
            with sample_path.open(newline="", encoding="utf-8") as handle:
                for sample in csv.DictReader(handle):
                    start = parse_int(sample.get("transfer_start_ns"))
                    latency = parse_float(sample.get("handshake_ms"))
                    if (
                        start is not None
                        and HANDSHAKE_CAPACITY_WARMUP_S
                        <= (start - first_start) / 1e9
                        < HANDSHAKE_CAPACITY_DURATION_S
                        and sample.get("success") == "1"
                        and latency is not None
                    ):
                        count += 1
                        latencies.append(latency)
        else:
            run_dir = campaign_dir / run_name
            samples, _ = load_run(
                campaign_dir,
                run_dir,
                run_dir / "metadata.json",
                run_dir / "csv" / "client.csv",
                campaign_status.get(run_name, {}),
            )
            window = [
                sample
                for sample in samples
                if (elapsed := parse_float(sample.get("elapsed_s"))) is not None
                and HANDSHAKE_CAPACITY_WARMUP_S <= elapsed < HANDSHAKE_CAPACITY_DURATION_S
                and sample.get("tls_success") == "true"
                and (row["mode"] != "resumption" or sample.get("tls_resumed") == "true")
            ]
            count = len(window)
            latencies = [
                latency
                for sample in window
                if sample.get("status") == "ok"
                and (latency := parse_float(sample.get("handshake_ms"))) is not None
            ]
        if not count or not latencies:
            raise ValueError(f"no successful handshakes in 5-60 s window for {run_name}")
        row["achieved_handshakes_rps"] = count / duration
        row["mean_handshake_ms"] = sum(latencies) / len(latencies)
    return selected


def write_selected_load_resources_latex(summary_rows: list[dict[str, Any]], output_path: Path) -> bool:
    selected_rates = (10.0, 100.0)  # use 500 later only after every strategy sustains it cleanly
    grouped: dict[tuple[str, float], list[dict[str, Any]]] = {}
    for row in summary_rows:
        if not include_throughput_row(row, True, ("persistent",), clients=1) or not point_is_usable(row):
            continue
        if throughput_strategy(row) == "tlmsp_no_handler":
            continue
        offered = parse_float(row.get("offered_rps"))
        if offered is None or not any(math.isclose(offered, rate) for rate in selected_rates):
            continue
        strategy = throughput_strategy(row)
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
    selected_clients = (1, 10, 30, 40)
    strategies = ("baremetal", "sgx", "docker", "docker_sgx")
    labels = {
        "baremetal": "Shared",
        "sgx": "Shared SGX",
        "docker": "Container",
        "docker_sgx": "Container+SGX",
    }
    grouped: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for row in summary_rows:
        if not include_throughput_row(row, False, ("persistent",), clients=None) or not point_is_usable(row):
            continue
        offered = parse_float(row.get("offered_rps"))
        clients = parse_int(row.get("clients"))
        if offered is None or not math.isclose(offered, fixed_rate) or clients is None:
            continue
        strategy, _, handler, _ = timeseries_group(str(row["deployment"]))
        if strategy not in strategies or handler != "full" or clients not in selected_clients:
            continue
        grouped.setdefault((strategy, clients), []).append(row)
    required = {
        (strategy, clients)
        for strategy in strategies
        for clients in selected_clients
    }
    if not required.issubset(grouped):
        print(f"[PLOT] incomplete clients/memory matrix; skipping {output_path.name}")
        return False

    lines = [
        (
            f"% CPU means exclude samples above each run's p{RESOURCE_SAMPLE_PERCENTILE}; "
            f"memory reports each run's p{RESOURCE_SAMPLE_PERCENTILE}."
        ),
        r"\begin{tabular}{llrrrr}",
        r"\toprule",
        r" & & \multicolumn{4}{c}{Persistent connections} \\",
        r"\cmidrule(lr){3-6}",
        r"Deployment & Metric & 1 & 10 & 30 & 40 \\",
        r"\midrule",
    ]
    for index, strategy in enumerate(strategies):
        if index:
            lines.append(r"\midrule")
        cpu_cells: list[str] = []
        memory_cells: list[str] = []
        for clients in selected_clients:
            rows = grouped[(strategy, clients)]
            cpu_values = [
                value
                for row in rows
                if (value := parse_float(row.get("mean_cpu_percent_p99_filtered"))) is not None
            ]
            memory_values = [
                value
                for row in rows
                if (value := parse_float(row.get("p99_memory_mib"))) is not None
            ]
            cpu_cells.append(latex_mean_ci(cpu_values))
            memory_cells.append(latex_mean_ci(memory_values))

        label = latex_escape(labels[strategy])
        lines.append(
            f"\\multirow{{2}}{{*}}{{{label}}} & Mean CPU (\\%) & "
            + " & ".join(cpu_cells)
            + r" \\"
        )
        lines.append(
            f" & p{RESOURCE_SAMPLE_PERCENTILE} memory (MiB) & "
            + " & ".join(memory_cells)
            + r" \\"
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
    operation_specs = [
        {
            "key": "request_validation_shared",
            "component": "Request validation",
            "variant": "Shared",
            "deployment": "baremetal_full_reuse",
            "mode": "persistent",
            "rate": 10.0,
            "role": "middlebox",
            "start": "middlebox_validation_start",
            "done": "middlebox_validation_done",
            "success_zero": True,
        },
        {
            "key": "request_validation_sgx",
            "component": "Request validation",
            "variant": "Shared SGX",
            "deployment": "sgx_full_reuse",
            "mode": "persistent",
            "rate": 10.0,
            "role": "middlebox",
            "start": "middlebox_validation_start",
            "done": "middlebox_validation_done",
            "success_zero": True,
        },
        {
            "key": "response_validation_shared",
            "component": "Response validation",
            "variant": "Shared",
            "deployment": "baremetal_full_reuse",
            "mode": "persistent",
            "rate": 10.0,
            "role": "middlebox",
            "start": "middlebox_response_validation_start",
            "done": "middlebox_response_validation_done",
            "success_zero": True,
        },
        {
            "key": "response_validation_sgx",
            "component": "Response validation",
            "variant": "Shared SGX",
            "deployment": "sgx_full_reuse",
            "mode": "persistent",
            "rate": 10.0,
            "role": "middlebox",
            "start": "middlebox_response_validation_start",
            "done": "middlebox_response_validation_done",
            "success_zero": True,
        },
        {
            "key": "quote_generation",
            "component": "Quote generation",
            "variant": "Shared SGX",
            "deployment": "sgx_full_noreuse",
            "mode": "fresh",
            "rate": 1.0,
            "role": "middlebox",
            "start": "middlebox_attestation_start_by_id",
            "done": "middlebox_attestation_done_by_id",
            "success_zero": False,
        },
        {
            "key": "quote_verification",
            "component": "Quote verification",
            "variant": "Shared SGX",
            "deployment": "sgx_full_noreuse",
            "mode": "fresh",
            "rate": 1.0,
            "role": "certserver",
            "start": "certserver_quote_verify_by_id",
            "done": "certserver_quote_done_by_id",
            # QuoteDone carries the DCAP result, not a generic reason code.
            "success_zero": False,
        },
        {
            "key": "dc_generation",
            "component": "DC generation (excl. attestation)",
            "variant": "Shared SGX",
            "deployment": "sgx_full_noreuse",
            "mode": "fresh",
            "rate": 1.0,
            "role": "certserver",
            "start": "certserver_generate_by_id",
            "done": "certserver_generate_done_by_id",
            "success_zero": True,
        },
    ]
    startup_specs = [
        ("container_worker", "Worker creation", "Container", "startup_container", "worker_ready_internal_ms"),
        (
            "container_sgxgo_worker",
            "Worker creation",
            "Container+SGX (SGX-Go)",
            "startup_container_sgxgo",
            "worker_ready_internal_ms",
        ),
        (
            "standard_go_sgx_startup",
            "SGX process startup",
            "Standard Go",
            "startup_gramine_sgx",
            "process_ready_ms",
        ),
        ("sgxgo_startup", "SGX process startup", "SGX-Go", "startup_sgxgo", "process_ready_ms"),
    ]
    row_order = [
        (str(spec["key"]), str(spec["component"]), str(spec["variant"]))
        for spec in operation_specs
    ] + [(key, component, variant) for key, component, variant, _, _ in startup_specs]
    run_values: dict[str, list[list[float]]] = {key: [] for key, _, _ in row_order}

    for summary in run_summaries:
        if summary.get("run_failed"):
            continue
        deployment = str(summary["deployment"])
        mode = str(summary["mode"])
        clients = parse_int(summary.get("clients"))
        rate = parse_float(summary.get("rate"))
        run_dir = Path(str(summary["run_dir"]))

        matching_operations = [
            spec
            for spec in operation_specs
            if deployment == spec["deployment"]
            and mode == spec["mode"]
            and clients == 1
            and rate is not None
            and math.isclose(rate, float(spec["rate"]))
        ]
        indexes: dict[str, dict[str, dict[str, list[tuple[int, int]]]]] = {}
        for spec in matching_operations:
            role = str(spec["role"])
            if role not in indexes:
                indexes[role] = load_trace_index(trace_paths(run_dir, role))
            values = event_durations_from_index(
                indexes[role],
                str(spec["start"]),
                str(spec["done"]),
                bool(spec["success_zero"]),
            )
            if values:
                run_values[str(spec["key"])].append(values)

        for key, _, _, startup_deployment, metric in startup_specs:
            if deployment != startup_deployment or mode != "startup":
                continue
            value = parse_float(startup_metrics_for_run(summary).get(metric))
            if value is not None:
                run_values[key].append([value])

    lines = [
        r"\begin{tabular}{@{}llr@{}}",
        r"\toprule",
        r"Component & Variant & \shortstack{Mean $\pm$ 95\% CI\\(ms)} \\",
        r"\midrule",
    ]
    for key, component, variant in row_order:
        run_means = [mean(values) for values in run_values[key] if values]
        value = latex_mean_ci(run_means, digits=3) if run_means else "--"
        lines.append(f"{latex_escape(component)} & {latex_escape(variant)} & {value} " + r"\\")
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


def tlmsp_latency_components(sample: dict[str, Any]) -> tuple[list[float], list[float]] | None:
    handshake_total = parse_float(sample.get("handshake_ms")) or 0.0
    handshake = [handshake_total, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
    if sample.get("dissection_ok") != "1":
        if sample.get("mode") == "fresh":
            return handshake, [0.0] * 10
        return None

    values = [
        parse_float(sample.get("path_before_request_validation_ms")),
        parse_float(sample.get("request_handler_ms")),
        parse_float(sample.get("path_to_application_ms")),
        parse_float(sample.get("application_ms")),
        parse_float(sample.get("path_to_response_validation_ms")),
        parse_float(sample.get("response_handler_ms")),
        parse_float(sample.get("path_after_response_validation_ms")),
    ]
    if any(value is None for value in values):
        return None
    grey_before, request_validation, grey_to_server, application, \
        grey_from_server, response_validation, grey_to_client = values
    request = [
        0.0,
        grey_before,
        request_validation,
        grey_to_server,
        application,
        0.0,
        grey_from_server,
        response_validation,
        grey_to_client,
        0.0,
    ]
    return handshake, request


def plot_latency_dissection(
    samples: list[dict[str, Any]],
    run_summaries: list[dict[str, Any]],
    output_dir: Path,
    summary_rows: list[dict[str, Any]],
) -> list[Path]:
    rate_by_mode = {"fresh": 1.0, "persistent": 10.0, "resumption": 5.0}
    summaries = {str(summary["run_name"]): summary for summary in run_summaries}
    quality_by_run = {str(row["run_name"]): row for row in summary_rows}
    by_run: dict[str, list[dict[str, Any]]] = {}
    for sample in samples:
        if sample.get("status") != "ok" or sample.get("steady_state") != "true":
            continue
        mode = str(sample.get("mode"))
        if mode in {"fresh", "resumption"}:
            row = quality_by_run.get(str(sample["run_name"]))
            if row is None or not point_is_usable(row):
                continue
            if mode == "resumption" and sample.get("tls_resumed") != "true":
                continue
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
    mode_cis: dict[str, tuple[dict[str, float], dict[str, float]]] = {}

    for mode in ("fresh", "persistent", "resumption"):
        vectors_by_run: dict[str, list[list[tuple[list[float], list[float]]]]] = {}
        for run_name, run_samples in by_run.items():
            if run_samples[0].get("protocol") == "tlmsp":
                if (
                    str(run_samples[0].get("mode")) != mode
                    or run_samples[0].get("tlmsp_profile") != "full"
                    or run_samples[0].get("tlmsp_experiment") != "latency"
                ):
                    continue
                vectors = [
                    vector for sample in run_samples
                    if (vector := tlmsp_latency_components(sample)) is not None
                ]
                if vectors:
                    vectors_by_run.setdefault("tlmsp", []).append(vectors)
                continue
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
                vectors_by_run.setdefault(strategy, []).append(vectors)

        run_vectors: dict[str, list[tuple[list[float], list[float]]]] = {}
        for strategy, runs in vectors_by_run.items():
            # Filter whole samples by their handshake/request total, within the
            # selected variant and mode, retaining equal weighting of run means.
            metric_index = 1 if mode == "persistent" else 0
            upper_fence = latency_upper_fence([
                sum(vector[metric_index]) for vectors in runs for vector in vectors
            ])
            for vectors in runs:
                vectors = [vector for vector in vectors
                           if 0 < sum(vector[metric_index]) <= upper_fence]
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
        # Sum within each run before computing uncertainty across independent runs.
        mode_cis[mode] = (
            {strategy: confidence_interval_95([sum(run[0]) for run in run_vectors[strategy]])
             for strategy in strategies},
            {strategy: confidence_interval_95([sum(run[1]) for run in run_vectors[strategy]])
             for strategy in strategies},
        )

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

    dissection_labels = {
        "baremetal": "Shared",
        "sgx": "Shared SGX",
        "docker": "Container",
        "docker_sgx": "Container+SGX",
    }

    def dissection_label(strategy: str) -> str:
        return dissection_labels.get(strategy, pretty_strategy_label(strategy))

    handshake_entries: list[tuple[str, list[float]]] = []
    handshake_cis: dict[str, float] = {}
    for strategy in STRATEGY_ORDER:
        if strategy in fresh_handshakes:
            handshake_entries.append((dissection_label(strategy), fresh_handshakes[strategy]))
            handshake_cis[dissection_label(strategy)] = mode_cis["fresh"][0][strategy]
        if strategy in {"docker", "docker_sgx"} and strategy in resumed_handshakes:
            handshake_entries.append(
                (f"{dissection_label(strategy)}\n(Resumption)", resumed_handshakes[strategy])
            )
            handshake_cis[handshake_entries[-1][0]] = mode_cis["resumption"][0][strategy]
    request_entries = [
        (dissection_label(strategy), persistent_requests[strategy])
        for strategy in STRATEGY_ORDER
        if strategy in persistent_requests
    ]
    request_cis = {
        dissection_label(strategy): mode_cis["persistent"][1][strategy]
        for strategy in persistent_requests
    }

    def draw_components(
        ax: Any,
        entries: list[tuple[str, list[float]]],
        labels: list[str],
        colors: list[str],
        title: str,
        legend_labels: list[str] | None = None,
        x_limit: float | None = None,
        mark_overflow: bool = False,
        total_cis: dict[str, float] | None = None,
        x_padding: float = 1.2,
    ) -> tuple[list[Any], list[str]]:
        positions = list(range(len(entries)))
        left = [0.0] * len(entries)
        for index, label in enumerate(labels):
            values = [components[index] for _, components in entries]
            legend_label = legend_labels[index] if legend_labels is not None else label
            bar_colors = [
                "lightgray"
                if labels is request_labels and entry_label.startswith("TLMSP")
                and index in {1, 3, 6, 8, 9}
                else colors[index]
                for entry_label, _ in entries
            ]
            ax.barh(
                positions,
                values,
                left=left,
                color=bar_colors,
                label=legend_label,
                height=0.8,
            )
            left = [current + value for current, value in zip(left, values)]
        for y, total in enumerate(left):
            ci = (total_cis or {}).get(entries[y][0], 0.0)
            if x_limit is not None and mark_overflow and total > x_limit:
                marker_center = x_limit * 0.88
                ax.annotate(
                    "",
                    xy=(marker_center + x_limit * 0.05, y + 0.25),
                    xytext=(marker_center - x_limit * 0.05, y + 0.25),
                    arrowprops={"arrowstyle": "-|>", "color": "black", "lw": 1.2},
                )
                ax.text(
                    marker_center,
                    y - 0.02,
                    f"{total:.2f} ms",
                    ha="center",
                    va="center",
                    fontsize=12,
                    bbox={"facecolor": "white", "edgecolor": "none", "alpha": 0.9},
                )
            else:
                if ci > 0:
                    ax.errorbar(total, y, xerr=ci, fmt="none", ecolor="black",
                                capsize=3, elinewidth=1, capthick=1, zorder=4)
                    ax.annotate(f"{total:.2f} ms", (total + ci, y),
                                xytext=(4, 0), textcoords="offset points",
                                va="center", fontsize=12)
                else:
                    ax.text(total, y, f" {total:.2f} ms", va="center", fontsize=12)
        ax.set_yticks(positions)
        ax.set_yticklabels([label for label, _ in entries], fontsize=12)
        ax.invert_yaxis()
        # ax.set_title(title)
        ax.set_xlabel("Mean latency (ms)", fontsize=13)
        ax.tick_params(axis="x", labelsize=12)
        upper = max(total + (total_cis or {}).get(label, 0.0)
                    for (label, _), total in zip(entries, left))
        ax.set_xlim(left=0, right=x_limit if x_limit is not None else upper * x_padding)
        ax.margins(y=0.03)
        ax.grid(True, axis="x", alpha=0.25)
        return ax.get_legend_handles_labels()

    fig, axes = plt.subplots(1, 2, figsize=(10.0, 5.0))
    handshake_legend = draw_components(
        axes[0],
        handshake_entries,
        handshake_labels,
        handshake_colors,
        "Connection and TLS handshake",
        handshake_legend_labels,
        total_cis=handshake_cis,
        x_padding=1.4,
    )
    request_legend = draw_components(
        axes[1],
        request_entries,
        request_labels,
        request_colors,
        "Request and response",
        request_legend_labels,
        total_cis=request_cis,
        x_padding=1.4,
    )
    fig.legend(
        handshake_legend[0] + request_legend[0],
        handshake_legend[1] + request_legend[1],
        loc="lower center",
        ncol=5,
        bbox_to_anchor=(0.5, 0.01),
        fontsize=10,
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
        total_cis: dict[str, float] | None = None,
    ) -> None:
        panel_fig, panel_ax = plt.subplots(figsize=(7.0, 5.5))
        handles, rendered_legend_labels = draw_components(
            panel_ax, entries, labels, colors, title, legend_labels,
            total_cis=total_cis,
        )
        panel_fig.legend(
            handles,
            rendered_legend_labels,
            loc="lower center",
            ncol=legend_ncol,
            bbox_to_anchor=(0.54, 0.01),
            fontsize=11,
            handlelength=1.3,
            handletextpad=0.4,
            columnspacing=0.8,
        )
        legend_rows = math.ceil(len(handles) / legend_ncol)
        legend_bottom = 0.20 + legend_rows * 0.04
        panel_fig.subplots_adjust(
            left=0.20,
            right=0.98,
            top=0.98,
            bottom=0.22,
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
        3,
        total_cis=handshake_cis,
    )
    save_single_panel(
        request_entries,
        request_labels,
        request_colors,
        "Request and response",
        request_path,
        request_legend_labels,
        2,
        total_cis=request_cis,
    )

    dual_scale_path = output_dir / "P2-request-latency-dissection-dual-scale.pdf"
    dc_request_entries = [entry for entry in request_entries if entry[0] != "TLMSP"]
    tlmsp_request_entries = [entry for entry in request_entries if entry[0] == "TLMSP"]
    written_paths = [combined_path, handshake_path, request_path]
    if dc_request_entries and tlmsp_request_entries:
        dc_limit = max(sum(components) + request_cis[label]
                       for label, components in dc_request_entries) * 1.18
        dual_fig, dual_ax = plt.subplots(figsize=(7.0, 5.5))
        dual_legend = draw_components(
            dual_ax,
            request_entries,
            request_labels,
            request_colors,
            "Request and response",
            request_legend_labels,
            x_limit=dc_limit,
            mark_overflow=True,
            total_cis=request_cis,
        )

        overview_ax = dual_ax.inset_axes([0.58, 0.70, 0.39, 0.26])
        overview_positions = list(range(len(request_entries)))
        overview_left = [0.0] * len(request_entries)
        for index, color in enumerate(request_colors):
            values = [components[index] for _, components in request_entries]
            bar_colors = [
                "lightgray"
                if entry_label == "TLMSP" and index in {1, 3, 6, 8, 9}
                else color
                for entry_label, _ in request_entries
            ]
            overview_ax.barh(
                overview_positions,
                values,
                left=overview_left,
                color=bar_colors,
                height=0.72,
            )
            overview_left = [
                current + value for current, value in zip(overview_left, values)
            ]
        overview_cis = [request_cis[label] for label, _ in request_entries]
        overview_ax.errorbar(
            overview_left, overview_positions, xerr=overview_cis,
            fmt="none", ecolor="black", capsize=2, elinewidth=0.8,
            capthick=0.8, zorder=4,
        )
        overview_ax.set_xlim(0, max(total + ci for total, ci in
                                   zip(overview_left, overview_cis)) * 1.05)
        overview_ax.set_yticks([])
        overview_ax.invert_yaxis()
        overview_ax.set_title("Full scale", fontsize=10, pad=2)
        overview_ax.tick_params(axis="x", labelsize=9, length=2)
        overview_ax.grid(True, axis="x", alpha=0.2)
        overview_ax.set_facecolor("white")
        overview_ax.patch.set_alpha(1.0)
        for spine in overview_ax.spines.values():
            spine.set_linewidth(0.8)

        dual_fig.legend(
            dual_legend[0],
            dual_legend[1],
            loc="lower center",
            ncol=3,
            handlelength=1.3,
            handletextpad=0.4,
            columnspacing=0.8,
            bbox_to_anchor=(0.54, 0.01),
            fontsize=11,
        )
        dual_fig.subplots_adjust(
            left=0.20,
            right=0.98,
            top=0.98,
            bottom=0.22,
        )
        dual_fig.savefig(dual_scale_path)
        plt.close(dual_fig)
        written_paths.append(dual_scale_path)

    baremetal_request = next(
        (components for label, components in request_entries if label == "Shared"),
        None,
    )
    tlmsp_request = next(
        (components for label, components in request_entries if label == "TLMSP"),
        None,
    )
    if baremetal_request is not None and tlmsp_request is not None:
        integrated_tlmsp = list(tlmsp_request)
        integrated_tlmsp[2] = baremetal_request[2]
        integrated_tlmsp[7] = baremetal_request[7]
        integrated_entries = [
            entry for entry in request_entries if entry[0] != "TLMSP"
        ]
        integrated_entries.append(("TLMSP", integrated_tlmsp))
        integrated_path = (
            output_dir / "P2-request-latency-dissection-integrated-validation-estimate.pdf"
        )
        save_single_panel(
            integrated_entries,
            request_labels,
            request_colors,
            "Request and response",
            integrated_path,
            request_legend_labels,
            2,
        )
        written_paths.append(integrated_path)
    return written_paths


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
    parser.add_argument(
        "--handshake-capacity-only", action="store_true",
        help="regenerate T4 from raw handshakes without loading the full campaign",
    )
    parser.add_argument(
        "--scalability-rerun-dir", type=Path, default=None,
        help="five additional 30-client Docker+SGX runs at 10 requests/s for P7 and T2",
    )
    args = parser.parse_args()

    campaign_dir = args.campaign_dir.resolve()
    out_dir = args.out_dir.resolve() if args.out_dir else campaign_dir / "plots"
    timeseries_filename = "e2e_timeseries.pdf"
    samples_filename = "e2e_latency_samples.csv"
    summary_filename = "run_summary.csv"

    if args.handshake_capacity_only:
        with (campaign_dir / "plots" / summary_filename).open(newline="", encoding="utf-8") as handle:
            summary_rows = list(csv.DictReader(handle))
        if not has_complete_handshake_capacity_data(summary_rows):
            raise SystemExit(f"incomplete handshake-capacity matrix under {campaign_dir}")
        windowed = handshake_capacity_windowed_rows(campaign_dir, summary_rows)
        table_path = out_dir / "T4-handshake-capacity.tex"
        csv_path = out_dir / "T4-handshake-capacity-windowed.csv"
        write_handshake_capacity_latex(windowed, table_path)
        write_handshake_capacity_csv(windowed, csv_path)
        print(f"[PLOT] wrote {table_path}")
        print(f"[PLOT] wrote {csv_path}")
        return

    samples, run_summaries = load_campaign(campaign_dir)
    if not run_summaries:
        raise SystemExit(f"no runs with metadata/client.csv found under {campaign_dir}")

    summary_rows = build_run_summary_rows(samples, run_summaries)
    tlmsp_samples, tlmsp_summary_rows = load_tlmsp_campaign(campaign_dir / "tlmsp")
    samples.extend(tlmsp_samples)
    summary_rows.extend(tlmsp_summary_rows)
    scalability_rows = summary_rows
    if args.scalability_rerun_dir is not None:
        rerun_dir = args.scalability_rerun_dir.resolve()
        if rerun_dir == campaign_dir:
            raise SystemExit("scalability rerun directory must differ from the primary campaign")
        rerun_samples, rerun_summaries = load_campaign(rerun_dir)
        rerun_rows = build_run_summary_rows(rerun_samples, rerun_summaries)
        if len(rerun_rows) != 5 or any(
            str(row["deployment"]) != "docker_sgxgo_full_reuse"
            or str(row["mode"]) != "persistent"
            or parse_int(row.get("clients")) != 30
            or not math.isclose(parse_float(row.get("offered_rps")) or 0, 10.0)
            or not point_is_usable(row)
            for row in rerun_rows
        ):
            raise SystemExit("expected five usable Docker+SGX persistent 30-client 10-rps reruns")
        scalability_rows = summary_rows + rerun_rows
        write_summary_csv(rerun_rows, out_dir / "P7-supplemental-run-summary.csv")
        print("[PLOT] pooling five supplemental 30-client Docker+SGX runs for P7 and T2")
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
    scalability_paths = plot_scalability(scalability_rows, out_dir)
    aggregate_scalability_paths = plot_aggregate_scalability(
        scalability_rows, out_dir / "P7-client-scalability.pdf"
    )
    epc_scalability_paths = plot_epc_scalability(run_summaries, out_dir)
    resource_table_path = out_dir / "analysis-resource-summary.tex"
    wrote_resource_table = write_resource_summary_latex(summary_rows, resource_table_path)
    component_table_path = out_dir / "T3-component-costs.tex"
    wrote_component_table = write_component_costs_latex(run_summaries, component_table_path)
    sustainable_table_path = out_dir / "P5-single-client-capacity.tex"
    wrote_sustainable_table = write_sustainable_capacity_latex(summary_rows, sustainable_table_path)
    handshake_table_path = out_dir / "T4-handshake-capacity.tex"
    windowed_handshakes = (
        handshake_capacity_windowed_rows(campaign_dir, summary_rows)
        if has_complete_handshake_capacity_data(summary_rows)
        else []
    )
    wrote_handshake_table = write_handshake_capacity_latex(windowed_handshakes, handshake_table_path)
    windowed_handshake_csv_path = out_dir / "T4-handshake-capacity-windowed.csv"
    wrote_windowed_handshake_csv = write_handshake_capacity_csv(
        windowed_handshakes, windowed_handshake_csv_path
    )
    handshake_csv_path = out_dir / "handshake-capacity-summary.csv"
    wrote_handshake_csv = write_handshake_capacity_csv(summary_rows, handshake_csv_path)
    selected_resources_path = out_dir / "T1-selected-load-resources.tex"
    wrote_selected_resources = write_selected_load_resources_latex(summary_rows, selected_resources_path)
    clients_memory_path = out_dir / "T2-clients-memory.tex"
    wrote_clients_memory = write_clients_memory_latex(scalability_rows, clients_memory_path)
    dissection_paths = plot_latency_dissection(samples, run_summaries, out_dir, summary_rows)

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
        (wrote_windowed_handshake_csv, windowed_handshake_csv_path),
        (wrote_handshake_csv, handshake_csv_path),
        (wrote_selected_resources, selected_resources_path),
        (wrote_clients_memory, clients_memory_path),
    ):
        if wrote:
            print(f"[PLOT] wrote {path}")
    for path in dissection_paths:
        print(f"[PLOT] wrote {path}")
    for path in aggregate_scalability_paths:
        print(f"[PLOT] wrote {path}")
    for path in epc_scalability_paths:
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
        "P5a-single-client-throughput-latency.pdf",
        "P5b-single-client-throughput-cpu.pdf",
        "P4-instance-startup-time.pdf",
    ):
        path = out_dir / filename
        if path.exists():
            print(f"[PLOT] wrote {path}")


if __name__ == "__main__":
    main()
