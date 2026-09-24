#!/usr/bin/env python3

import argparse
import csv
import os
from pathlib import Path
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parents[2]
BENCHMARK_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "ETSI/Configurations/local_init.ucl"
DEFAULT_DRIVER = BENCHMARK_DIR / "tlmsp_loadgen"
DEFAULT_URL = "https://127.0.0.1:4444/function/init"
EVENT_PREFIX = "TLMSP_BENCH "
RESULT_PREFIX = "TLMSP_RESULT "
SUMMARY_PREFIX = "TLMSP_SUMMARY "
FIELDS = [
    "sample_id", "transfer_id", "mode", "success", "http_code",
    "curl_code", "num_connects", "local_port", "transfer_start_ns",
    "handshake_start_ns", "handshake_done_ns", "request_start_ns",
    "response_done_ns", "client_half_done_ns", "server_half_start_ns",
    "server_half_done_ns", "request_handler_start_ns",
    "request_handler_done_ns", "response_handler_start_ns",
    "response_handler_done_ns", "application_start_ns",
    "application_done_ns", "end_to_end_ms", "setup_ms",
    "handshake_ms", "request_ms", "client_half_ms", "server_half_ms",
    "path_before_request_validation_ms", "request_handler_ms",
    "path_to_application_ms", "application_ms",
    "path_to_response_validation_ms", "response_handler_ms",
    "path_after_response_validation_ms", "dissection_ok", "body_ok",
    "error",
]


def parse_key_values(line, prefix):
    if not line.startswith(prefix):
        return None
    values = {}
    for item in line[len(prefix):].strip().split():
        if "=" in item:
            key, value = item.split("=", 1)
            values[key] = value
    return values


def parse_client_events(lines):
    transfers = {}
    for line in lines:
        values = parse_key_values(line, EVENT_PREFIX)
        if not values or values.get("component") != "client":
            continue
        transfer = int(values["transfer"])
        events = transfers.setdefault(transfer, {})
        events[values["event"]] = int(values["ts_ns"])
        port = int(values.get("local_port", "-1"))
        if port > 0:
            events["local_port"] = port
    return transfers


def parse_middlebox_events(lines):
    by_port = {}
    for line in lines:
        values = parse_key_values(line, EVENT_PREFIX)
        if not values or values.get("component") != "middlebox":
            continue
        port = int(values.get("client_port", "0"))
        if port <= 0:
            continue
        events = by_port.setdefault(port, {})
        events.setdefault(values["event"], []).append(int(values["ts_ns"]))
    return by_port


def milliseconds(end, start):
    if not isinstance(end, int) or not isinstance(start, int) or end < start:
        return ""
    return f"{(end - start) / 1_000_000:.6f}"


def make_row(sample_id, mode, events, result, error=""):
    body_ok = result.get("body_ok") == "1"
    row = {field: "" for field in FIELDS}
    row.update({
        "sample_id": sample_id,
        "transfer_id": result.get("transfer", ""),
        "mode": mode,
        "http_code": result.get("http_code", "0"),
        "curl_code": result.get("curl_code", "0"),
        "num_connects": result.get("num_connects", ""),
        "local_port": events.get("local_port", result.get("local_port", "")),
        "body_ok": 1 if body_ok else 0,
        "error": error,
    })
    for event in (
        "transfer_start", "handshake_start", "handshake_done",
        "request_start", "response_done",
    ):
        row[f"{event}_ns"] = events.get(event, "")

    row["end_to_end_ms"] = milliseconds(
        events.get("response_done"), events.get("transfer_start"))
    row["setup_ms"] = milliseconds(
        events.get("handshake_done"), events.get("transfer_start"))
    row["handshake_ms"] = milliseconds(
        events.get("handshake_done"), events.get("handshake_start"))
    row["request_ms"] = milliseconds(
        events.get("response_done"), events.get("request_start"))

    required = ("transfer_start", "request_start", "response_done")
    event_ok = all(isinstance(events.get(name), int) for name in required)
    status_ok = result.get("curl_code") == "0" and \
        result.get("http_code") == "200"
    row["success"] = 1 if event_ok and status_ok and body_ok else 0
    if not row["success"] and not row["error"]:
        row["error"] = "missing events, request failure, or unexpected body"
    return row


def read_new_middlebox_events(path, offset):
    if path is None:
        return []
    with path.open("r", encoding="utf-8", errors="replace") as source:
        source.seek(offset)
        return source.readlines()


