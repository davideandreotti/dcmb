from __future__ import annotations


import re
import statistics
from dataclasses import dataclass
from pathlib import Path

from grafici_t1_t10_explosion import render_t1_t10_explosion_chart


############################################
# CONFIGURAZIONE
############################################

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR if (SCRIPT_DIR / "DC").exists() else SCRIPT_DIR.parent
DEFAULT_BASE_DIR = PROJECT_ROOT / "Misure"

INPUT_DIR = DEFAULT_BASE_DIR / "Orchestrate"
OUTPUT_DIR = INPUT_DIR

CLIENT_LOG = INPUT_DIR / "ClientLog.txt"
MIDDLEBOX_LOG = INPUT_DIR / "MiddleboxLog.txt"
SERVER_LOG = INPUT_DIR / "ServerLog.txt"
OPERATOR_LOG = INPUT_DIR / "OperatorLog.txt"

SUMMARY_FILE = OUTPUT_DIR / "GraficiOrchestrate.txt"

X_TICK_EVERY = 5
SAVE_PNG = True
SHOW_PLOTS = False

FIGURE_SIZE = (12, 6)
POINT_COLOR = "tab:blue"
AVERAGE_COLOR = "tab:green"
MEDIAN_COLOR = "tab:red"
EXPLOSION_ALPHA = 0.25


############################################
# CONFIG RUNTIME
############################################


@dataclass(frozen=True)
class PlotConfig:
	input_dir: Path
	output_dir: Path
	client_log: Path
	middlebox_log: Path
	server_log: Path
	operator_log: Path
	summary_file: Path
	x_tick_every: int
	save_png: bool
	show_plots: bool
	figure_size: tuple[int, int]
	point_color: str
	average_color: str
	median_color: str


def build_config() -> PlotConfig:
	candidate_dirs = [
		DEFAULT_BASE_DIR / "Orchestrate",
		DEFAULT_BASE_DIR / "Runtime",
		SCRIPT_DIR / "Misure" / "Orchestrate",
		SCRIPT_DIR / "Misure" / "Runtime",
	]

	def resolve_log_path(directory: Path, txt_name: str, runtime_name: str) -> Path:
		txt_path = directory / txt_name
		runtime_path = directory / runtime_name
		if txt_path.exists():
			return txt_path
		if runtime_path.exists():
			return runtime_path
		return txt_path

	def score_directory(directory: Path) -> int:
		score = 0
		for txt_name, runtime_name in (
			("ClientLog.txt", "Client.log"),
			("MiddleboxLog.txt", "Middlebox.log"),
			("ServerLog.txt", "Server.log"),
			("OperatorLog.txt", "Operator.log"),
		):
			if (directory / txt_name).exists() or (directory / runtime_name).exists():
				score += 1
		return score

	scored = sorted(((score_directory(path), path) for path in candidate_dirs), reverse=True)
	best_score, best_dir = scored[0] if scored else (0, INPUT_DIR)

	input_dir = best_dir if best_score > 0 else INPUT_DIR
	output_dir = input_dir
	client_log = resolve_log_path(input_dir, "ClientLog.txt", "Client.log")
	middlebox_log = resolve_log_path(input_dir, "MiddleboxLog.txt", "Middlebox.log")
	server_log = resolve_log_path(input_dir, "ServerLog.txt", "Server.log")
	operator_log = resolve_log_path(input_dir, "OperatorLog.txt", "Operator.log")

	return PlotConfig(
		input_dir=input_dir,
		output_dir=output_dir,
		client_log=client_log,
		middlebox_log=middlebox_log,
		server_log=server_log,
		operator_log=operator_log,
		summary_file=output_dir / "GraficiOrchestrate.txt",
		x_tick_every=X_TICK_EVERY,
		save_png=SAVE_PNG,
		show_plots=SHOW_PLOTS,
		figure_size=FIGURE_SIZE,
		point_color=POINT_COLOR,
		average_color=AVERAGE_COLOR,
		median_color=MEDIAN_COLOR,
	)


