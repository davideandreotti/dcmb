# Plotting Plan

Goal: build simple plotting scripts/functions that consume one experiment campaign folder and produce latency, throughput, CPU, and startup plots from converted trace/resource CSVs.

Example usage:

```bash
python3 benchmarking/plot_latency.py experiments/2026-06-26_11-46-16_latency_1
```

Expected outputs:

```text
<campaign>/plots/
  e2e_timeseries.pdf
  e2e_violin.pdf
  e2e_latency_samples.csv
  run_summary.csv
  throughput_offered_achieved.pdf
  latency_vs_offered.pdf
  cpu_timeseries.pdf
  memory_timeseries.pdf
  cpu_vs_offered.pdf
  startup_times.pdf
  worker_startup_distribution.pdf
  handshake_request_duration.pdf
```

## Inputs

For every run folder inside the campaign, read:

```text
metadata.json
csv/client.csv
csv/startup.csv
cpu/processes.csv
cpu/containers_total.csv
```

Ignore runs without `csv/client.csv`.

Do not support old resource CSV formats in the next implementation pass. New process resource files should include RSS/VMS/thread fields; Docker aggregate files should use `cpu_perc_sum` from `cpu/containers_total.csv`.

## End-To-End Latency Extraction

Group client trace events by request `id`.

Use these events:

```text
client_request_start
client_response_done
client_request_error
```

A request is valid when:

- `client_request_start` exists
- `client_response_done` exists
- `client_response_done.arg` is a 2xx HTTP status
- no `client_request_error` exists for that request

Compute:

```text
latency_ns = client_response_done.timestamp_ns - client_request_start.timestamp_ns
latency_ms = latency_ns / 1e6
elapsed_s  = (client_request_start.timestamp_ns - first_request_start_in_run) / 1e9
```

Failed requests are excluded from latency plots but counted for annotations.

## Steady-State Window

For throughput/latency summary plots, use a steady-state request set:

- ignore request IDs starting with `warmup-`
- sort remaining requests by `client_request_start`
- exclude the first remaining real request from steady-state calculations
- include requests whose request-start timestamp is inside the measurement window
- use request-start time for the end cutoff, so requests started before the end still count if they finish after it
- use successful 2xx response events only for achieved-throughput and latency statistics

This cuts both the explicit warmup request and the first measured outlier where delegation retrieval or one-time persistent handshake setup can dominate.

If multiple persistent clients are used, keep the first-pass rule simple: exclude the first request in the run after warmup. If per-client first-request removal becomes necessary, update the helper in one place.

## Persistent Mode

Use `metadata.json`:

```text
parameters.mode
parameters.clients
parameters.rate
parameters.deployment.name
parameters.iteration
```

For persistent mode:

- Keep all valid requests in the raw time-series view unless the plot explicitly says it is steady-state only.
- For steady-state violin/summary plots, use the same window rule above: cut `warmup-*`, then cut the first real request.

First-pass persistent client identification:

- Request IDs look like `lambrate-1-23`.
- Remove the final `-<request_number>` suffix.
- Treat the remaining prefix as the logical persistent client ID.

Example:

```text
lambrate-1-23 -> lambrate-1
```

If the ID shape changes later, update this helper only.

## Processed Data

Write one normalized CSV:

```text
plots/e2e_latency_samples.csv
```

Suggested columns:

```text
campaign
run_name
deployment
mode
clients
rate
iteration
request_id
elapsed_s
latency_ns
latency_ms
status
in_violin
```

For failed requests, include a row with `status=failed` and empty latency fields if practical. This keeps failure annotations reproducible.

This CSV may be large if the campaign has many requests, but it is useful for inspection and avoids reparsing raw traces for every plot. If size becomes annoying later, add an option to skip it or write `.csv.gz`.

## Plot 1: Time Series

Output:

```text
plots/e2e_timeseries.pdf
```

One subplot per run in the campaign.

Each subplot:

- x-axis: `elapsed_s`
- y-axis: `latency_ms`
- linear scale
- all valid requests plotted as unconnected dot samples
- dot samples should be light blue
- overlay a time-window moving average line in dark blue
- add an average-latency annotation on the right side, vertically aligned with the run's average latency
- title includes deployment, mode, clients, rate, and run number
- if failed requests exist, add a red annotation such as `failed: 3`
- keep the failure annotation as a tiny obvious code block that can be manually commented out; do not add a dedicated CLI flag for it

## Plot 2: Violin

Output:

```text
plots/e2e_violin.pdf
```

Pool repeated runs for now.

Group by:

```text
deployment + mode
```

Example labels:

```text
baremetal
fresh

baremetal
persistent

docker_gateway
fresh

docker_gateway
persistent
```

Rules:

- use only valid requests
- exclude `warmup-*`
- exclude the first real request after warmup
- use a linear y-axis
- remove the black violin body border
- print average latency for each violin
- place the average text to the right of the corresponding violin, vertically aligned with the average marker/line
- support an optional percentile clip for violin samples only, e.g. `percentile_clip = 99`; keep it as an easy-to-edit local variable and make it clear in the title if enabled
- add a red failure annotation per group if failures exist

## Script Shape

Keep the plotting script easy to edit manually. Prefer simple functions over deep abstractions:

- Do not grow a large CLI for every visual detail.
- Keep editable plotting knobs close to the specific plot function, or in `main()` when they describe output paths.
- Do not force one global plotting configuration for every figure; time-series and violin plots may need different titles, labels, colors, sizes, y-limits, and annotation behavior.
- Keep the data extraction functions separate from plotting functions so later latency-breakdown plots can reuse the processed samples.

