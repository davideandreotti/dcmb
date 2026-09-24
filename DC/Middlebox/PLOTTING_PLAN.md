# Plotting Plan

The final paper artifact inventory is in `PAPER_FIGURES_MEMO.md`. This file
records the implemented extraction and plotting contract.

## Usage

```bash
python3 benchmarking/plot_latency.py experiments/<campaign-directory>
```

The script always writes normalized data first:

```text
plots/e2e_latency_samples.csv
plots/run_summary.csv
```

It then emits only figures/tables for which the campaign contains the required
matrix. Paper-numbered outputs use complete-matrix guards so smoke and startup
campaigns cannot create plausible-looking partial paper figures.

## Implemented Paper Outputs

```text
P1-P2-latency-dissection.pdf
P1-handshake-latency-dissection.pdf
P2-request-latency-dissection.pdf
P2-request-latency-dissection-dual-scale.pdf
P2-request-latency-dissection-integrated-validation-estimate.pdf
P3a-handshake-latency-distribution-linear.pdf
P3a-handshake-latency-distribution-log.pdf
P3b-persistent-request-latency-distribution.pdf
P4-instance-startup-time.pdf
P5-single-client-throughput-latency-cpu.pdf
P5a-single-client-throughput-latency.pdf
P5b-single-client-throughput-cpu.pdf
P5-single-client-capacity.tex
P7-client-scalability.pdf
P7a-client-scalability-throughput.pdf
P7b-client-scalability-latency.pdf
T1-selected-load-resources.tex
T2-clients-memory.tex
T3-component-costs.tex
T4-handshake-capacity.tex
handshake-capacity-summary.csv
```

P6 is represented by T4 rather than a primary plot. Its detailed curves are
analytical artifacts.

Obsolete overlapping plots have been removed: generic end-to-end violin,
worker-startup distribution, handshake/request duration bars, DC-reuse bars,
and handler-cost bars.

## Extraction Rules

Client trace events are grouped by trace ID. A successful sample requires:

```text
client_request_start
client_response_done with a 2xx status
no client_request_error
```

Durations:

```text
end-to-end = client_response_done - client_request_start
handshake  = client_tls_done - client_request_start
request    = client_response_done - client_request_sent
```

Priming IDs beginning with `warmup-` are excluded. The steady-state extractor
sorts remaining requests by start timestamp and skips the first 10 real
requests. Requests starting within the configured measurement window remain in
the sample even if their response completes just after the window.

Persistent and resumption trace IDs retain the logical client identity.
Resumption capacity counts only handshakes explicitly marked
`client_tls_resumed`; full-handshake fallbacks are invalid quality points.

Connection and delegation binding events correlate client, gateway, worker,
certserver, and Go application-server traces for latency dissection. Component
deltas across different machines require synchronized clocks.

If `<campaign>/tlmsp/` exists, the plotter recursively discovers copied TLMSP
campaigns from their `summary.csv` files. TLMSP events use Unix wall-clock
nanoseconds. The adapter drops the first 10 measured rows per run, uses
`end_to_end_ms` for fresh latency and `request_ms` for persistent latency, and
does not synthesize DC trace events. Full latency data supplies P1/P2 and P3;
Full and No-handler persistent sweeps are distinct P5/P5a and capacity-table
series; No-handler fresh sweeps supply the handshake-capacity comparison.

The request dissection uses repeated path-level components rather than a
generic remainder: client-middlebox segments share one color, middlebox-server
segments share one color, and request/response validation share one legend
item. The final response path absorbs reverse-proxy streaming and tiny
uninstrumented callback-boundary gaps because no complete-upstream-body event
exists.

When TLMSP data is present, the ordinary P2 file retains one common absolute
scale. The additional dual-scale P2 file keeps all rows in one compact frame,
limits the main axis to the DCMB range, and marks the clipped TLMSP bar with an
arrow and its exact total. An inset in the upper-right whitespace shows the
complete plot at its true common scale. No source component is removed.