def event_for_row(event_values, row):
    if not event_values:
        return None
    if len(event_values) == 1:
        return event_values[0]
    try:
        index = int(row["transfer_id"]) - 1
    except (TypeError, ValueError):
        return None
    if 0 <= index < len(event_values):
        return event_values[index]
    return None


def merge_middlebox(rows, lines, raw_file):
    if not lines:
        return
    raw_file.write("TLMSP_RUN middlebox_events_begin=1\n")
    for line in lines:
        if line.startswith(EVENT_PREFIX):
            raw_file.write(line if line.endswith("\n") else line + "\n")
    raw_file.flush()

    by_port = parse_middlebox_events(lines)
    for row in rows:
        try:
            port = int(row["local_port"])
        except (TypeError, ValueError):
            continue
        events = by_port.get(port, {})

        # Half-handshake events belong only to transfers that opened a socket.
        try:
            opened_connection = int(row["num_connects"]) > 0
        except (TypeError, ValueError):
            opened_connection = False
        if opened_connection:
            for event in (
                "client_half_done", "server_half_start", "server_half_done",
            ):
                value = event_for_row(events.get(event), row)
                if value is not None:
                    row[f"{event}_ns"] = value
            row["client_half_ms"] = milliseconds(
                row.get("client_half_done_ns"), row.get("handshake_start_ns"))
            row["server_half_ms"] = milliseconds(
                row.get("server_half_done_ns"), row.get("server_half_start_ns"))

        for direction in ("request", "response"):
            start_name = f"{direction}_handler_start"
            done_name = f"{direction}_handler_done"
            start = event_for_row(events.get(start_name), row)
            done = event_for_row(events.get(done_name), row)
            if start is not None:
                row[f"{start_name}_ns"] = start
            if done is not None:
                row[f"{done_name}_ns"] = done
            row[f"{direction}_handler_ms"] = milliseconds(done, start)


