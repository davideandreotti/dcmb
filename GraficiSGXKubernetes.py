from __future__ import annotations

import argparse
import importlib
import re
import shutil
import statistics
import subprocess
import sys
from pathlib import Path

from grafici_t1_t10_explosion import render_t1_t10_explosion_chart


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR if (SCRIPT_DIR / "DC").exists() else SCRIPT_DIR.parent

BASE_DIR = PROJECT_ROOT / "MisureSGXKubernetes" / "Analisi"
RUNTIME_DIR_DEFAULT = PROJECT_ROOT / "MisureSGXKubernetes" / "Runtime"
OUTPUT_TXT = BASE_DIR / "Grafici.txt"
DIAGNOSTIC_TXT = BASE_DIR / "DiagnosticaRuntime.txt"
OUTPUT_PDF_DIR = BASE_DIR
EXPLOSION_ALPHA = 0.25

CLIENT_RUNTIME_LOG = "Client.log"
MIDDLEBOX_RUNTIME_LOG = "Middlebox.log"
SERVER_RUNTIME_LOG = "Server.log"

TIMESTAMP_RE = re.compile(r"\bt(\d+)\b\s*:\s*[^\n\r]*?=\s*(\d+)")
EXP_MARKER_RE = re.compile(r"^===== ESPERIMENTO (\d+) =====$")
OP_MARKER_RE = re.compile(r"^--- OPERAZIONE (\d+) \(esperimento \d+\) ---$")
MB_POD_CREATION_RE = re.compile(r"\[MB\] Pod creation time = (\d+) ns")

RAW_TIMESTAMP_METRICS = [f"t{index}" for index in range(1, 29)] + ["t37", "t38", "t39", "t40"]
TOTAL_METRIC = "t10 - t1"
MB_POD_CREATION_TIME = "MB Pod Creation Time"
CONTAINER_METRICS = [MB_POD_CREATION_TIME]
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
ALL_METRICS = RAW_TIMESTAMP_METRICS + [TOTAL_METRIC] + PARTIAL_DIFF_METRICS + CONTAINER_METRICS

DISABLED_METRICS_FOR_ALL_OPERATIONS = {
    "t23 - t22",
}

