# DCMB Middlebox

This directory contains the delegated-credential middlebox, its supporting
services, container images, experiment controller, canonical campaign files,
and plotting code.

The controller currently runs every role on one Linux host. The `host` values
in campaign YAML must be `localhost`, `127.0.0.1`, or empty; SSH and distributed
execution are not implemented.

## Components

| Component | Default endpoint | Purpose |
| --- | --- | --- |
| `client` | outbound | Generates fresh, persistent, or resumed TLS/HTTP traffic |
| `middlebox` | `:8443` | Terminates downstream TLS, validates messages, and proxies upstream |
| `middlebox_gateway` | `:9443`, health on `:8088` | Leases container workers and splices TCP streams |
| `certserver` | `:5000` | Verifies optional SGX quotes and issues delegated credentials |
| `appserver` | `:8000` | Serves the benchmark request over TLS or plaintext HTTP |
| `tracecsv` | n/a | Converts binary trace files to CSV |

Direct campaigns connect the client to the application server. Bare-metal and
Gramine campaigns listen through the middlebox on port 8443. Container
campaigns connect through the gateway on port 9443; the gateway creates and
leases workers through the Docker-compatible API.

## Prerequisites

The bare-metal path needs Linux, the custom Go toolchain under `DC/go`, Python
3.9 or newer, and the root Python requirements. SGX and container campaigns
add host-specific prerequisites:

