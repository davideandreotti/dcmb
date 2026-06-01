#grafici.py
from __future__ import annotations

import argparse
import importlib
import re
import shutil
import statistics
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from grafici_t1_t10_explosion import render_t1_t10_explosion_chart

TIMESTAMP_RE = re.compile(r"\bt(\d+)\b\s*:\s*[^\n\r]*?=\s*(\d+)")
EXP_MARKER_RE = re.compile(r"^=====\s*ESPERIMENTO\s+(\d+)\s*=====$")
OP_MARKER_RE = re.compile(r"^---\s*OPERAZIONE\s+(\d+)\s*\(esperimento\s+\d+\)\s*---$")
MB_CONTAINER_CREATION_RE = re.compile(r"\[MB\]\s*Container\s+creation\s+time\s*=\s*(\d+)\s*ns")
MB_POD_CREATION_RE = re.compile(r"\[MB\]\s*Pod\s+creation\s+time\s*=\s*(\d+)\s*ns")


############################################
# METRICHE
############################################

RAW_TIMESTAMP_METRICS = [f"t{index}" for index in range(1, 29)] + ["t37", "t38", "t39", "t40"]
TOTAL_METRIC = "t10 - t1"
MB_CONTAINER_CREATION_TIME = "MB Container Creation Time"
MB_POD_CREATION_TIME = "MB Pod Creation Time"
PARTIAL_DIFF_METRICS = [
    "t2 - t1",
    "t28 - t27",
    "t3 - t2",
    "t4 - t3",
    "t5 - t4",
    "t6 - t5",
    "t7 - t6",
    "t8 - t7",
    "t38 - t37",
    "t39 - t38",
    "t40 - t39",
    "t26 - t25",
    "t10 - t40",
    "t11 - t8",
    "t12 - t11",
    "t13 - t12",
    "t14 - t13",
    "t15 - t14",
    "t16 - t15",
    "t17 - t16",
    "t18 - t17",
    "t19 - t18",
    "t9 - t19",
    "t9 - t8",
    "t10 - t9",
    "t21 - t20",
    "t23 - t22",
    "t24 - t23",
]
DISABLED_METRICS_FOR_ALL_OPERATIONS = {"t23 - t22"}

# Disabilitazioni per scenari Docker (4 operazioni, op4 = riapertura sessione).
DISABLED_OPS_DOCKER = {
    "t2 - t1": {4},
    "t3 - t2": {4},
    "t4 - t3": {3, 4},
    "t5 - t4": {3, 4},
    "t6 - t5": {3, 4},
    "t7 - t6": {3, 4},
    "t8 - t7": {3, 4},
    "t9 - t8": {3, 4},
    "t9 - t19": {3, 4},
    "t10 - t9": {3, 4},
    "t11 - t8": {3, 4},
    "t12 - t11": {3, 4},
    "t13 - t12": {3, 4},
    "t14 - t13": {3, 4},
    "t15 - t14": {3, 4},
    "t16 - t15": {3, 4},
    "t17 - t16": {3, 4},
    "t18 - t17": {3, 4},
    "t19 - t18": {3, 4},
    "t38 - t37": {4},
    "t39 - t38": {4},
    "t40 - t39": {4},
    "t10 - t40": {4},
}

# Disabilitazioni per scenari Kubernetes (3 operazioni, op3 = riapertura).
DISABLED_OPS_K8S = {
    "t4 - t3": {3},
    "t5 - t4": {3},
    "t6 - t5": {3},
    "t7 - t6": {3},
    "t8 - t7": {3},
    "t9 - t8": {3},
    "t9 - t19": {3},
    "t10 - t9": {3},
    "t11 - t8": {3},
    "t12 - t11": {3},
    "t13 - t12": {3},
    "t14 - t13": {3},
    "t15 - t14": {3},
    "t16 - t15": {3},
    "t17 - t16": {3},
    "t18 - t17": {3},
    "t19 - t18": {3},
    "t26 - t25": {3},
    "t40 - t39": {3},
    "t10 - t40": {3},
}


############################################
# CONFIGURAZIONE SCENARIO
############################################


@dataclass
class ScenarioConfig:
    name: str
    label: str
    ops: list[int]
    container_metric: str | None
    container_regex: re.Pattern | None
    disabled_ops_by_metric: dict[str, set[int]]
    grouping: str  # "operation", "experiment", "anchor"