# Solo operazioni 1-3 in Kubernetes (nessuna op 4)
DISABLED_OPERATIONS_BY_METRIC: dict[str, set[int]] = {
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

# Numero di operazioni Kubernetes (1, 2, 3)
OPS = list(range(1, 4))


def should_generate_metric_pdf(metric: str) -> bool:
    if metric in RAW_TIMESTAMP_METRICS:
        return False
    if metric in DISABLED_METRICS_FOR_ALL_OPERATIONS:
        return False
    return True


def operation_is_enabled_for_metric(metric: str, operation: int) -> bool:
    if not should_generate_metric_pdf(metric):
        return False
    if operation not in OPS:
        return False
    return operation not in DISABLED_OPERATIONS_BY_METRIC.get(metric, set())


############################################
# LETTURA DATI DA RUNTIME AGGREGATO
############################################


def resolve_runtime_dir(explicit_path: str | None) -> Path:
    path = Path(explicit_path).expanduser().resolve() if explicit_path else RUNTIME_DIR_DEFAULT

    if not path.exists():
        raise FileNotFoundError(f"Cartella runtime non trovata: {path}")

    required = [CLIENT_RUNTIME_LOG, MIDDLEBOX_RUNTIME_LOG, SERVER_RUNTIME_LOG]
    missing = [name for name in required if not (path / name).exists()]
    if missing:
        raise FileNotFoundError(f"File runtime mancanti in {path}: {', '.join(missing)}")

    return path


def read_text(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def split_experiment_sections(text: str) -> dict[int, str]:
    sections: dict[int, list[str]] = {}
    current_exp: int | None = None

    for line in text.splitlines():
        marker = EXP_MARKER_RE.match(line.strip())
        if marker:
            current_exp = int(marker.group(1))
            sections.setdefault(current_exp, [])
            continue

        if current_exp is not None:
            sections[current_exp].append(line)

    return {exp: "\n".join(lines) for exp, lines in sections.items()}


def split_operation_sections(text: str) -> dict[int, str]:
    sections: dict[int, list[str]] = {}
    current_op: int | None = None

    for line in text.splitlines():
        marker = OP_MARKER_RE.match(line.strip())
        if marker:
            current_op = int(marker.group(1))
            sections.setdefault(current_op, [])
            continue

        if current_op is not None:
            sections[current_op].append(line)

    return {op: "\n".join(lines) for op, lines in sections.items()}


def parse_ordered_times(text: str) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for line in text.splitlines():
        for match in TIMESTAMP_RE.finditer(line):
            out.append((int(match.group(1)), int(match.group(2))))
    return out


def latest_times_per_operation(text: str) -> dict[int, dict[int, int]]:
    # Solo operazioni 1-3
    operations: dict[int, dict[int, int]] = {op: {} for op in OPS}

    for op, op_text in split_operation_sections(text).items():
        times: dict[int, int] = {}
        for ts, value in parse_ordered_times(op_text):
            times[ts] = value
        if op in operations:
            operations[op] = times

    return operations


def reconstruct_operations(runtime_dir: Path) -> dict[int, dict[int, dict[int, int]]]:
    client_sections = split_experiment_sections(read_text(runtime_dir / CLIENT_RUNTIME_LOG))
    middlebox_sections = split_experiment_sections(read_text(runtime_dir / MIDDLEBOX_RUNTIME_LOG))
    server_sections = split_experiment_sections(read_text(runtime_dir / SERVER_RUNTIME_LOG))

    all_exp = sorted(set(client_sections) | set(middlebox_sections) | set(server_sections))
    reconstructed: dict[int, dict[int, dict[int, int]]] = {
        exp: {op: {} for op in OPS}
        for exp in all_exp
    }

    for exp in all_exp:
        client_ops = latest_times_per_operation(client_sections.get(exp, ""))
        middlebox_ops = latest_times_per_operation(middlebox_sections.get(exp, ""))
        server_ops = latest_times_per_operation(server_sections.get(exp, ""))

        for op in OPS:
            merged = {}
            merged.update(client_ops.get(op, {}))
            merged.update(middlebox_ops.get(op, {}))
            merged.update(server_ops.get(op, {}))
            reconstructed[exp][op] = merged

    return reconstructed


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


def metric_sort_key(metric: str) -> tuple[int, int, str]:
    if metric.startswith("t") and metric[1:].isdigit():
        return (0, int(metric[1:]), metric)
    if metric == TOTAL_METRIC:
        return (1, 0, metric)
    if metric in PARTIAL_DIFF_METRICS:
        return (2, PARTIAL_DIFF_METRICS.index(metric), metric)
    if metric in CONTAINER_METRICS:
        return (3, CONTAINER_METRICS.index(metric), metric)
    return (4, 0, metric)


def extract_pod_creation_metrics(runtime_dir: Path) -> dict[str, dict[int, list[tuple[int, int]]]]:
    """Estrae i tempi di creazione del pod middlebox dal log."""
    mb_log = read_text(runtime_dir / MIDDLEBOX_RUNTIME_LOG)

    creation_series: dict[int, list[tuple[int, int]]] = {op: [] for op in OPS}

    experiments = split_experiment_sections(mb_log)
    for exp, exp_text in experiments.items():
        creation_match = MB_POD_CREATION_RE.search(exp_text)
        if creation_match:
            creation_time_ns = int(creation_match.group(1))
            # Il tempo di creazione pod è associato all'operazione 1 (avviene una volta per esperimento)
            creation_series[1].append((exp, creation_time_ns))

    return {
        MB_POD_CREATION_TIME: creation_series,
    }


def parse_metric_series(runtime_dir: Path) -> dict[str, dict[int, list[tuple[int, int]]]]:
    series: dict[str, dict[int, list[tuple[int, int]]]] = {
        metric: {op: [] for op in OPS}
        for metric in ALL_METRICS
    }

    operations = reconstruct_operations(runtime_dir)

    for exp in sorted(operations):
        for op in OPS:
            times = operations[exp][op]
            diffs = compute_diffs(times)

            for ts in range(1, 29):
                if ts in times:
                    series[f"t{ts}"][op].append((exp, times[ts]))

            for ts in [37, 38, 39, 40]:
                if ts in times:
                    series[f"t{ts}"][op].append((exp, times[ts]))

            if TOTAL_METRIC in diffs:
                series[TOTAL_METRIC][op].append((exp, diffs[TOTAL_METRIC]))

            for metric in PARTIAL_DIFF_METRICS:
                if metric in diffs:
                    series[metric][op].append((exp, diffs[metric]))

    pod_metrics = extract_pod_creation_metrics(runtime_dir)
    for metric, data in pod_metrics.items():
        series[metric] = data

    return series


def write_grafici_txt(series: dict[str, dict[int, list[tuple[int, int]]]], runtime_dir: Path) -> None:
    BASE_DIR.mkdir(parents=True, exist_ok=True)

    with OUTPUT_TXT.open("w", encoding="utf-8") as handle:
        handle.write(f"Sorgente runtime log: {runtime_dir}\n")
        handle.write("Metriche: timestamp grezzi, t10 - t1 e differenze parziali (operazioni 1-3 Kubernetes)\n")
        handle.write("==============================\n")

        for metric in sorted(series, key=metric_sort_key):
            handle.write(f"METRICA {metric}:\n")
            for op in OPS:
                handle.write(f"OPERAZIONI N_{op}:\n")
                values = series[metric][op]
                if not values:
                    handle.write("(nessun dato)\n")
                else:
                    for exp, value in values:
                        handle.write(f"{exp}_{op} = {value} ns\n")
            handle.write("------------------------------\n")


def write_runtime_diagnostics(series: dict[str, dict[int, list[tuple[int, int]]]], runtime_dir: Path) -> None:
    BASE_DIR.mkdir(parents=True, exist_ok=True)

    client_log = read_text(runtime_dir / CLIENT_RUNTIME_LOG)
    conn_refused_count = client_log.lower().count("connection refused")
    request_error_count = client_log.lower().count("error during request")

    lines = [
        f"Sorgente runtime log: {runtime_dir}",
        "Diagnostica qualit\u00e0 dati timestamp",
        "==============================",
    ]

    total_complete = 0
    for op in OPS:
        n_t1 = len(series.get("t1", {}).get(op, []))
        n_t10 = len(series.get("t10", {}).get(op, []))
        n_total = len(series.get(TOTAL_METRIC, {}).get(op, []))
        total_complete += n_total
        lines.append(
            f"OPERAZIONE N_{op}: campioni t1={n_t1}, t10={n_t10}, {TOTAL_METRIC}={n_total}"
        )

    lines.append("------------------------------")
    lines.append(f"Client connection refused: {conn_refused_count}")
    lines.append(f"Client error during request: {request_error_count}")

    if total_complete == 0:
        lines.append(
            "ATTENZIONE: nessun campione completo t10 - t1 trovato. "
            "I grafici risultano vuoti perche' le richieste non completano il flusso end-to-end."
        )

    DIAGNOSTIC_TXT.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_timestamp_avg_for_explosion_by_operation(
    series: dict[str, dict[int, list[tuple[int, int]]]],
) -> dict[int, dict[str, float]]:
    out: dict[int, dict[str, float]] = {op: {} for op in OPS}
    for op in OPS:
        for ts in list(range(1, 29)) + [37, 38, 39, 40]:
            label = f"t{ts}"
            values = [value for _, value in series.get(label, {}).get(op, [])]
            if values:
                out[op][label] = float(statistics.fmean(values))
    return out


############################################
# PLOTTING
############################################


def make_plots(
    series: dict[str, dict[int, list[tuple[int, int]]]],
    *,
    show: bool,
    save_pdf: bool,
) -> None:
    def import_matplotlib():
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.backends.backend_pdf import PdfPages

        return plt, PdfPages

    def safe_run(command: list[str]) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(command, capture_output=True, text=True)
        except FileNotFoundError:
            return subprocess.CompletedProcess(command, returncode=127, stderr="command not found")

    def has_module(module_name: str) -> bool:
        try:
            importlib.import_module(module_name)
            return True
        except ImportError:
            return False

    def ensure_pip_available() -> bool:
        if has_module("pip"):
            return True
        ensurepip = safe_run([sys.executable, "-m", "ensurepip", "--user"])
        return ensurepip.returncode == 0 and has_module("pip")

    def ensure_matplotlib_installed() -> bool:
        try:
            import_matplotlib()
            return True
        except Exception:
            print("matplotlib non installato: provo installazione automatica...")

        install_attempts: list[subprocess.CompletedProcess[str]] = []

        if ensure_pip_available():
            install_attempts.append(
                safe_run([sys.executable, "-m", "pip", "install", "--user", "matplotlib"])
            )

        if shutil.which("pip3"):
            install_attempts.append(safe_run(["pip3", "install", "--user", "matplotlib"]))

        if any(result.returncode == 0 for result in install_attempts):
            try:
                import_matplotlib()
                print("matplotlib installato correttamente.")
                return True
            except Exception:
                print("matplotlib ancora non disponibile dopo installazione.")
                return False

        print("Installazione matplotlib fallita.")
        for result in install_attempts:
            if result.stderr:
                print(result.stderr.strip())
        return False

    if not ensure_matplotlib_installed():
        print("Creo solo Grafici.txt (senza PDF).")
        return

    plt, PdfPages = import_matplotlib()
    OUTPUT_PDF_DIR.mkdir(parents=True, exist_ok=True)

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

    def set_xticks_for_points(axis, sample_count: int) -> None:
        if sample_count <= 0:
            return

        tick_positions = [idx for idx in range(1, sample_count + 1) if idx % 10 == 0]
        if not tick_positions:
            tick_positions = [1, sample_count]

        axis.set_xticks(tick_positions)
        axis.set_xticklabels([str(value) for value in tick_positions])

    def draw_left_panel(axis, points: list[tuple[int, int]], *, title: str) -> None:
        if points:
            y_values_ms = points_to_ms(points)
            x_positions = list(range(1, len(y_values_ms) + 1))
            axis.scatter(x_positions, y_values_ms, color="tab:blue", alpha=0.8, s=24)

            avg_ms = sum(y_values_ms) / len(y_values_ms)
            median_ms = statistics.median(y_values_ms)

            axis.axhline(avg_ms, linestyle="--", color="tab:green", linewidth=1.5, label="Valore medio")
            axis.axhline(median_ms, linestyle="--", color="tab:red", linewidth=1.5, label="Mediana")

            axis.text(
                0.95,
                0.10,
                f"Media = {avg_ms:.3f} ms",
                color="tab:green",
                fontsize=10,
                fontweight="bold",
                ha="right",
                va="top",
                transform=axis.transAxes,
            )
            axis.text(
                0.42,
                0.10,
                f"Mediana = {median_ms:.3f} ms",
                color="tab:red",
                fontsize=10,
                fontweight="bold",
                ha="right",
                va="top",
                transform=axis.transAxes,
            )
            scale_axis(axis, y_values_ms)
            set_xticks_for_points(axis, len(y_values_ms))
        else:
            axis.text(0.5, 0.5, "Nessun dato", ha="center", va="center", transform=axis.transAxes)

        axis.set_title(title)
        axis.set_xlabel("Indice campione")
        axis.set_ylabel("Millisecondi")
        axis.grid(True, alpha=0.3)

    def draw_right_panel(axis, points: list[tuple[int, int]], *, title: str) -> None:
        if points:
            y_values_ms = points_to_ms(points)
            parts = axis.violinplot(
                [y_values_ms],
                positions=[1],
                widths=0.6,
                showmeans=False,
                showmedians=False,
                showextrema=True,
            )
            for body in parts["bodies"]:
                body.set_alpha(0.35)
                body.set_facecolor("tab:blue")
            axis.set_xlim(0.5, 1.5)
            axis.set_xticks([1])
            axis.set_xticklabels([f"N={len(y_values_ms)}"])
            scale_axis(axis, y_values_ms)
        else:
            axis.text(0.5, 0.5, "Nessun dato", ha="center", va="center", transform=axis.transAxes)

        axis.set_title(title)
        axis.set_xlabel("Distribuzione campioni")
        axis.set_ylabel("Millisecondi")
        axis.grid(True, alpha=0.3)

    def save_metric_pdf(metric: str, metric_series: dict[int, list[tuple[int, int]]]) -> bool:
        ops_to_plot = [op for op in OPS if operation_is_enabled_for_metric(metric, op)]
        if not ops_to_plot:
            return False

        if all(not metric_series.get(op, []) for op in ops_to_plot):
            if show:
                print(f"Salto PDF {metric}: nessun dato disponibile")
            return False

        safe_name = metric.replace(" ", "_").replace("-", "minus")
        pdf_path = OUTPUT_PDF_DIR / f"{safe_name}.pdf"

        with PdfPages(pdf_path) as pdf:
            cumulative_fig, cumulative_axes = plt.subplots(len(ops_to_plot), 2, figsize=(20, 4.5 * len(ops_to_plot)))
            if len(ops_to_plot) == 1:
                cumulative_axes = [cumulative_axes]
            cumulative_title = f"Distribuzione cumulata di {metric} per tipo operazione"

            for row, op in enumerate(ops_to_plot):
                left_axis = cumulative_axes[row][0]
                right_axis = cumulative_axes[row][1]
                points = metric_series.get(op, [])
                draw_left_panel(left_axis, points, title=f"Operazione N_{op} - valori")
                draw_right_panel(right_axis, points, title=f"Operazione N_{op} - violino")

            cumulative_fig.suptitle(cumulative_title, fontsize=13)
            cumulative_fig.tight_layout(rect=[0, 0.02, 1, 0.97])
            pdf.savefig(cumulative_fig)
            plt.close(cumulative_fig)

            for op in ops_to_plot:
                detail_fig, detail_axes = plt.subplots(1, 2, figsize=(16, 4.5))
                points = metric_series.get(op, [])
                detail_title = f"Distribuzione di {metric} - operazione N_{op}"
                draw_left_panel(detail_axes[0], points, title=f"{metric} - valori")
                draw_right_panel(detail_axes[1], points, title=f"{metric} - violino")
                detail_fig.suptitle(detail_title, fontsize=13)
                detail_fig.tight_layout(rect=[0, 0.02, 1, 0.95])
                pdf.savefig(detail_fig)
                plt.close(detail_fig)

        if show:
            print(f"Creato PDF: {pdf_path}")
        return True

    generated_count = 0
    for metric in sorted(series, key=metric_sort_key):
        if save_pdf and should_generate_metric_pdf(metric):
            if save_metric_pdf(metric, series[metric]):
                generated_count += 1

    if show:
        print(f"PDF generati con dati: {generated_count}")

    if show:
        plt.show()
    else:
        plt.close("all")


############################################
# CLI
############################################


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Grafici da runtime log in ~/Desktop/MisureKubernetes/Runtime"
    )
    parser.add_argument(
        "--runtime-dir",
        help="Percorso cartella runtime (default: ~/Desktop/MisureKubernetes/Runtime)",
    )
    parser.add_argument(
        "--show",
        action="store_true",
        help="Mostra i grafici a video (se ambiente grafico disponibile)",
    )
    parser.add_argument(
        "--no-pdf",
        action="store_true",
        help="Non salvare i PDF in ~/Desktop/MisureKubernetes/Analisi",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    runtime_dir = resolve_runtime_dir(args.runtime_dir)

    series = parse_metric_series(runtime_dir)
    write_grafici_txt(series, runtime_dir)
    write_runtime_diagnostics(series, runtime_dir)
    make_plots(series, show=args.show, save_pdf=not args.no_pdf)

    explosion_by_op = build_timestamp_avg_for_explosion_by_operation(series)
    for op in OPS:
        render_t1_t10_explosion_chart(
            timestamp_avg_ns=explosion_by_op.get(op, {}),
            output_path=OUTPUT_PDF_DIR / f"T1_T10_Explosion_Operazione_{op}.png",
            title=f"Esplosione temporale t1->t10 - Operazione N_{op}",
            alpha=EXPLOSION_ALPHA,
        )

    print(f"File riepilogo: {OUTPUT_TXT}")
    print(f"Diagnostica runtime: {DIAGNOSTIC_TXT}")
    if not args.no_pdf:
        print(f"Grafici PDF: {OUTPUT_PDF_DIR}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
