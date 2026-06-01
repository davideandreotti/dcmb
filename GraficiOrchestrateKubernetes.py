from __future__ import annotations

import statistics
import re
from dataclasses import dataclass
from pathlib import Path

import matplotlib.pyplot as plt

from grafici_t1_t10_explosion import render_t1_t10_explosion_chart

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR if (SCRIPT_DIR / "DC").exists() else SCRIPT_DIR.parent

BASE_DIR = PROJECT_ROOT / "MisureOrchestrateKubernetes"
RUNTIME_DIR = BASE_DIR / "Runtime"
ANALYSIS_DIR = BASE_DIR / "Analisi"

CLIENT_LOG = RUNTIME_DIR / "Client.log"
MIDDLEBOX_LOG = RUNTIME_DIR / "Middlebox.log"
SERVER_LOG = RUNTIME_DIR / "Server.log"
SUMMARY_OUT = ANALYSIS_DIR / "GraficiOrchestrateKubernetes.txt"
ORCH_SUMMARY = ANALYSIS_DIR / "OrchestrateKubernetesSummary.txt"
EXPLOSION_ALPHA = 0.25


@dataclass(frozen=True)
class SeriesSpec:
    name: str
    start_source: Path
    end_source: Path
    start_tag: str
    end_tag: str
    png_name: str


TIMESTAMP_RE = re.compile(r"\b(t\d+):\s*\[[^\]]+\].*?=\s*(\d+)\s*ns")


def read_text(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="ignore")


def parse_tag_series(text: str, tag: str) -> list[int]:
    values: list[int] = []
    for found_tag, value in TIMESTAMP_RE.findall(text):
        if found_tag == tag:
            values.append(int(value))
    return values


def paired_deltas(start_values: list[int], end_values: list[int]) -> list[int]:
    deltas: list[int] = []
    end_idx = 0
    for start in start_values:
        while end_idx < len(end_values) and end_values[end_idx] < start:
            end_idx += 1
        if end_idx >= len(end_values):
            break
        deltas.append(end_values[end_idx] - start)
        end_idx += 1
    return deltas


def ns_to_ms(values: list[int]) -> list[float]:
    return [value / 1_000_000.0 for value in values]


def plot_series(name: str, values_ns: list[int], output_path: Path) -> None:
    plt.figure(figsize=(11, 5.5))
    if values_ns:
        x = list(range(1, len(values_ns) + 1))
        y_ms = ns_to_ms(values_ns)
        avg_ms = statistics.fmean(y_ms)
        med_ms = statistics.median(y_ms)
        plt.plot(x, y_ms, marker="o", linestyle="-", linewidth=1.5, color="tab:blue", label=name)
        plt.axhline(avg_ms, color="tab:green", linestyle="--", linewidth=1.4, label=f"media {avg_ms:.3f} ms")
        plt.axhline(med_ms, color="tab:red", linestyle=":", linewidth=1.6, label=f"mediana {med_ms:.3f} ms")
        plt.legend()
    else:
        plt.text(0.5, 0.5, "n/d (campioni assenti)", ha="center", va="center", fontsize=12)
        plt.xlim(0, 1)
        plt.ylim(0, 1)
    plt.xlabel("Esperimento")
    plt.ylabel("Tempo (ms)")
    plt.title(name)
    plt.grid(True, alpha=0.25)
    plt.tight_layout()
    plt.savefig(output_path, dpi=150)
    plt.close()


def detect_operator_mode() -> str:
    text = read_text(ORCH_SUMMARY)
    match = re.search(r"Operator mode:\s*(sgx|nosgx)", text, flags=re.IGNORECASE)
    if not match:
        return "unknown"
    mode = match.group(1).lower()
    if mode == "sgx":
        return "SGX"
    if mode == "nosgx":
        return "NO-SGX"
    return mode.upper()


def write_summary(lines: list[str]) -> None:
    ANALYSIS_DIR.mkdir(parents=True, exist_ok=True)
    SUMMARY_OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_timestamp_avg_from_logs(
    text_map: dict[Path, str],
) -> dict[str, float]:
    # Seleziona, per ogni timestamp disponibile, la sorgente con più campioni.
    all_tags: set[str] = set()
    for source in (CLIENT_LOG, MIDDLEBOX_LOG, SERVER_LOG):
        all_tags.update(tag for tag, _ in TIMESTAMP_RE.findall(text_map[source]))

    tag_best_series: dict[str, list[int]] = {}
    for tag in sorted(all_tags, key=lambda name: int(name[1:])):
        best: list[int] = []
        for source in (CLIENT_LOG, MIDDLEBOX_LOG, SERVER_LOG):
            values = parse_tag_series(text_map[source], tag)
            if len(values) > len(best):
                best = values
        tag_best_series[tag] = best

    out: dict[str, float] = {}
    for tag in sorted(tag_best_series, key=lambda name: int(name[1:])):
        values = tag_best_series[tag]
        if values:
            out[tag] = float(statistics.fmean(values))
    return out