def detect_scenario(path: Path) -> ScenarioConfig:
    parts = {part for part in path.resolve().parts}

    if "MisureSGXKubernetes" in parts:
        return ScenarioConfig(
            name="sgx_kubernetes",
            label="SGX Kubernetes",
            ops=[1, 2, 3],
            container_metric=MB_POD_CREATION_TIME,
            container_regex=MB_POD_CREATION_RE,
            disabled_ops_by_metric=DISABLED_OPS_K8S,
            grouping="operation",
        )
    if "MisureOrchestrateKubernetes" in parts:
        return ScenarioConfig(
            name="orchestrate_kubernetes",
            label="Orchestrate Kubernetes",
            ops=[1],
            container_metric=MB_POD_CREATION_TIME,
            container_regex=MB_POD_CREATION_RE,
            disabled_ops_by_metric={},
            grouping="anchor",
        )
    if "MisureKubernetes" in parts:
        return ScenarioConfig(
            name="kubernetes",
            label="Kubernetes",
            ops=[1, 2, 3],
            container_metric=MB_POD_CREATION_TIME,
            container_regex=MB_POD_CREATION_RE,
            disabled_ops_by_metric=DISABLED_OPS_K8S,
            grouping="operation",
        )
    if "MisureOrchestrate" in parts:
        return ScenarioConfig(
            name="orchestrate",
            label="Orchestrate Docker",
            ops=[1],
            container_metric=MB_CONTAINER_CREATION_TIME,
            container_regex=MB_CONTAINER_CREATION_RE,
            disabled_ops_by_metric={},
            grouping="anchor",
        )
    if "MisureBaremetal" in parts:
        return ScenarioConfig(
            name="baremetal",
            label="Baremetal",
            ops=[1],
            container_metric=None,
            container_regex=None,
            disabled_ops_by_metric={},
            grouping="anchor",
        )
    # Default: baseline Misure (Docker).
    return ScenarioConfig(
        name="misure",
        label="Misure (Docker)",
        ops=[1, 2, 3, 4],
        container_metric=MB_CONTAINER_CREATION_TIME,
        container_regex=MB_CONTAINER_CREATION_RE,
        disabled_ops_by_metric=DISABLED_OPS_DOCKER,
        grouping="operation",
    )


############################################
# RISOLUZIONE PATH E LOG
############################################


LOG_NAMES = {
    "client": ["Client.log", "client.log", "ClientLog.txt"],
    "middlebox": ["Middlebox.log", "middlebox.log", "MiddleboxLog.txt"],
    "server": ["Server.log", "server.log", "ServerLog.txt"],
}


def resolve_log_path(directory: Path, role: str) -> Path | None:
    for name in LOG_NAMES[role]:
        candidate = directory / name
        if candidate.exists():
            return candidate
    return None


def resolve_runtime_dir(measurement_dir: Path) -> Path:
    if not measurement_dir.exists():
        raise FileNotFoundError(f"Cartella non trovata: {measurement_dir}")

    candidates = [measurement_dir / "Runtime", measurement_dir]
    for candidate in candidates:
        if not candidate.is_dir():
            continue
        if resolve_log_path(candidate, "client") is not None:
            return candidate

    raise FileNotFoundError(
        f"Nessun log Client.log/Middlebox.log/Server.log trovato sotto {measurement_dir}"
    )


def read_text(path: Path | None) -> str:
    if path is None or not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


############################################
# PARSING SEZIONI
############################################


def split_experiment_sections(text: str) -> dict[int, str]:
    sections: dict[int, list[str]] = {}
    current: int | None = None
    for line in text.splitlines():
        marker = EXP_MARKER_RE.match(line.strip())
        if marker:
            current = int(marker.group(1))
            sections.setdefault(current, [])
            continue
        if current is not None:
            sections[current].append(line)
    return {key: "\n".join(value) for key, value in sections.items()}


def split_operation_sections(text: str) -> dict[int, str]:
    sections: dict[int, list[str]] = {}
    current: int | None = None
    for line in text.splitlines():
        marker = OP_MARKER_RE.match(line.strip())
        if marker:
            current = int(marker.group(1))
            sections.setdefault(current, [])
            continue
        if current is not None:
            sections[current].append(line)
    return {key: "\n".join(value) for key, value in sections.items()}


def parse_ordered_times(text: str) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for line in text.splitlines():
        for match in TIMESTAMP_RE.finditer(line):
            out.append((int(match.group(1)), int(match.group(2))))
    return out