The integrated-validation estimate is a separate sensitivity figure, not a
measured TLMSP deployment. It preserves TLMSP's measured communication,
protocol, and application components, replaces only its two external-handler
intervals with the measured Baremetal Go request- and response-validation
components, and recomputes the displayed total from that stack.

## Statistical Rules

- Requests are samples within a run; independent runs are the statistical
  units for means and confidence intervals.
- Error bars are two-sided Student-t 95% confidence intervals and appear only
  with multiple runs.
- Final latency plots use arithmetic run means.
- Violin bodies are clipped at per-group p95, but displayed means are computed
  from unfiltered successful samples.
- The logarithmic handshake violin estimates density in log10 latency space.
- Failed/partial runs do not enter aggregate means or capacity estimates.

## Point Quality

One classifier supplies every aggregate plot and table. Invalid conditions
include controller/client failure, request errors/timeouts, non-2xx responses,
gateway drops, resumption fallback, partial client participation, and material
scheduled/achieved deficits. Warnings include boundary-slot deficits, late
slots, and in-flight saturation without an otherwise-invalid outcome.

Plot symbols:

```text
red X       invalid point
amber ^     warning point
```

Every symbol is also explained in terminal output. Invalid points remain
visible but do not connect to healthy curves.

## Resource Rules

- Baremetal/SGX: process-tree CPU time from `cpu/processes.csv`.
- Baremetal memory: process-tree RSS. Shared SGX memory:
  `go_retained_bytes` from `cpu/middlebox_memory.csv`, with no RSS fallback.
- Docker: gateway plus all current worker cgroups from
  `cpu/containers_total.csv`.
- CPU may exceed 100%, meaning more than one logical core.
- Memory is RSS/cgroup memory, not VMS and not SGX EPC consumption.
- Direct is excluded from middlebox CPU/memory outputs.

T2 uses full-handler persistent runs at 10 requests/s and client counts 1, 10,
30, and 40. It reports a p99-filtered sample mean for CPU and p99 memory. A
later refinement should replace the sampled CPU mean with CPU time integrated
over the complete steady-state window.

Docker uses the cgroup-v2 collector locally. Rootful Podman currently lacks the
same cgroup-v2 setup and uses the persistent Podman stats stream with a
no-stream fallback. Verify `metadata.json` reports `podman_stats_stream` before
final Podman measurements.

## Figure-Specific Data

- P1/P2 and T3 operation rows: `configs_latency_dissection.yml`.
- P3, P5, T1: `configs.yml`.
- P4 and T3 worker/startup rows: `configs_startup.yml`; the focused SGX-Go
  process-startup point uses `configs_startup_sgxgo.yml`.
- P7 and T2: `configs_clients_scalability.yml`, optionally supplemented by
  `configs_clients_scalability_docker_sgx_30_rate10_rerun.yml` via
  `--scalability-rerun-dir` (ten runs only for the 30-client fixed-rate point).
- T4 and handshake analytical curves: `configs_handshake_capacity.yml`.
- TLMSP: unchanged ETSI campaign directories copied below
  `<campaign>/tlmsp/{latency,throughput,handshake}`.

Plot styling remains local to each function rather than exposed through a large
CLI. Titles, colors, labels, filenames, percentile clipping, and optional
annotations are intentionally straightforward to edit in code for the final
paper pass.

## Remaining Work

- Run final repeated campaigns and select between the linear/log P3a versions.
- Replace the sampled mean CPU statistic with duration-integrated CPU time over
  the steady-state window for the final resource accounting pass.
- Decide whether the TLMSP P5a line helps at the final scale. It can be removed
  without changing data by setting `INCLUDE_TLMSP_IN_P5A = False`.
- Validate Podman resource collection on Bovisa.
- Implement remote orchestration and clock-synchronization checks before using
  cross-machine component deltas.
