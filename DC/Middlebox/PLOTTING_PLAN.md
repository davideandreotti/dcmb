# Plotting Plan

Goal: build a simple plotting script that consumes one experiment campaign folder and produces end-to-end latency plots from the converted client trace CSVs.

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
```

## Inputs

For every run folder inside the campaign, read:

```text
metadata.json
csv/client.csv
```

Ignore runs without `csv/client.csv`.

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

- Keep all valid requests in the time-series plot.
- Exclude the first successful request per persistent client from the violin dataset, because it includes the connection/TLS setup.

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
- exclude persistent first request per client
- do not discard warmup for now
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

## Gitignore

Add a gitignore rule for generated plot folders:

```gitignore
DC/Middlebox/experiments/*/plots/
```

If the normalized samples CSV lives under `plots/`, it will be ignored together with generated PDFs. That is acceptable because it is generated data.