def latest_times(text: str) -> dict[int, int]:
    times: dict[int, int] = {}
    for ts, value in parse_ordered_times(text):
        times[ts] = value
    return times


def split_anchor_samples(text: str, anchor_ts: int = 1) -> list[dict[int, int]]:
    """Suddivide il log in campioni usando un timestamp ancora (default t1)."""
    samples: list[dict[int, int]] = []
    current: dict[int, int] | None = None
    for ts, value in parse_ordered_times(text):
        if ts == anchor_ts:
            if current:
                samples.append(current)
            current = {ts: value}
            continue
        if current is None:
            current = {}
        current[ts] = value
    if current:
        samples.append(current)
    return samples


def build_series_by_timestamp(text: str) -> dict[int, list[int]]:
    series: dict[int, list[int]] = {}
    for ts, value in parse_ordered_times(text):
        series.setdefault(ts, []).append(value)
    return series


############################################
# RICOSTRUZIONE PER OPERAZIONE / ESPERIMENTO
############################################


def reconstruct_operation_grouping(
    client_text: str,
    middlebox_text: str,
    server_text: str,
    ops: list[int],
) -> dict[int, dict[int, dict[int, int]]]:
    """Modello classico: ESPERIMENTO -> OPERAZIONE -> {ts: valore}."""
    client_exp = split_experiment_sections(client_text)
    middlebox_exp = split_experiment_sections(middlebox_text)
    server_exp = split_experiment_sections(server_text)

    all_exp = sorted(set(client_exp) | set(middlebox_exp) | set(server_exp))
    out: dict[int, dict[int, dict[int, int]]] = {
        exp: {op: {} for op in ops} for exp in all_exp
    }

    for exp in all_exp:
        client_ops = {
            op: latest_times(text)
            for op, text in split_operation_sections(client_exp.get(exp, "")).items()
        }
        middlebox_ops = {
            op: latest_times(text)
            for op, text in split_operation_sections(middlebox_exp.get(exp, "")).items()
        }
        server_ops = {
            op: latest_times(text)
            for op, text in split_operation_sections(server_exp.get(exp, "")).items()
        }

        for op in ops:
            merged: dict[int, int] = {}
            merged.update(client_ops.get(op, {}))
            merged.update(middlebox_ops.get(op, {}))
            merged.update(server_ops.get(op, {}))
            out[exp][op] = merged

    return out


def reconstruct_anchor_grouping(
    client_text: str,
    middlebox_text: str,
    server_text: str,
) -> dict[int, dict[int, dict[int, int]]]:
    """Allinea i campioni per indice usando TUTTI i timestamp presenti nei tre log."""
    client_series = build_series_by_timestamp(client_text)
    middlebox_series = build_series_by_timestamp(middlebox_text)
    server_series = build_series_by_timestamp(server_text)

    all_series = [client_series, middlebox_series, server_series]
    all_timestamps = sorted(
        {
            ts
            for series in all_series
            for ts in series
        }
    )
    total = max(
        (len(values) for series in all_series for values in series.values()),
        default=0,
    )

    out: dict[int, dict[int, dict[int, int]]] = {}
    for index in range(total):
        merged: dict[int, int] = {}
        for ts in all_timestamps:
            if index < len(client_series.get(ts, [])):
                merged[ts] = client_series[ts][index]
            if index < len(middlebox_series.get(ts, [])):
                merged[ts] = middlebox_series[ts][index]
            if index < len(server_series.get(ts, [])):
                merged[ts] = server_series[ts][index]
        out[index + 1] = {1: merged}
    return out


def resolve_measurement_root(input_path: Path, runtime_dir: Path) -> Path:
    if input_path.name.lower() == "runtime":
        return input_path.parent
    if runtime_dir.name.lower() == "runtime":
        return runtime_dir.parent
    return input_path


############################################
# CALCOLO METRICHE
############################################