- [Gramine documentation](https://gramine.readthedocs.io/en/stable/) and the
  [Gramine GitHub repository](https://github.com/gramineproject/gramine);
- Gramine's [installation guide](https://gramine.readthedocs.io/en/stable/installation.html),
  [manifest reference](https://gramine.readthedocs.io/en/stable/manifest-syntax.html),
  and [attestation documentation](https://gramine.readthedocs.io/en/stable/attestation.html);
- Intel's [confidential-computing documentation](https://cc-enabling.trustedservices.intel.com/),
  [SGX software stack](https://github.com/intel/confidential-computing.sgx),
  and [SGX SDK repository](https://github.com/intel/confidential-computing.sgx.sdk);
- Intel's SGX SDK [sample code](https://github.com/intel/confidential-computing.sgx.sdk/tree/main/SampleCode)
  and [DCAP repository](https://github.com/intel/confidential-computing.tee.dcap);
- Intel's DCAP [quote-generation sample](https://github.com/intel/confidential-computing.tee.dcap/tree/main/SampleCode/QuoteGenerationSample)
  and [quote-verification sample](https://github.com/intel/confidential-computing.tee.dcap/tree/main/SampleCode/QuoteVerificationSample);
- Docker, or a Docker-compatible Podman socket, for container deployments;
- an SGX-capable host with the required device nodes, AESM, DCAP libraries,
  and PCCS access when quotes are generated or verified.

The certserver's QVL integration was developed against the Intel source layout
under `linux-sgx/external/dcap_source`. See
[`cmd/certserver/README.md`](cmd/certserver/README.md) for the tested version,
library requirements, cache policy, and focused verification checks.

From the repository root, initialize submodules, build the custom Go toolchain,
install Python dependencies, and generate local certificates:

```bash
git submodule update --init --recursive

cd DC/go/src
./make.bash
cd ../../..

python3 -m pip install -r requirements.txt

cd certs_external
./generate_server_certs.sh
cd ..
```

Generated certificates, signing keys, binaries, manifests, images, logs, and
experiment results are local artifacts and are not committed.

## Build

Run the build from this directory:

```bash
cd DC/Middlebox
./compile.sh
```

The current positional interface is:

```text
./compile.sh [all|middleboxHandler|handler|full|emptyHandler|empty|emptyhandler|certserver]
```

`certserver` builds only the certificate server. Omitting the argument, using
`all`, or using one of the accepted handler aliases runs the same complete
pipeline: all service binaries, the full and empty-handler variants, the SGX-Go
middlebox, Gramine manifests/signatures, and—unless disabled—the container
images. The handler aliases are accepted for compatibility; they are not
selective builds.

### Build controls

| Variable | Default | Effect |
| --- | --- | --- |
| `SGX_GOROOT` | `/home/bonsai/go-sgx-mod` | Go toolchain used for `middlebox_sgxgo` |
| `GRAMINE_ENCLAVE_KEY` | `$HOME/.config/gramine/enclave-key.pem` | Gramine signing key; generated when absent |
| `BUILD_DCAP_VERIFY` | `1` | Build QVL and the `dcapverify` certserver tag; `0` builds the non-verifying stub |
| `DCAP_SOURCE` | `$HOME/linux-sgx/external/dcap_source` | Intel QVL source directory used by CMake |
| `BUILD_CONTAINER_IMAGES` | `1` | Build container images; set to `0` to skip them |
| `BUILD_STARTUP_IMAGE` | `0` | Build the standard-Gramine startup-only image when set to `1` |

Examples:

```bash
# Bare binaries/manifests without container images
BUILD_CONTAINER_IMAGES=0 ./compile.sh

# Certserver without QVL/DCAP support
BUILD_DCAP_VERIFY=0 ./compile.sh certserver

# Complete paper build, including the startup-only image
BUILD_STARTUP_IMAGE=1 ./compile.sh
```

The complete build produces these primary artifacts:

| Artifact | Purpose |
| --- | --- |
| `client`, `certserver`, `appserver` | Benchmark client and supporting services |
| `middlebox` | Full validation handler |
| `middlebox_emptyhandler` | No-validation ablation |
| `middlebox_gateway` | Container worker gateway |
| `middlebox_sgxgo` | SGX-Go middlebox binary |
| `*.manifest`, `*.manifest.sgx`, `*.sig` | Gramine-generated local artifacts |
| `dcmiddlebox-worker:baseline` | Native worker container |
| `dcmiddlebox-worker:emptyhandler` | Empty-handler worker container |
| `dcmiddlebox-worker:sgxgo` | SGX-Go worker container |
| `dcmb_gateway:docker` | Gateway container |
| `dcmiddlebox-worker:sgx-standard-startup` | Optional startup-only Gramine image |

For a bare-metal-only build that does not require Gramine or containers, use
the custom Go toolchain directly as shown in the repository root README.

## Runtime controls

Normal experiments should be driven through `benchmarking/run.py`; it supplies
trace paths and most component flags automatically. The most important manual
controls are summarized below. Each Go binary also supports `-h`.

### Client

| Flag | Meaning |
| --- | --- |
| `-mode fresh|persistent|resumption` | Connection behavior |
| `-clients N` | Logical clients for persistent and resumption modes |
| `-d SECONDS` | Timed-run duration |
| `-rate RPS` | Aggregate offered rate; `0` is closed loop where supported |
| `-requests-per-client N` | Fixed per-client work instead of duration |
| `-pacing spin|timer` | Open-loop pacing implementation |
| `-max-in-flight N` | Fresh-request concurrency limit |
| `-ca PATH`, `-servername NAME` | TLS verification inputs |
| `-H "Key: Value"`, `-data BODY` | Request headers and POST body |

### Middlebox and services

| Component | Important controls |
| --- | --- |
| Middlebox | `-reuse_dc`, `-operator_mode`, `-operator_id`, `-operator_default_sni`, `-ca`, `-consume_after_request`, `-exit_after_request` |
| Certserver | `-addr`, `-cert-path`, `-key-path`, `-signature-scheme`, `-duration` |
| Appserver | `-addr`, `-tls`, `-cert-path`, `-key-path` |
| Gateway | `-log_level` plus the Docker/backend environment generated from YAML |
| All traced roles | `-trace`, `-trace-buffer-events`, `-trace-drop-on-full` |

The middlebox receives its upstream endpoints through `OPERATOR_TARGET` and
`OPERATOR_CERT_URL`. `MBX_EMIT_QUOTE=1` enables quote generation in SGX runs.
Container settings such as worker image, network, socket, reuse policy, SGX
devices, AESM directory, pool sizing, and tracing are generated by the
controller from the deployment YAML.

## Benchmark campaigns

The controller has one command-line option:

```bash
python3 benchmarking/run.py --config benchmarking/configs.yml
```

It expands the YAML matrix and runs every deployment/mode/client/rate point for
the requested number of repetitions. For every point it removes stale managed
containers, starts the certificate and application servers when needed, starts
the selected deployment, waits for readiness, applies campaign warmup, starts
resource collection, runs the client, stops components, saves metadata and
logs, and converts nonempty binary traces to CSV. A failed point is recorded in
the campaign summary and does not prevent subsequent points from running.

### Canonical files

| File | Matrix |
| --- | --- |
| `configs.yml` | Single-client latency and operating curves across direct, shared, SGX, container, and resumed-container strategies |
| `configs_clients_scalability.yml` | Persistent closed-loop capacity and fixed-rate latency over increasing client counts |
| `configs_handshake_capacity.yml` | Fresh and resumed handshake-capacity sweeps across the complete deployment set |
| `configs_latency_dissection.yml` | Complete traced fresh, persistent, and resumed latency-dissection matrix |
| `configs_startup.yml` | Native, Gramine, container, standard-Gramine-container, SGX-Go-container, and SGX-Go-process startup |

The committed files describe the reference campaign matrices and testbed.
Create an ignored local copy for machine-specific paths, IP addresses, runtime
sockets, shorter smoke runs, or focused reruns:

```bash
cp benchmarking/configs.yml benchmarking/configs_local.yml
$EDITOR benchmarking/configs_local.yml
python3 benchmarking/run.py -c benchmarking/configs_local.yml
```

The repository ignore rules exclude noncanonical `configs*.yml` files. Do not
edit the five canonical files merely to adapt them to one machine.

### YAML structure

`campaign` controls the campaign as a whole:

| Key | Meaning |
| --- | --- |
| `name`, `output_root` | Output naming and location |
| `runs`, `duration_s` | Repetitions and measured duration |
| `warmup_s`, `cooldown_s`, `readiness_timeout_s` | Lifecycle timing |
| `cpu_interval_s` | Resource sampling interval |
| `trace_buffer_events`, `trace_drop_on_full`, `trace_roles` | Trace buffering and role selection |
| `resource_sampling_enabled` | Enable process/container sampling |
| `sgx_epc_monitor_enabled`, `sgx_epc_interval_ms` | Optional host EPC telemetry |
| `startup_only` | Measure readiness without running the client |

`hosts` defines `client`, `middlebox`, and `server`. `host` is currently only a
locality check; `ip` is the address placed in client and container endpoints;
`work_dir` is retained in copied metadata. `paths.middlebox_dir` is the local
working directory used to launch binaries and resolve relative output paths.

`server` defines `target_url`, `cert_url`, `request_path`, and optional command
arrays for the certificate and application servers. `client` defines the
binary, request data and headers, SNI, pacing, log level, and fresh-mode
`max_in_flight` limit.

`client_matrix` supports either Cartesian inputs or explicit points:

```yaml
client_matrix:
  modes: [fresh, persistent, resumption]
  clients: [1, 10]
  rates_by_mode:
    fresh: [1, 10]
    persistent: [10, 100]

# Alternatively:
client_matrix:
  modes: [persistent]
  points:
    - {clients: 1, rate: 10}
    - {clients: 10, rate: 0}
```

A deployment can override `modes`, `clients`, `rates`, `rates_by_mode`, or
`points`. Supported `kind` values are `direct`, `baremetal`, `gramine_sgx`, and
`docker_gateway`.

Common deployment keys are:

| Kind | Keys |
| --- | --- |
| Bare metal / Gramine | `command`, `env`, `trace_enabled` |
| Container gateway | `runtime`, `image`, `container_name`, `network`, `socket_host`, `socket_container`, `worker_image`, `worker_name_prefix` |
| Worker policy | `worker_reuse_dc`, `worker_sgx_enabled`, `worker_emit_quote`, `worker_ca`, `delete_after_use`, `ticket_identity_key` |
| Pool/readiness | `min_ready`, `max_workers`, `scale_up_by`, `prewarm_for_clients`, `wait_for_full_pool`, `pool_readiness_timeout_s`, `docker_ready_timeout_ms` |
| Collection | `worker_trace_enabled`, `container_stats_scope`, `container_stats_collector`, `docker_api_timeout_ms` |
| SGX container mounts | `worker_sgx_enclave_device`, `worker_sgx_provision_device`, `worker_aesm_dir`, `worker_certs_host_path`, `worker_certs_container_path` |

When `prewarm_for_clients` is enabled, the controller fixes the initial worker
pool to one worker for fresh mode, `clients + 1` for persistent mode, and one
worker per logical client for resumption.

### Output layout

Each invocation creates a timestamped directory below `output_root`:

```text
experiments/<timestamp>_<campaign>/
  campaign.yml
  summary.csv
  <deployment>_clients<N>_<mode>_<rate>_run<I>/
    metadata.json
    stdout/
    stderr/
    traces/
    csv/
    cpu/
```

`campaign.yml` is the exact input copy. `metadata.json` records the effective
configuration, commands, timestamps, process results, and collector details.
Keep failed run directories: their logs and status are part of the experiment
record.

Plot a completed campaign with:

```bash
python3 benchmarking/plot_latency.py experiments/<campaign-directory>
```

Normalized CSVs, analytical plots, and any complete paper figures/tables are
written beneath `<campaign>/plots`. The measurement and data-treatment rules
are documented in [`PAPER_EXPERIMENT_METHOD.md`](PAPER_EXPERIMENT_METHOD.md),
and the campaign-to-output mapping is in
[`PAPER_FIGURES_MEMO.md`](PAPER_FIGURES_MEMO.md).

## Known limitations

- The controller is local-only even though role placement is represented in
  YAML. Multi-host measurements need a separate orchestration layer and clock
  synchronization.
- `-exit_after_request=true` exits before deferred trace flushing; avoid it for
  traced single-use runs.
- Abruptly killing a client may leave buffered trace events unwritten. Managed
  timed runs exit normally and flush their trace.
- Container CPU/memory scope covers the gateway and matching workers, not the
  container daemon, AESM, PCCS, client, certserver, or application server.
- Shared-SGX Go-retained memory is not EPC usage; enable the separate EPC
  monitor only when the host exposes the required counters.
- Canonical SGX results depend on firmware, kernel, Gramine, Intel SGX/DCAP,
  AESM, PCCS, container runtime, and custom toolchain versions. Record these
  alongside every final campaign.