OPERATION_MARKER_RE = re.compile(r"OPERAZIONE\s+(\d+)")
EXPERIMENT_MARKER_RE = re.compile(r"=====\s*ESPERIMENTO\s+(\d+)\s*=====")
# Parse only raw timestamp lines like:
#   t31: [ORCH] - request_dispatched = 1776679903794636082 ns
# and ignore derived delta lines like:
#   t31 - t30 = 807475 ns
TIMESTAMP_RE = re.compile(r"\b(t(?:_op)?\d+)\b\s*:\s*[^\n\r]*?=\s*(\d+)\b")
PRECOMPUTED_T10_T1_RE = re.compile(r"\bt10\s*-\s*t1\s*=\s*(-?\d+)\s*ns\b", re.IGNORECASE)
CLIENT_ID_RE = re.compile(r"as client_id:\s*([^\s]+)")


@dataclass(frozen=True)
class Metric:
	key: str
	start: str
	end: str
	title: str


@dataclass(frozen=True)
class MetricSeries:
	metric: Metric
	points: list[tuple[int, int]]
	missing_operations: list[int]


def extract_precomputed_t10_t1(section_text: str) -> int | None:
	last_value: int | None = None
	for line in section_text.splitlines():
		match = PRECOMPUTED_T10_T1_RE.search(line)
		if match:
			last_value = int(match.group(1))
	return last_value


def metric_key(start: str, end: str) -> str:
	return f"{end}_minus_{start}"


def metric_title(start: str, end: str, description: str) -> str:
	return f"{end} - {start} | {description}"


METRICS = [
	Metric(metric_key("t1", "t10"), "t1", "t10", "t10 - t1 | TOTALE: Tempo end-to-end completo dal ClientHello al ricevimento della risposta"),
	Metric(metric_key("t20", "t21"), "t20", "t21", "t21 - t20 | TLS CLIENT: Tempo TLS handshake dal client (TLSHandshakeStart→Done)"),
	Metric(metric_key("t21", "t10"), "t21", "t10", "t10 - t21 | APP TIME: Tempo di processing della richiesta application-side"),
	Metric(metric_key("t23", "t24"), "t23", "t24", "t24 - t23 | CERT VALIDATION: Tempo di validazione certificato da parte del client"),
	Metric(metric_key("t5", "t6"), "t5", "t6", "t6 - t5 | GEN CERT SERVER: Tempo di generazione certificati delegati al server"),
	Metric(metric_key("t37", "t38"), "t37", "t38", "t38 - t37 | XCODE VALIDATION: Tempo di validazione XCode nel middlebox"),
	Metric(metric_key("t26", "t27"), "t26", "t27", "t27 - t26 | OPERATOR SELECTION: Tempo per selezionare un operator pronto dal pool"),
	Metric(metric_key("t28", "t29"), "t28", "t29", "t29 - t28 | GATEWAY→OPERATOR: Latenza tra gateway e operator selezionato"),
	Metric(metric_key("t30", "t33"), "t30", "t33", "t33 - t30 | MISS DECISION: Tempo di decisione miss (se precedente richiesta ancora in volo)"),
]


def normalize_timestamp_name(name: str) -> str:
	if name.startswith("t_op"):
		return "top" + name[4:]
	return name


def extract_timestamp_events(text: str) -> list[tuple[str, int]]:
	events: list[tuple[str, int]] = []
	for line in text.splitlines():
		for match in TIMESTAMP_RE.finditer(line):
			name = normalize_timestamp_name(match.group(1))
			value = int(match.group(2))
			events.append((name, value))
	return events


def group_messages_by_anchor(
	events: list[tuple[str, int]],
	anchor: str,
) -> list[dict[str, int]]:
	groups: list[dict[str, int]] = []
	current: dict[str, int] | None = None

	for name, value in events:
		if name == anchor:
			if current is not None and anchor in current:
				groups.append(current)
			current = {anchor: value}
			continue

		if current is None:
			continue

		current[name] = value

	if current is not None and anchor in current:
		groups.append(current)

	return groups