def compute_diffs(times: dict[int, int]) -> dict[str, int]:
    diffs: dict[str, int] = {}
    for index in range(2, 20):
        if index in times and (index - 1) in times:
            diffs[f"t{index} - t{index - 1}"] = times[index] - times[index - 1]
    if 11 in times and 8 in times:
        diffs["t11 - t8"] = times[11] - times[8]
    if 9 in times and 19 in times:
        diffs["t9 - t19"] = times[9] - times[19]
    if 1 in times and 10 in times:
        diffs[TOTAL_METRIC] = times[10] - times[1]
    if 20 in times and 21 in times:
        diffs["t21 - t20"] = times[21] - times[20]
    if 27 in times and 28 in times:
        diffs["t28 - t27"] = times[28] - times[27]
    if 22 in times and 23 in times:
        diffs["t23 - t22"] = times[23] - times[22]
    if 23 in times and 24 in times:
        diffs["t24 - t23"] = times[24] - times[23]
    if 37 in times and 38 in times:
        diffs["t38 - t37"] = times[38] - times[37]
    if 38 in times and 39 in times:
        diffs["t39 - t38"] = times[39] - times[38]
    if 39 in times and 40 in times:
        diffs["t40 - t39"] = times[40] - times[39]
    if 40 in times and 10 in times:
        diffs["t10 - t40"] = times[10] - times[40]
    if 25 in times and 26 in times:
        diffs["t26 - t25"] = times[26] - times[25]
    return diffs


def all_metrics(scenario: ScenarioConfig) -> list[str]:
    metrics = list(RAW_TIMESTAMP_METRICS) + [TOTAL_METRIC] + list(PARTIAL_DIFF_METRICS)
    if scenario.container_metric:
        metrics.append(scenario.container_metric)
    return metrics


def should_generate_metric_pdf(metric: str) -> bool:
    if metric in RAW_TIMESTAMP_METRICS:
        return False
    if metric in DISABLED_METRICS_FOR_ALL_OPERATIONS:
        return False
    return True


def operation_is_enabled(scenario: ScenarioConfig, metric: str, op: int) -> bool:
    if not should_generate_metric_pdf(metric):
        return False
    if op not in scenario.ops:
        return False
    return op not in scenario.disabled_ops_by_metric.get(metric, set())


def metric_sort_key(metric: str, scenario: ScenarioConfig) -> tuple[int, int, str]:
    if metric.startswith("t") and metric[1:].isdigit():
        return (0, int(metric[1:]), metric)
    if metric == TOTAL_METRIC:
        return (1, 0, metric)
    if metric in PARTIAL_DIFF_METRICS:
        return (2, PARTIAL_DIFF_METRICS.index(metric), metric)
    if scenario.container_metric and metric == scenario.container_metric:
        return (3, 0, metric)
    return (4, 0, metric)


def extract_container_metric(
    middlebox_text: str, scenario: ScenarioConfig
) -> dict[int, list[tuple[int, int]]] | None:
    if not scenario.container_metric or not scenario.container_regex:
        return None
    series: dict[int, list[tuple[int, int]]] = {op: [] for op in scenario.ops}
    experiments = split_experiment_sections(middlebox_text)
    if experiments:
        for exp, text in experiments.items():
            match = scenario.container_regex.search(text)
            if match:
                series[scenario.ops[0]].append((exp, int(match.group(1))))
    else:
        for index, match in enumerate(scenario.container_regex.finditer(middlebox_text), start=1):
            series[scenario.ops[0]].append((index, int(match.group(1))))
    return series


def parse_metric_series(
    runtime_dir: Path, scenario: ScenarioConfig
) -> dict[str, dict[int, list[tuple[int, int]]]]:
    client_text = read_text(resolve_log_path(runtime_dir, "client"))
    middlebox_text = read_text(resolve_log_path(runtime_dir, "middlebox"))
    server_text = read_text(resolve_log_path(runtime_dir, "server"))

    if scenario.grouping == "operation":
        grouping = reconstruct_operation_grouping(
            client_text, middlebox_text, server_text, scenario.ops
        )
    else:
        grouping = reconstruct_anchor_grouping(client_text, middlebox_text, server_text)

    series: dict[str, dict[int, list[tuple[int, int]]]] = {
        metric: {op: [] for op in scenario.ops} for metric in all_metrics(scenario)
    }

    for exp in sorted(grouping):
        for op in scenario.ops:
            times = grouping[exp].get(op, {})
            if not times:
                continue
            diffs = compute_diffs(times)

            for ts in list(range(1, 29)) + [37, 38, 39, 40]:
                if ts in times:
                    series[f"t{ts}"][op].append((exp, times[ts]))

            if TOTAL_METRIC in diffs:
                series[TOTAL_METRIC][op].append((exp, diffs[TOTAL_METRIC]))

            for metric in PARTIAL_DIFF_METRICS:
                if metric in diffs:
                    series[metric][op].append((exp, diffs[metric]))

    container_data = extract_container_metric(middlebox_text, scenario)
    if container_data and scenario.container_metric:
        series[scenario.container_metric] = container_data

    return series


