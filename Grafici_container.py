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

DEFAULT_LOG_FILE = PROJECT_ROOT / "Misure" / "Log_container.txt"
OUTPUT_DIR = PROJECT_ROOT / "Misure" / "Analisi"
EXPLOSION_ALPHA = 0.25

EXP_MARKER_RE = re.compile(r"^===== ESPERIMENTO (\d+) =====$")
EVENT_RE = re.compile(r"^(client|middlebox|server) (START|END) = (\d+) ns$")

CONTAINERS = ["client", "middlebox", "server"]


############################################
# PARSING
############################################


def read_text(path: Path) -> str:
    if not path.exists():
        raise FileNotFoundError(f"File non trovato: {path}")
    return path.read_text(encoding="utf-8", errors="replace")


def parse_container_times(path: Path) -> dict[str, list[tuple[int, int]]]:
    text = read_text(path)
    current_exp: int | None = None

    starts: dict[tuple[str, int], int] = {}
    durations: dict[str, list[tuple[int, int]]] = {name: [] for name in CONTAINERS}

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        exp_marker = EXP_MARKER_RE.match(line)
        if exp_marker:
            current_exp = int(exp_marker.group(1))
            continue

        event = EVENT_RE.match(line)
        if not event or current_exp is None:
            continue

        container = event.group(1)
        phase = event.group(2)
        timestamp_ns = int(event.group(3))
        key = (container, current_exp)

        if phase == "START":
            starts[key] = timestamp_ns
            continue

        if phase == "END" and key in starts:
            durations[container].append((current_exp, timestamp_ns - starts[key]))

    for container in CONTAINERS:
        durations[container] = sorted(durations[container], key=lambda item: item[0])

    return durations


############################################
# PLOTTING
############################################


def make_plots(durations: dict[str, list[tuple[int, int]]], *, show: bool) -> None:
    TITLE_TEMPLATE = "Tempo creazione container: {container}"
    X_LABEL = "Esperimento N"
    Y_LABEL = "Millisecondi"

    AVERAGE_LINE_STYLE = "--"
    AVERAGE_LINE_COLOR = "tab:green"
    AVERAGE_LINE_WIDTH = 1.5
    MEDIAN_LINE_STYLE = "--"
    MEDIAN_LINE_COLOR = "tab:red"
    MEDIAN_LINE_WIDTH = 1.5
    AVERAGE_LABEL_X = 0.95
    AVERAGE_LABEL_Y = 0.10
    MEDIAN_LABEL_X = 0.42
    MEDIAN_LABEL_Y = 0.10

    FIGSIZE = (14, 6)

    def import_plt():
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        return plt

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
            import_plt()
            return True
        except ImportError:
            print("matplotlib non installato: provo installazione automatica...")

        install_attempts: list[subprocess.CompletedProcess[str]] = []

        if ensure_pip_available():
            install_attempts.append(
                safe_run([sys.executable, "-m", "pip", "install", "--user", "matplotlib"])
            )

        if shutil.which("pip3"):
            install_attempts.append(safe_run(["pip3", "install", "--user", "matplotlib"]))

        install_ok = any(result.returncode == 0 for result in install_attempts)
        if not install_ok:
            print("Installazione matplotlib fallita.")
            return False

        try:
            import_plt()
            return True
        except ImportError:
            return False

    if not ensure_matplotlib_installed():
        print("Nessun grafico creato (matplotlib non disponibile).")
        return

    plt = import_plt()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    def ns_to_ms(value_ns: int) -> float:
        return value_ns / 1_000_000.0

    for container in CONTAINERS:
        points = durations.get(container, [])
        figure, axis = plt.subplots(1, 1, figsize=FIGSIZE)

        if points:
            experiments = [exp for exp, _ in points]
            values_ms = [ns_to_ms(duration_ns) for _, duration_ns in points]

            axis.plot(experiments, values_ms, marker="o", linewidth=2)

            avg_ms = sum(values_ms) / len(values_ms)
            median_ms = statistics.median(values_ms)

            axis.axhline(
                y=avg_ms,
                linestyle=AVERAGE_LINE_STYLE,
                color=AVERAGE_LINE_COLOR,
                linewidth=AVERAGE_LINE_WIDTH,
                label="Valore medio",
            )
            axis.axhline(
                y=median_ms,
                linestyle=MEDIAN_LINE_STYLE,
                color=MEDIAN_LINE_COLOR,
                linewidth=MEDIAN_LINE_WIDTH,
                label="Mediana",
            )

            observed_max = max(values_ms)
            upper = observed_max * 1.10 if observed_max > 0 else 1.0
            axis.set_ylim(0.0, upper)

            axis.text(
                AVERAGE_LABEL_X,
                AVERAGE_LABEL_Y,
                f"Media = {avg_ms:.3f} ms",
                color=AVERAGE_LINE_COLOR,
                fontsize=10,
                fontweight="bold",
                ha="right",
                va="top",
                transform=axis.transAxes,
            )
            axis.text(
                MEDIAN_LABEL_X,
                MEDIAN_LABEL_Y,
                f"Mediana = {median_ms:.3f} ms",
                color=MEDIAN_LINE_COLOR,
                fontsize=10,
                fontweight="bold",
                ha="right",
                va="top",
                transform=axis.transAxes,
            )
            
            axis.legend(loc="best")
        else:
            axis.text(0.5, 0.5, "Nessun dato", ha="center", va="center", transform=axis.transAxes)

        axis.set_title(TITLE_TEMPLATE.format(container=container))
        axis.set_xlabel(X_LABEL)
        axis.set_ylabel(Y_LABEL)
        axis.grid(True, alpha=0.3)

        out_name = f"ContainerCreation_{container}.png"
        figure.tight_layout()
        figure.savefig(OUTPUT_DIR / out_name, dpi=140)

        if show:
            figure.show()
        else:
            plt.close(figure)


############################################
# CLI
############################################


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Genera 3 grafici (client/middlebox/server) da Log_container.txt"
    )
    parser.add_argument(
        "log_file",
        nargs="?",
        default=str(DEFAULT_LOG_FILE),
        help="Percorso del file Log_container.txt",
    )
    parser.add_argument(
        "--show",
        action="store_true",
        help="Mostra i grafici a video (se ambiente grafico disponibile)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    log_file = Path(args.log_file).expanduser().resolve()

    durations = parse_container_times(log_file)
    make_plots(durations, show=args.show)

    # Questo script non contiene i timestamp t1..t10: genera comunque il pannello finale
    # per mantenere un output uniforme tra tutti gli script di grafici.
    render_t1_t10_explosion_chart(
        timestamp_avg_ns={},
        output_path=OUTPUT_DIR / "T1_T10_Explosion.png",
        title="Esplosione temporale t1->t10 (non disponibile in Log_container)",
        alpha=EXPLOSION_ALPHA,
    )

    print(f"Grafici salvati in: {OUTPUT_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