def group_messages_with_best_anchor(
	text: str,
	anchors: list[str],
) -> list[dict[str, int]]:
	events = extract_timestamp_events(text)
	best_groups: list[dict[str, int]] = []
	best_score = -1

	for anchor in anchors:
		groups = group_messages_by_anchor(events, anchor)
		if not groups:
			continue
		# Prefer grouping with more messages; tie-break with more avg fields per message.
		avg_fields = sum(len(group) for group in groups) / len(groups)
		score = len(groups) * 1000 + int(avg_fields * 10)
		if score > best_score:
			best_score = score
			best_groups = groups

	return best_groups


def split_experiments(text: str) -> dict[int, str]:
	sections: dict[int, list[str]] = {}
	current_experiment: int | None = None

	for line in text.splitlines():
		match = EXPERIMENT_MARKER_RE.search(line)
		if match:
			current_experiment = int(match.group(1))
			sections.setdefault(current_experiment, [])
			continue

		if current_experiment is not None:
			sections[current_experiment].append(line)

	return {experiment: "\n".join(lines) for experiment, lines in sections.items()}


def split_operations(text: str) -> dict[int, str]:
	sections: dict[int, list[str]] = {}
	current_operation: int | None = None

	for line in text.splitlines():
		match = OPERATION_MARKER_RE.search(line)
		if match:
			current_operation = int(match.group(1))
			sections.setdefault(current_operation, [])
			continue

		if current_operation is not None:
			sections[current_operation].append(line)

	return {operation: "\n".join(lines) for operation, lines in sections.items()}


def extract_latest_timestamps(section_text: str) -> dict[str, int]:
	timestamps: dict[str, int] = {}

	for line in section_text.splitlines():
		for match in TIMESTAMP_RE.finditer(line):
			name = normalize_timestamp_name(match.group(1))
			value = int(match.group(2))
			timestamps[name] = value

	return timestamps




############################################
# OUTPUT TESTUALE
############################################


def ns_to_ms(value_ns: int) -> float:
	return value_ns / 1_000_000.0


def build_timestamp_avg_from_merged(merged_operations: dict[int, dict[str, int]]) -> dict[str, float]:
	buckets: dict[str, list[int]] = {}
	for _, timestamps in sorted(merged_operations.items()):
		for tag, value in timestamps.items():
			if not (len(tag) >= 2 and tag[0] == "t" and tag[1:].isdigit()):
				continue
			buckets.setdefault(tag, []).append(value)

	out: dict[str, float] = {}
	for tag in sorted(buckets, key=lambda name: int(name[1:])):
		values = buckets[tag]
		if values:
			out[tag] = float(statistics.fmean(values))
	return out