############################################
# OUTPUT TESTUALE
############################################


def write_grafici_txt(
    series: dict[str, dict[int, list[tuple[int, int]]]],
    runtime_dir: Path,
    scenario: ScenarioConfig,
    output_txt: Path,
) -> None:
    output_txt.parent.mkdir(parents=True, exist_ok=True)
    with output_txt.open("w", encoding="utf-8") as handle:
        handle.write(f"Sorgente runtime log: {runtime_dir}\n")
        handle.write(f"Scenario: {scenario.label}\n")
        handle.write("Metriche: timestamp grezzi, t10 - t1 e differenze parziali\n")
        handle.write("==============================\n")
        for metric in sorted(series, key=lambda name: metric_sort_key(name, scenario)):
            handle.write(f"METRICA {metric}:\n")
            for op in scenario.ops:
                handle.write(f"OPERAZIONI N_{op}:\n")
                values = series[metric].get(op, [])
                if not values:
                    handle.write("(nessun dato)\n")
                else:
                    for exp, value in values:
                        handle.write(f"{exp}_{op} = {value} ns\n")
            handle.write("------------------------------\n")


def write_diagnostics(
    series: dict[str, dict[int, list[tuple[int, int]]]],
    runtime_dir: Path,
    scenario: ScenarioConfig,
    diagnostic_txt: Path,
) -> None:
    diagnostic_txt.parent.mkdir(parents=True, exist_ok=True)
    client_text = read_text(resolve_log_path(runtime_dir, "client"))
    conn_refused = client_text.lower().count("connection refused")
    request_error = client_text.lower().count("error during request")

    lines = [
        f"Sorgente runtime log: {runtime_dir}",
        f"Scenario: {scenario.label}",
        "Diagnostica qualita dati timestamp",
        "==============================",
    ]
    total_complete = 0
    for op in scenario.ops:
        n_t1 = len(series.get("t1", {}).get(op, []))
        n_t10 = len(series.get("t10", {}).get(op, []))
        n_total = len(series.get(TOTAL_METRIC, {}).get(op, []))
        total_complete += n_total
        lines.append(
            f"OPERAZIONE N_{op}: campioni t1={n_t1}, t10={n_t10}, {TOTAL_METRIC}={n_total}"
        )
    lines.append("------------------------------")
    lines.append(f"Client connection refused: {conn_refused}")
    lines.append(f"Client error during request: {request_error}")
    if total_complete == 0:
        lines.append("ATTENZIONE: nessun campione completo t10 - t1.")
    diagnostic_txt.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_timestamp_avg_for_explosion(
    series: dict[str, dict[int, list[tuple[int, int]]]], scenario: ScenarioConfig
) -> dict[int, dict[str, float]]:
    # Timestamp che devono comparire nell'esploso solo se presenti su tutte le
    # richieste client (t1) per evitare medie distorte su campioni molto parziali.
    strict_full_coverage_tags = {"t4", "t5", "t6", "t7", "t8"}

    out: dict[int, dict[str, float]] = {op: {} for op in scenario.ops}
    for op in scenario.ops:
        t1_count = len(series.get("t1", {}).get(op, []))
        for ts in list(range(1, 29)) + [37, 38, 39, 40]:
            label = f"t{ts}"
            values = [value for _, value in series.get(label, {}).get(op, [])]
            if label in strict_full_coverage_tags and t1_count > 0 and len(values) < t1_count:
                continue
            if values:
                out[op][label] = float(statistics.fmean(values))
    return out


############################################
# PLOTTING
############################################


def _import_matplotlib():
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.backends.backend_pdf import PdfPages

    return plt, PdfPages