def merge_application_trace(samples_path, trace_path):
    with samples_path.open(newline="", encoding="utf-8") as source:
        rows = list(csv.DictReader(source))

    application_events = {}
    with trace_path.open(newline="", encoding="utf-8") as source:
        for event in csv.DictReader(source):
            name = event.get("event_name", "")
            if name not in (
                    "requestserver_request_start",
                    "requestserver_response_start"):
                continue
            request_id = event.get("id", "")
            application_events.setdefault(request_id, {})[name] = int(
                event["timestamp_ns"])

    intervals = []
    for events in application_events.values():
        start = events.get("requestserver_request_start")
        done = events.get("requestserver_response_start")
        if start is not None and done is not None and done >= start:
            intervals.append((start, done))
    intervals.sort()

    for row in rows:
        try:
            request_start = int(row["request_start_ns"])
            response_done = int(row["response_done_ns"])
        except (KeyError, TypeError, ValueError):
            continue

        matches = [
            interval for interval in intervals
            if request_start <= interval[0] <= interval[1] <= response_done
        ]
        if len(matches) != 1:
            continue
        application_start, application_done = matches[0]
        row["application_start_ns"] = application_start
        row["application_done_ns"] = application_done
        row["application_ms"] = milliseconds(application_done, application_start)

        try:
            timeline = [
                request_start,
                int(row["request_handler_start_ns"]),
                int(row["request_handler_done_ns"]),
                application_start,
                application_done,
                int(row["response_handler_start_ns"]),
                int(row["response_handler_done_ns"]),
                response_done,
            ]
        except (KeyError, TypeError, ValueError):
            continue
        if any(done < start for start, done in zip(timeline, timeline[1:])):
            continue

        row["path_before_request_validation_ms"] = milliseconds(
            timeline[1], timeline[0])
        row["path_to_application_ms"] = milliseconds(timeline[3], timeline[2])
        row["path_to_response_validation_ms"] = milliseconds(
            timeline[5], timeline[4])
        row["path_after_response_validation_ms"] = milliseconds(
            timeline[7], timeline[6])
        row["dissection_ok"] = 1

    with samples_path.open("w", newline="", encoding="utf-8") as target:
        writer = csv.DictWriter(target, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(
            {field: row.get(field, "") for field in FIELDS}
            for row in rows
        )


def driver_command(args):
    command = [
        str(args.driver), "--mode", args.mode,
        "--url", args.url, "--config", str(args.config),
        "--warmup", str(args.warmup),
        "--warmup-pause-ms", str(args.warmup_pause_ms),
        "--max-in-flight", str(args.max_in_flight),
    ]
    if args.duration_seconds is None:
        command.extend([
            "--requests", str(args.samples),
            "--interval-ms", str(args.interval_ms),
        ])
    else:
        command.extend([
            "--duration-seconds", str(args.duration_seconds),
            "--rate", str(args.rate),
        ])
    return command


def run_driver(args, environment, raw_file):
    if not args.driver.is_file():
        raise RuntimeError(
            f"TLMSP load generator not found: {args.driver}; "
            f"run make in {BENCHMARK_DIR}")
    process = subprocess.Popen(
        driver_command(args), env=environment, stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE, text=True, bufsize=1)
    lines = []
    assert process.stderr is not None
    for line in process.stderr:
        line = line.rstrip("\n")
        lines.append(line)
        raw_file.write(line + "\n")
        raw_file.flush()
    return_code = process.wait()
    if return_code not in (0, 2):
        raise RuntimeError(f"TLMSP load generator exited with status {return_code}")

    transfers = parse_client_events(lines)
    rows = []
    summary = None
    for line in lines:
        result = parse_key_values(line, RESULT_PREFIX)
        if result and result.get("component") == "driver":
            if result.get("warmup") == "1":
                continue
            transfer = int(result["transfer"])
            sample = int(result["sample"])
            rows.append(make_row(
                sample, args.mode, transfers.get(transfer, {}), result))
            if len(rows) == 1 or len(rows) % args.progress_every == 0:
                target = args.samples if args.duration_seconds is None else "duration"
                print(f"{args.mode}: completed {len(rows)}/{target}", flush=True)
        parsed_summary = parse_key_values(line, SUMMARY_PREFIX)
        if parsed_summary:
            summary = parsed_summary
    return rows, summary


def parse_args():
    parser = argparse.ArgumentParser(
        description="Collect raw TLMSP fresh or persistent latency samples")
    parser.add_argument("mode", choices=("fresh", "persistent"))
    parser.add_argument("--samples", type=int, default=1000)
    parser.add_argument("--warmup", type=int, default=1)
    parser.add_argument("--interval-ms", type=int, default=1000)
    parser.add_argument("--duration-seconds", type=float)
    parser.add_argument("--rate", type=float)
    parser.add_argument("--warmup-pause-ms", type=int, default=0)
    parser.add_argument("--max-in-flight", type=int, default=1024)
    parser.add_argument("--progress-every", type=int, default=25)
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--driver", type=Path, default=DEFAULT_DRIVER)
    parser.add_argument("--middlebox-log", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.samples <= 0 or args.warmup < 0 or args.interval_ms < 0:
        parser.error("samples must be positive; warmup and interval must be nonnegative")
    if args.warmup_pause_ms < 0 or args.max_in_flight <= 0:
        parser.error("warmup pause must be nonnegative and max in flight positive")
    if args.duration_seconds is not None:
        if args.duration_seconds <= 0 or args.rate is None or args.rate <= 0:
            parser.error("duration mode requires positive --duration-seconds and --rate")
    elif args.rate is not None:
        parser.error("--rate requires --duration-seconds")
    if args.output is None:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        args.output = BENCHMARK_DIR / "results" / f"tlmsp_{args.mode}_{stamp}.csv"
    return args


def main():
    args = parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    raw_path = args.output.with_suffix(".events.log")
    environment = os.environ.copy()
    environment["TLMSP_BENCH_TRACE"] = "1"
    library_path = str(ROOT / ".tlmsp/lib")
    old_library_path = environment.get("LD_LIBRARY_PATH")
    environment["LD_LIBRARY_PATH"] = (
        library_path if not old_library_path else library_path + ":" + old_library_path)

    middlebox_offset = 0
    if args.middlebox_log is not None:
        if not args.middlebox_log.exists():
            raise RuntimeError(f"middlebox log does not exist: {args.middlebox_log}")
        middlebox_offset = args.middlebox_log.stat().st_size

    with raw_path.open("w", encoding="utf-8") as raw_file:
        rows, summary = run_driver(args, environment, raw_file)
        middlebox_lines = read_new_middlebox_events(
            args.middlebox_log, middlebox_offset)
        merge_middlebox(rows, middlebox_lines, raw_file)

    rows.sort(key=lambda row: int(row["sample_id"]))
    with args.output.open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    failures = sum(int(row["success"]) == 0 for row in rows)
    print(f"wrote {len(rows)} samples to {args.output}")
    print(f"raw events: {raw_path}")
    print(f"failed samples: {failures}")
    if summary:
        print(
            "summary: " + " ".join(
                f"{key}={value}" for key, value in summary.items()))
    return 0 if failures == 0 else 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, RuntimeError) as error:
        print(f"error: {error}", file=sys.stderr)
        sys.exit(1)