class OrchestratedPlotter:
	def __init__(self, config: PlotConfig):
		self.config = config

	def ensure_input_logs_exist(self) -> list[Path]:
		return [
			path
			for path in (
				self.config.client_log,
				self.config.middlebox_log,
				self.config.server_log,
				self.config.operator_log,
			)
			if not path.exists()
		]

	def read_text(self, path: Path) -> str:
		return path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""

	def split_operations(self, text: str) -> dict[int, str]:
		sections: dict[int, list[str]] = {}
		current_operation: int | None = None

		for line in text.splitlines():
			match = OPERATION_MARKER_RE.search(line)
			if match:
				current_operation = int(match.group(1))
				sections.setdefault(current_operation, [])
				continue

			if current_operation is not None:
				sections[current_operation].append(line)

		return {operation: "\n".join(lines) for operation, lines in sections.items()}

	def extract_latest_timestamps(self, section_text: str) -> dict[str, int]:
		timestamps: dict[str, int] = {}
		for line in section_text.splitlines():
			for match in TIMESTAMP_RE.finditer(line):
				name = normalize_timestamp_name(match.group(1))
				value = int(match.group(2))
				timestamps[name] = value
		return timestamps

	def detect_client_label(self, client_text: str) -> str:
		match = CLIENT_ID_RE.search(client_text)
		return match.group(1) if match else "client"

	def merge_operations(self) -> tuple[dict[int, dict[str, int]], str]:
		client_text = self.read_text(self.config.client_log)
		middlebox_text = self.read_text(self.config.middlebox_log)
		server_text = self.read_text(self.config.server_log)
		operator_text = self.read_text(self.config.operator_log)

		# Build per-message groups directly from repeated timestamp anchors.
		# This avoids depending on experiment markers that may be missing or sparse.
		client_groups = group_messages_with_best_anchor(client_text, ["t1"])
		middlebox_groups = group_messages_with_best_anchor(middlebox_text, ["t25", "t26", "t2"])
		server_groups = group_messages_with_best_anchor(server_text, ["t4", "t5"])
		operator_groups = group_messages_with_best_anchor(operator_text, ["t29", "t2"])

		message_count = max(
			len(client_groups),
			len(middlebox_groups),
			len(server_groups),
			len(operator_groups),
		)
		merged: dict[int, dict[str, int]] = {}

		for index in range(message_count):
			timestamps: dict[str, int] = {}
			if index < len(middlebox_groups):
				timestamps.update(middlebox_groups[index])
			if index < len(operator_groups):
				timestamps.update(operator_groups[index])
			if index < len(server_groups):
				timestamps.update(server_groups[index])
			if index < len(client_groups):
				timestamps.update(client_groups[index])

			merged[index + 1] = timestamps

		return merged, self.detect_client_label(client_text)

	def build_metric_series(self, merged_operations: dict[int, dict[str, int]]) -> dict[str, MetricSeries]:
		series: dict[str, MetricSeries] = {}
		all_messages = sorted(merged_operations)

		for metric in METRICS:
			points: list[tuple[int, int]] = []
			missing_operations: list[int] = []

			for message_idx in all_messages:
				timestamps = merged_operations[message_idx]
				if metric.start == "t1" and metric.end == "t10":
					precomputed = timestamps.get("t10_minus_t1_precomputed")
					if isinstance(precomputed, int):
						points.append((message_idx, precomputed))
						continue

				if metric.start in timestamps and metric.end in timestamps:
					points.append((message_idx, timestamps[metric.end] - timestamps[metric.start]))
				else:
					missing_operations.append(message_idx)

			series[metric.key] = MetricSeries(metric, points, missing_operations)

		return series

	def write_summary(self, series: dict[str, MetricSeries], client_label: str) -> None:
		self.config.output_dir.mkdir(parents=True, exist_ok=True)

		with self.config.summary_file.open("w", encoding="utf-8") as handle:
			handle.write("RIEPILOGO METRICHE - MISURE ORCHESTRATE\n")
			handle.write("=" * 60 + "\n\n")
			
			handle.write("SPIEGAZIONE TIMESTAMP CHIAVE:\n")
			handle.write("-" * 60 + "\n")
			handle.write("t1:   ClientHello inviato dal client\n")
			handle.write("t10:  ServerHello ricevuto dal client\n")
			handle.write("t20:  TLSHandshakeStart (client)\n")
			handle.write("t21:  TLSHandshakeDone (client)\n")
			handle.write("t22:  CertValidationStart (client)\n")
			handle.write("t23:  CertValidationStart richiesto (client)\n")
			handle.write("t24:  CertValidationDone (client)\n")
			handle.write("t5:   ClientHello arriva al server\n")
			handle.write("t6:   Generazione certificati delegati completata (server)\n")
			handle.write("t26:  Operator selection inizio (gateway)\n")
			handle.write("t27:  Operator selected (gateway)\n")
			handle.write("t28:  Gateway→Operator send\n")
			handle.write("t29:  Gateway→Operator latency done\n")
			handle.write("t30:  Pacing tick reached (orchestrator)\n")
			handle.write("t31:  Request dispatched (orchestrator)\n")
			handle.write("t32:  Request completed (orchestrator)\n")
			handle.write("t33:  Miss decision (orchestrator)\n")
			handle.write("\n")
			handle.write(f"Input directory: {self.config.input_dir}\n")
			handle.write(f"Client: {client_label}\n")
			handle.write(f"X tick every: {self.config.x_tick_every}\n")
			handle.write(f"Metriche totali: {len(METRICS)}\n")
			handle.write("=" * 60 + "\n\n")

			for metric in METRICS:
				metric_series = series[metric.key]
				handle.write(f"METRICA: {metric.title}\n")
				if not metric_series.points:
					handle.write("Nessun dato disponibile.\n")
				else:
					values_ns = [value for _, value in metric_series.points]
					handle.write(f"Media: {ns_to_ms(int(statistics.fmean(values_ns))):.3f} ms\n")
					handle.write(f"Mediana: {ns_to_ms(int(statistics.median(values_ns))):.3f} ms\n")
					handle.write("Valori per messaggio:\n")
					for operation, value_ns in metric_series.points:
						handle.write(f"  messaggio {operation}: {value_ns} ns ({ns_to_ms(value_ns):.3f} ms)\n")

				if metric_series.missing_operations:
					missing_text = ", ".join(str(value) for value in metric_series.missing_operations)
					handle.write(f"Messaggi senza dato: {missing_text}\n")

				handle.write("\n")

	def build_xticks(self, operations: list[int]) -> list[int]:
		if not operations:
			return []

		ticks = [operation for operation in operations if operation % self.config.x_tick_every == 0]
		if operations[0] not in ticks:
			ticks.insert(0, operations[0])
		if operations[-1] not in ticks:
			ticks.append(operations[-1])
		return sorted(set(ticks))

	def plot_metric(self, metric_series: MetricSeries, client_label: str) -> None:
		try:
			import matplotlib.pyplot as plt
		except ImportError as exc:
			raise RuntimeError("matplotlib non installato: impossibile generare i PNG") from exc

		if not metric_series.points:
			figure, (axis_scatter, axis_violin) = plt.subplots(
				1,
				2,
				figsize=(max(self.config.figure_size[0] * 1.6, 14), self.config.figure_size[1]),
			)
			axis_scatter.set_title(f"{metric_series.metric.title} | {client_label}")
			axis_scatter.set_xlabel("Numero messaggio")
			axis_scatter.set_ylabel("Millisecondi")
			axis_scatter.grid(True, alpha=0.2)
			axis_scatter.text(
				0.5,
				0.5,
				"Nessun dato disponibile",
				transform=axis_scatter.transAxes,
				ha="center",
				va="center",
				fontsize=12,
			)

			axis_violin.set_title("Distribuzione (violino)")
			axis_violin.set_ylabel("Millisecondi")
			axis_violin.set_xticks([1])
			axis_violin.set_xticklabels(["Campioni"])
			axis_violin.set_xlim(0.6, 1.34)
			axis_violin.grid(True, axis="y", alpha=0.25)
			axis_violin.text(
				0.5,
				0.5,
				"Nessun dato disponibile",
				transform=axis_violin.transAxes,
				ha="center",
				va="center",
				fontsize=12,
			)
			figure.tight_layout()

			if self.config.save_png:
				output_name = self.config.output_dir / f"Orch_{metric_series.metric.key}_{client_label}.png"
				figure.savefig(output_name, dpi=140)

			if self.config.show_plots:
				plt.show()
			else:
				plt.close(figure)
			return

		operations = [operation for operation, _ in metric_series.points]
		values_ms = [ns_to_ms(value_ns) for _, value_ns in metric_series.points]
		average_ms = statistics.fmean(values_ms)
		median_ms = statistics.median(values_ms)

		figure, (axis_scatter, axis_violin) = plt.subplots(
			1,
			2,
			figsize=(max(self.config.figure_size[0] * 1.6, 14), self.config.figure_size[1]),
		)

		axis_scatter.scatter(operations, values_ms, color=self.config.point_color, s=32, label="Valori")
		axis_scatter.axhline(
			average_ms,
			color=self.config.average_color,
			linestyle="--",
			linewidth=1.5,
			label="Media",
		)
		axis_scatter.axhline(
			median_ms,
			color=self.config.median_color,
			linestyle="--",
			linewidth=1.5,
			label="Mediana",
		)

		axis_scatter.text(
			0.98,
			0.10,
			f"Media = {average_ms:.3f} ms",
			transform=axis_scatter.transAxes,
			ha="right",
			va="top",
			color=self.config.average_color,
			fontsize=10,
			fontweight="bold",
		)
		axis_scatter.text(
			0.98,
			0.04,
			f"Mediana = {median_ms:.3f} ms",
			transform=axis_scatter.transAxes,
			ha="right",
			va="top",
			color=self.config.median_color,
			fontsize=10,
			fontweight="bold",
		)

		axis_scatter.set_title(f"{metric_series.metric.title} | {client_label}")
		axis_scatter.set_xlabel("Numero messaggio")
		axis_scatter.set_ylabel("Millisecondi")
		axis_scatter.set_xticks(self.build_xticks(operations))
		axis_scatter.grid(True, alpha=0.3)
		axis_scatter.legend(loc="best")

		viol = axis_violin.violinplot(values_ms, showmeans=False, showmedians=False)
		for body in viol["bodies"]:
			body.set_facecolor(self.config.point_color)
			body.set_alpha(0.35)

		# Draw shortened summary lines in the violin panel.
		line_x_min = 0.84
		line_x_max = 1.16
		min_ms = min(values_ms)
		max_ms = max(values_ms)
		axis_violin.hlines(
			average_ms,
			line_x_min,
			line_x_max,
			colors=self.config.point_color,
			linestyles="--",
			linewidth=1.8,
		)
		axis_violin.hlines(
			median_ms,
			line_x_min,
			line_x_max,
			colors=self.config.point_color,
			linestyles="--",
			linewidth=1.8,
		)
		axis_violin.hlines(
			min_ms,
			line_x_min,
			line_x_max,
			colors=self.config.point_color,
			linestyles=":",
			linewidth=1.4,
		)
		axis_violin.hlines(
			max_ms,
			line_x_min,
			line_x_max,
			colors=self.config.point_color,
			linestyles=":",
			linewidth=1.4,
		)
		axis_violin.text(
			1.20,
			average_ms,
			"media",
			color=self.config.point_color,
			fontsize=8,
			va="center",
		)
		axis_violin.text(
			1.20,
			median_ms,
			"mediana",
			color=self.config.point_color,
			fontsize=8,
			va="center",
		)
		axis_violin.text(
			1.20,
			min_ms,
			"min",
			color=self.config.point_color,
			fontsize=8,
			va="center",
		)
		axis_violin.text(
			1.20,
			max_ms,
			"max",
			color=self.config.point_color,
			fontsize=8,
			va="center",
		)
		axis_violin.set_title("Distribuzione (violino)")
		axis_violin.set_xticks([1])
		axis_violin.set_xticklabels(["Campioni"])
		axis_violin.set_xlim(0.6, 1.34)
		axis_violin.set_ylabel("Millisecondi")
		axis_violin.grid(True, axis="y", alpha=0.25)
		figure.tight_layout()

		if self.config.save_png:
			output_name = self.config.output_dir / f"Orch_{metric_series.metric.key}_{client_label}.png"
			figure.savefig(output_name, dpi=140)

		if self.config.show_plots:
			plt.show()
		else:
			plt.close(figure)

	def run(self) -> int:
		missing_logs = self.ensure_input_logs_exist()
		if missing_logs:
			missing_list = ", ".join(str(path) for path in missing_logs)
			print(f"ATTENZIONE: file log mancanti: {missing_list}")
			print("Continuo comunque: verranno prodotti riepilogo e grafici con i dati disponibili.")

		print(f"Input log directory: {self.config.input_dir}")
		merged_operations, client_label = self.merge_operations()
		series = self.build_metric_series(merged_operations)
		self.write_summary(series, client_label)

		for metric in METRICS:
			self.plot_metric(series[metric.key], client_label)

			explosion_avg = build_timestamp_avg_from_merged(merged_operations)
		render_t1_t10_explosion_chart(
					timestamp_avg_ns=explosion_avg,
			output_path=self.config.output_dir / "T1_T10_Explosion.png",
			title=f"Esplosione temporale t1->t10 [{client_label}]",
			alpha=EXPLOSION_ALPHA,
		)

		print(f"Riepilogo scritto in: {self.config.summary_file}")
		if self.config.save_png:
			print(f"Grafici PNG scritti in: {self.config.output_dir}")
		return 0


def main() -> int:
	plotter = OrchestratedPlotter(build_config())
	return plotter.run()


if __name__ == "__main__":
	raise SystemExit(main())