def _ensure_matplotlib_installed() -> bool:
    try:
        _import_matplotlib()
        return True
    except Exception:
        print("matplotlib non installato: provo installazione automatica...")

    def safe_run(command: list[str]) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(command, capture_output=True, text=True)
        except FileNotFoundError:
            return subprocess.CompletedProcess(command, returncode=127, stderr="not found")

    def has_module(name: str) -> bool:
        try:
            importlib.import_module(name)
            return True
        except ImportError:
            return False

    if not has_module("pip"):
        safe_run([sys.executable, "-m", "ensurepip", "--user"])

    attempts = [safe_run([sys.executable, "-m", "pip", "install", "--user", "matplotlib"])]
    if shutil.which("pip3"):
        attempts.append(safe_run(["pip3", "install", "--user", "matplotlib"]))

    if any(result.returncode == 0 for result in attempts):
        try:
            _import_matplotlib()
            return True
        except Exception:
            return False
    return False


def make_plots(
    series: dict[str, dict[int, list[tuple[int, int]]]],
    scenario: ScenarioConfig,
    output_pdf_dir: Path,
    *,
    show: bool,
    save_pdf: bool,
) -> None:
    if not save_pdf:
        return
    if not _ensure_matplotlib_installed():
        print("matplotlib non disponibile: salto la generazione dei PDF.")
        return

    plt, PdfPages = _import_matplotlib()
    output_pdf_dir.mkdir(parents=True, exist_ok=True)

    def ns_to_ms(value_ns: int) -> float:
        return value_ns / 1_000_000.0

    def points_to_ms(points: list[tuple[int, int]]) -> list[float]:
        return [ns_to_ms(value) for _, value in points]

    def scale_axis(axis, values_ms: list[float]) -> None:
        if not values_ms:
            return
        y_min = min(values_ms)
        y_max = max(values_ms)
        if y_min == y_max:
            delta = max(abs(y_min) * 0.1, 0.1)
            axis.set_ylim(max(0.0, y_min - delta), y_max + delta)
            return
        padding = (y_max - y_min) * 0.15
        axis.set_ylim(max(0.0, y_min - padding), y_max + padding)

    def set_xticks(axis, count: int) -> None:
        if count <= 0:
            return
        positions = [idx for idx in range(1, count + 1) if idx % 10 == 0]
        if not positions:
            positions = [1, count] if count > 1 else [1]
        axis.set_xticks(positions)
        axis.set_xticklabels([str(value) for value in positions])

    def draw_left(axis, points: list[tuple[int, int]], *, title: str) -> None:
        if points:
            y = points_to_ms(points)
            x = list(range(1, len(y) + 1))
            axis.scatter(x, y, color="tab:blue", alpha=0.8, s=24)
            avg = sum(y) / len(y)
            median = statistics.median(y)
            axis.axhline(avg, linestyle="--", color="tab:green", linewidth=1.5, label="Media")
            axis.axhline(median, linestyle="--", color="tab:red", linewidth=1.5, label="Mediana")
            axis.text(0.95, 0.10, f"Media = {avg:.3f} ms", color="tab:green",
                      fontsize=10, fontweight="bold", ha="right", va="top",
                      transform=axis.transAxes)
            axis.text(0.42, 0.10, f"Mediana = {median:.3f} ms", color="tab:red",
                      fontsize=10, fontweight="bold", ha="right", va="top",
                      transform=axis.transAxes)
            scale_axis(axis, y)
            set_xticks(axis, len(y))
        else:
            axis.text(0.5, 0.5, "Nessun dato", ha="center", va="center", transform=axis.transAxes)
        axis.set_title(title)
        axis.set_xlabel("Indice campione")
        axis.set_ylabel("Millisecondi")
        axis.grid(True, alpha=0.3)

    def draw_right(axis, points: list[tuple[int, int]], *, title: str) -> None:
        if points:
            y = points_to_ms(points)
            parts = axis.violinplot([y], positions=[1], widths=0.6,
                                    showmeans=False, showmedians=False, showextrema=True)
            for body in parts["bodies"]:
                body.set_alpha(0.35)
                body.set_facecolor("tab:blue")
            axis.set_xlim(0.5, 1.5)
            axis.set_xticks([1])
            axis.set_xticklabels([f"N={len(y)}"])
            scale_axis(axis, y)
        else:
            axis.text(0.5, 0.5, "Nessun dato", ha="center", va="center", transform=axis.transAxes)
        axis.set_title(title)
        axis.set_xlabel("Distribuzione campioni")
        axis.set_ylabel("Millisecondi")
        axis.grid(True, alpha=0.3)

    def save_metric_pdf(metric: str, metric_series: dict[int, list[tuple[int, int]]]) -> bool:
        ops_to_plot = [op for op in scenario.ops if operation_is_enabled(scenario, metric, op)]
        if not ops_to_plot:
            return False
        if all(not metric_series.get(op, []) for op in ops_to_plot):
            return False

        safe_name = metric.replace(" ", "_").replace("-", "minus")
        pdf_path = output_pdf_dir / f"{safe_name}.pdf"

        with PdfPages(pdf_path) as pdf:
            fig, axes = plt.subplots(len(ops_to_plot), 2, figsize=(20, 4.5 * len(ops_to_plot)))
            if len(ops_to_plot) == 1:
                axes = [axes]
            fig.suptitle(f"Distribuzione cumulata di {metric} per tipo operazione [{scenario.label}]", fontsize=13)
            for row, op in enumerate(ops_to_plot):
                points = metric_series.get(op, [])
                draw_left(axes[row][0], points, title=f"Operazione N_{op} - valori")
                draw_right(axes[row][1], points, title=f"Operazione N_{op} - violino")
            fig.tight_layout(rect=[0, 0.02, 1, 0.97])
            pdf.savefig(fig)
            plt.close(fig)

            for op in ops_to_plot:
                detail_fig, detail_axes = plt.subplots(1, 2, figsize=(16, 4.5))
                points = metric_series.get(op, [])
                detail_fig.suptitle(f"Distribuzione di {metric} - operazione N_{op} [{scenario.label}]", fontsize=13)
                draw_left(detail_axes[0], points, title=f"{metric} - valori")
                draw_right(detail_axes[1], points, title=f"{metric} - violino")
                detail_fig.tight_layout(rect=[0, 0.02, 1, 0.95])
                pdf.savefig(detail_fig)
                plt.close(detail_fig)

        if show:
            print(f"Creato PDF: {pdf_path}")
        return True

    generated = 0
    for metric in sorted(series, key=lambda name: metric_sort_key(name, scenario)):
        if should_generate_metric_pdf(metric):
            if save_metric_pdf(metric, series[metric]):
                generated += 1

    print(f"PDF generati con dati: {generated}")
    plt.close("all")