```python
def load_campaign(campaign_dir): ...
def load_run(run_dir): ...
def extract_client_latencies(client_csv, metadata): ...
def write_samples_csv(samples, path): ...

def plot_timeseries(samples, run_summaries, output_path):
    figure_size = (12, 8)
    y_label = "End-to-end latency (ms)"
    dot_color = "#8ecae6"
    moving_average_color = "#023047"
    moving_average_window_s = 1.0
    show_failure_annotations = True  # comment out locally if not desired
    ...

def plot_violin(samples, run_summaries, output_path):
    figure_size = (8, 5)
    y_label = "End-to-end latency (ms)"
    percentile_clip = None  # set to 99 to clip each violin at p99
    show_failure_annotations = True  # comment out locally if not desired
    ...

def main():
    timeseries_filename = "e2e_timeseries.pdf"
    violin_filename = "e2e_violin.pdf"
    samples_filename = "e2e_latency_samples.csv"
    ...
```

Use:

```text
Python stdlib csv/json
matplotlib
```

Pandas is not required for the first version. Avoid seaborn unless it is clearly useful and already installed. Matplotlib's `violinplot` is enough for the first version.

## CLI

Initial CLI:

```bash
python3 benchmarking/plot_latency.py <campaign_dir>
```

Useful options:

```text
--out-dir <campaign_dir>/plots
```

Avoid adding flags for style details unless they are truly needed for automation.

## Next Plotting Pass

The next implementation pass should keep the current latency plots and add the following simple, editable functions.

### Processed Summary

Write a small run-level summary:

```text
plots/run_summary.csv
```

One row per run:

```text
campaign
run_name
deployment
mode
clients
offered_rps
achieved_rps
success
failed
p99_latency_ms
mean_cpu_percent
peak_cpu_percent
startup_process_ready_ms
startup_first_worker_ready_ms
```

This table should be small. Do not duplicate all packet samples into it. Keep packet-level data in `e2e_latency_samples.csv` and raw trace CSVs.

### Latency Time Series With Drops

Extend the existing `e2e_timeseries.pdf`:

- successful requests remain light-blue dots
- moving average remains dark blue
- failed/dropped requests are shown as small red ticks at the top edge of the subplot
- y-axis starts at zero

The red ticks should be a small obvious code block in the plotting function, easy to comment out manually.

### Offered vs Achieved Throughput

Output:

```text
plots/throughput_offered_achieved.pdf
```

Rules:

- x-axis: offered requests/sec from metadata/client config
- y-axis: achieved requests/sec
- achieved throughput counts successful 2xx response events only
- use the steady-state window rule above
- group lines by deployment and mode

### Offered Throughput vs End-To-End Latency

Output:

```text
plots/latency_vs_offered.pdf
```

Rules:

- x-axis: offered requests/sec
- y-axis: p99 end-to-end latency in ms
- use successful requests only
- use the steady-state window rule above
- group by deployment and mode

### CPU Time Series

Output:

```text
plots/cpu_timeseries.pdf
```

Rules:

- baremetal/SGX: use `cpu/processes.csv`
- direct: skip from middlebox CPU plots
- Docker: use `cpu/containers_total.csv`
- process CPU percent is computed from deltas of `user_time_s + system_time_s` over wall-clock sample deltas
- for Gramine/wrapper-style deployments, use process-tree CPU/memory where available
- Docker CPU uses `cpu_perc_sum`
- CPU percent may exceed 100% on multi-core workloads

For process deployments, identify the middlebox process by excluding `server`, `certserver`, and `client` roles rather than relying on old role names.

### Memory Time Series

Output:

```text
plots/memory_timeseries.pdf
```

Rules:

- baremetal/SGX: use `cpu/processes.csv`
- direct: skip from middlebox memory plots
- Docker: use `cpu/containers_total.csv`
- process deployments use `rss_tree_bytes` when available, falling back to `rss_bytes`
- Docker uses `mem_usage_bytes_sum`
- y-axis should be in MiB
- keep this as a diagnostic time-series plot; do not overinterpret Go RSS/VMS as exact live heap usage

### Offered Throughput vs CPU

Output:

```text
plots/cpu_vs_offered.pdf
```

Rules:

- x-axis: offered requests/sec
- y-axis: mean CPU percent over the steady-state window
- skip direct
- Docker uses whole gateway/worker machinery from `containers_total.csv`
- baremetal/SGX use the middlebox process tree where available

### Startup Time

Output:

```text
plots/startup_times.pdf
```

Input:

```text
csv/startup.csv
```

Rules:

- bar plot by deployment strategy
- repeated runs should show mean plus confidence interval, and ideally individual points if run count is small
- baremetal/SGX: process start to `[OPERATOR_READY]`
- Docker: plot both gateway ready and gateway plus first worker ready
- direct has no middlebox startup and should be skipped

### Worker Startup Distribution

Output:

```text
plots/worker_startup_distribution.pdf
```

Input:

```text
csv/startup.csv
```

Rules:

- Docker only
- use `worker_ready_internal` rows
- violin or boxplot of per-worker `startup_ms`

### Handshake vs Request Duration

Output:

```text
plots/handshake_request_duration.pdf
```

Rules:

- separate panels or separate figures for fresh and persistent modes
- group by deployment strategy
- use two adjacent bars per deployment: handshake duration and request duration
- do not stack the bars in the first implementation
- keep deeper breakdowns such as delegation, attestation, validation, and upstream request for a later plotting pass

## Gitignore

Add a gitignore rule for generated plot folders:

```gitignore
DC/Middlebox/experiments/*/plots/
```

If the normalized samples CSV lives under `plots/`, it will be ignored together with generated PDFs. That is acceptable because it is generated data.