def main() -> None:
    ANALYSIS_DIR.mkdir(parents=True, exist_ok=True)

    client_text = read_text(CLIENT_LOG)
    middlebox_text = read_text(MIDDLEBOX_LOG)
    server_text = read_text(SERVER_LOG)

    mode_label = detect_operator_mode()

    specs = [
        SeriesSpec(
            name="t10 - t1 | End-to-end",
            start_source=CLIENT_LOG,
            end_source=CLIENT_LOG,
            start_tag="t1",
            end_tag="t10",
            png_name="K8s_t10_minus_t1.png",
        ),
        SeriesSpec(
            name="t21 - t20 | TLS client handshake",
            start_source=CLIENT_LOG,
            end_source=CLIENT_LOG,
            start_tag="t20",
            end_tag="t21",
            png_name="K8s_t21_minus_t20_tls_client.png",
        ),
        SeriesSpec(
            name="t10 - t21 | App processing",
            start_source=CLIENT_LOG,
            end_source=CLIENT_LOG,
            start_tag="t21",
            end_tag="t10",
            png_name="K8s_t10_minus_t21_app_time.png",
        ),
        SeriesSpec(
            name="t24 - t23 | Cert validation",
            start_source=CLIENT_LOG,
            end_source=CLIENT_LOG,
            start_tag="t23",
            end_tag="t24",
            png_name="K8s_t24_minus_t23_cert_validation.png",
        ),
        SeriesSpec(
            name="t38 - t37 | XCode validation",
            start_source=MIDDLEBOX_LOG,
            end_source=MIDDLEBOX_LOG,
            start_tag="t37",
            end_tag="t38",
            png_name="K8s_t38_minus_t37_xcode_validation.png",
        ),
        SeriesSpec(
            name="t25 - t1 | Client -> Gateway",
            start_source=CLIENT_LOG,
            end_source=MIDDLEBOX_LOG,
            start_tag="t1",
            end_tag="t25",
            png_name="K8s_t25_minus_t1_client_gateway.png",
        ),
        SeriesSpec(
            name="t27 - t26 | Operator selection",
            start_source=MIDDLEBOX_LOG,
            end_source=MIDDLEBOX_LOG,
            start_tag="t26",
            end_tag="t27",
            png_name="K8s_t27_minus_t26_operator_selection.png",
        ),
        SeriesSpec(
            name="t28 - t27 | Scelta pod -> inoltro gateway",
            start_source=MIDDLEBOX_LOG,
            end_source=MIDDLEBOX_LOG,
            start_tag="t27",
            end_tag="t28",
            png_name="K8s_t28_minus_t27_gateway_select_forward.png",
        ),
        SeriesSpec(
            name="t29 - t28 | Gateway -> Operator",
            start_source=MIDDLEBOX_LOG,
            end_source=MIDDLEBOX_LOG,
            start_tag="t28",
            end_tag="t29",
            png_name="K8s_t29_minus_t28_gateway_operator.png",
        ),
        SeriesSpec(
            name="t4 - t3 | Operator -> Server",
            start_source=MIDDLEBOX_LOG,
            end_source=SERVER_LOG,
            start_tag="t3",
            end_tag="t4",
            png_name="K8s_t4_minus_t3_operator_server.png",
        ),
        SeriesSpec(
            name="t6 - t5 | Cert generation server",
            start_source=SERVER_LOG,
            end_source=SERVER_LOG,
            start_tag="t5",
            end_tag="t6",
            png_name="K8s_t6_minus_t5_server_cert_gen.png",
        ),
        SeriesSpec(
            name="t33 - t30 | Miss decision",
            start_source=CLIENT_LOG,
            end_source=CLIENT_LOG,
            start_tag="t30",
            end_tag="t33",
            png_name="K8s_t33_minus_t30_miss_decision.png",
        ),
    ]

    summary_lines = [
        "GraficiOrchestrateKubernetes",
        "==========================",
        f"Client log: {CLIENT_LOG}",
        f"Middlebox log: {MIDDLEBOX_LOG}",
        f"Server log: {SERVER_LOG}",
        "",
    ]

    text_map = {
        CLIENT_LOG: client_text,
        MIDDLEBOX_LOG: middlebox_text,
        SERVER_LOG: server_text,
    }

    for spec in specs:
        start_series = parse_tag_series(text_map[spec.start_source], spec.start_tag)
        end_series = parse_tag_series(text_map[spec.end_source], spec.end_tag)
        deltas_ns = paired_deltas(start_series, end_series)

        summary_lines.append(f"{spec.name}: campioni={len(deltas_ns)}")
        if not deltas_ns:
            summary_lines.append("  n/d")
            summary_lines.append("")
            continue

        summary_lines.append(f"  media={round(statistics.fmean(deltas_ns))} ns")
        summary_lines.append(f"  mediana={round(statistics.median(deltas_ns))} ns")
        summary_lines.append(f"  min={min(deltas_ns)} ns")
        summary_lines.append(f"  max={max(deltas_ns)} ns")
        summary_lines.append("")

        out_png = ANALYSIS_DIR / spec.png_name
        titled_name = f"{spec.name} [{mode_label}]"
        plot_series(titled_name, deltas_ns, out_png)

    # Genera comunque i grafici anche quando campioni assenti.
    for spec in specs:
        out_png = ANALYSIS_DIR / spec.png_name
        if not out_png.exists():
            titled_name = f"{spec.name} [{mode_label}]"
            plot_series(titled_name, [], out_png)

    explosion_avg = build_timestamp_avg_from_logs(text_map)
    render_t1_t10_explosion_chart(
        timestamp_avg_ns=explosion_avg,
        output_path=ANALYSIS_DIR / "T1_T10_Explosion.png",
        title=f"Esplosione temporale t1->t10 [{mode_label}]",
        alpha=EXPLOSION_ALPHA,
    )

    write_summary(summary_lines)
    print(f"Summary scritto in: {SUMMARY_OUT}")
    print(f"Grafici PNG in: {ANALYSIS_DIR}")


if __name__ == "__main__":
    main()