############################################
# CLI
############################################


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generatore unificato grafici a partire dai log di runtime."
    )
    # Supporta -path, --path e -p (anche stile -path=...).
    parser.add_argument(
        "-path",
        "--path",
        "-p",
        dest="path",
        required=True,
        help="Cartella con i log (o con sottocartella Runtime/).",
    )
    parser.add_argument(
        "--show",
        action="store_true",
        help="Mostra alcuni log durante la generazione.",
    )
    parser.add_argument(
        "--no-pdf",
        action="store_true",
        help="Non generare i PDF (solo file di testo).",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    input_path = Path(args.path).expanduser().resolve()
    runtime_dir = resolve_runtime_dir(input_path)
    measurement_dir = resolve_measurement_root(input_path, runtime_dir)
    scenario = detect_scenario(measurement_dir)

    analisi_dir = measurement_dir / "Analisi"
    analisi_dir.mkdir(parents=True, exist_ok=True)
    output_txt = analisi_dir / "Grafici.txt"
    diagnostic_txt = analisi_dir / "DiagnosticaRuntime.txt"

    print(f"Scenario rilevato: {scenario.label}")
    print(f"Cartella runtime:  {runtime_dir}")
    print(f"Output Analisi:    {analisi_dir}")

    series = parse_metric_series(runtime_dir, scenario)
    write_grafici_txt(series, runtime_dir, scenario, output_txt)
    write_diagnostics(series, runtime_dir, scenario, diagnostic_txt)
    make_plots(series, scenario, analisi_dir, show=args.show, save_pdf=not args.no_pdf)

    explosion_by_op = build_timestamp_avg_for_explosion(series, scenario)
    for op in scenario.ops:
        suffix = f"_Operazione_{op}" if len(scenario.ops) > 1 else ""
        render_t1_t10_explosion_chart(
            timestamp_avg_ns=explosion_by_op.get(op, {}),
            output_path=analisi_dir / f"T1_T10_Explosion{suffix}.png",
            title=f"Esplosione temporale t1->t10 [{scenario.label}]"
            + (f" - Operazione N_{op}" if len(scenario.ops) > 1 else ""),
            alpha=0.25,
        )

    print(f"File riepilogo:    {output_txt}")
    print(f"Diagnostica:       {diagnostic_txt}")
    if not args.no_pdf:
        print(f"PDF grafici in:    {analisi_dir}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
