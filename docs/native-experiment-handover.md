# Native Shared/Container experiment handover

Updated 2026-10-18. Publish the portable changes first, then clone from GitHub
and rebuild on the new server. Do not copy experiments, images, virtual
environments, certificates, CPU restore state, or the old checkout.

## Scope and final measurements

Rerun **native Shared and native Container only**, in this order:

| Template | Workload | Repetitions |
| --- | --- | ---: |
| [native-scalability.yml](native-scalability.yml) | Persistent; clients 1/5/10/20/30/40/50; closed loop and 500 aggregate RPS | 140 |
| [native-single-client.yml](native-single-client.yml) | Persistent; one client; 17 rates through 5000 RPS | 170 |

These templates come from the final campaigns' copied configurations. They keep
five 60-second repetitions, one-second warmup, ten-second cooldown, credential
reuse, full validation, POST /function/init, and the existing request/headers.
Single-client pacing is **spin**; scalability pacing is **timer**.
Shared uses a tracing-enabled binary with -trace buffers; container workers use
worker_trace_enabled: buffers. Client/service trace files remain enabled.
Buffer mode retains logger allocations without recording or disk writes.

No SGX, Direct, TLMSP, handshake, latency-dissection, or startup reruns are needed
for this move. Existing measurements stay on the old host. The latest full
sources there are:

- DC/Middlebox/experiments/2026-10-05_21-32-03_clients_scalability
- DC/Middlebox/experiments/2026-10-06_04-17-45_paper_single_client
- DC/Middlebox/experiments/2026-10-06_11-25-13_handshake_capacity
- DC/Middlebox/experiments/2026-10-03_13-45-38_latency_dissection
- DC/Middlebox/experiments/2026-10-04_08-50-07_middlebox_startup

Keep new-host results separate until any cross-host replacement is reviewed.
Equal hardware does not establish identical software/runtime conditions.

## 1. Clone and build native components

Install Git, a bootstrap Go compiler (Go 1.20.6 or newer), a C compiler, Python
3.10+ with pip/venv, OpenSSL, Docker Engine, TuneD, and screen using the new OS's
package instructions. The controller uses a rootful Docker socket. No Gramine,
DCAP/QVL, AESM, or SGX-Go compiler is needed for these native campaigns.

After the portable commit has been reviewed and pushed to main:

~~~bash
git clone https://github.com/davideandreotti/dcmb.git
cd dcmb
export DCMB_ROOT="$PWD"
git submodule update --init DC/go
cd DC/go/src
bash make.bash
cd "$DCMB_ROOT"
python3 -m venv DC/Middlebox/.venv-native
DC/Middlebox/.venv-native/bin/python -m pip install -r requirements.txt
export GOROOT="$DCMB_ROOT/DC/go"
export PATH="$GOROOT/bin:$PATH"
export GOTOOLCHAIN=local
~~~

The client currently has a host-specific default CA constant. Reapply this
local-only edit **before building**; the controller does not forward a client.ca
YAML option. Do not globally replace container-internal /home/bonsai paths.

~~~bash
cd "$DCMB_ROOT/DC/Middlebox"
sed -i "s|^const defaultCA = .*|const defaultCA = \"$DCMB_ROOT/certs_external/ca.crt\"|" cmd/client/main.go
go build -tags trace -o client ./cmd/client
go build -tags trace -o middlebox ./cmd/middlebox
go build -tags trace -o middlebox_gateway ./cmd/gateway
go build -tags trace -o certserver ./cmd/certserver
go build -tags trace -o appserver ./cmd/appserver
cd "$DCMB_ROOT"
bash certs_external/generate_server_certs.sh
sudo systemctl enable --now docker
sudo docker build -f DC/Middlebox/dcmiddlebox.Dockerfile \
  --build-arg MIDDLEBOX_BINARY=middlebox -t dcmiddlebox-worker:baseline .
sudo docker build -f DC/Middlebox/Dockerfile.gateway -t dcmb_gateway:docker .
~~~

The certserver build above uses the existing non-QVL stub, appropriate for
quote-disabled runs. It does not enable attestation verification.
Build the images on the new host; no image export/import or hash inventory is
required. Docker updates tags on a successful build; any old containers would
still need recreation. A new server has no old containers to preserve.

## 2. Apply host settings in ignored local YAMLs

~~~bash
export SERVER_IP=REPLACE_WITH_NEW_HOST_IPV4
cd "$DCMB_ROOT/DC/Middlebox"
cp ../../docs/native-scalability.yml benchmarking/configs_native_scalability.local.yml
cp ../../docs/native-single-client.yml benchmarking/configs_native_single_client.local.yml
sed -i -e "s|/path/to/dcmb|$DCMB_ROOT|g" -e "s|SERVER_IP|$SERVER_IP|g" \
  benchmarking/configs_native_scalability.local.yml \
  benchmarking/configs_native_single_client.local.yml
~~~

Use a real host IP reachable from Docker workers, not loopback. Roles remain
localhost: the runner does not implement remote orchestration.
Templates use Docker and /var/run/docker.sock. Do not change container-internal
certificate paths: the Dockerfiles and worker mounts use those deliberately.
Allow the container network to reach host ports 5000 and 8000. If SELinux is
Enforcing, validate socket/certificate/trace mounts before the full run; do not
silently disable security or rewrite the controller.

Apply the same CPU controls, using the new machine's own saved restore state:

~~~bash
cd "$DCMB_ROOT"
sudo bash scripts/cpu-performance-mode.sh on
sudo bash scripts/cpu-performance-mode.sh status
~~~

The script selects latency-performance, performance governor/EPP where supported,
and disables idle states 2 and 3 by default. Do not add CPU/NUMA pinning, GOGC,
pacing, or native GOMAXPROCS overrides. Restore afterwards with the script's off
command if appropriate. Never copy its ignored .local-state file between hosts.

## 3. Smoke, then execute

First derive a two-point smoke configuration from the local single-client file:

~~~bash
cd "$DCMB_ROOT/DC/Middlebox"
.venv-native/bin/python - <<'PY'
import pathlib, yaml
source = pathlib.Path("benchmarking/configs_native_single_client.local.yml")
config = yaml.safe_load(source.read_text())
config["campaign"].update(name="native_smoke", runs=1, duration_s=5, warmup_s=1, cooldown_s=2)
config["client_matrix"] = {"modes": ["persistent"], "clients": [1], "rates": [10]}
for deployment in config["deployments"]:
    deployment.pop("points", None)
    deployment.pop("rates_by_mode", None)
    deployment["rates"] = [10]
pathlib.Path("benchmarking/configs_native_smoke.local.yml").write_text(yaml.safe_dump(config, sort_keys=False))
PY
sudo "$DCMB_ROOT/DC/Middlebox/.venv-native/bin/python" benchmarking/run.py \
  --config benchmarking/configs_native_smoke.local.yml
~~~

Check both runs' summaries and logs: readiness succeeds, client exits zero, no
errors/timeouts/non-2xx, and nonempty client CSV traces. The controller's own exit
alone is insufficient: it continues after individual point failures.

Then, inside screen, run scalability first and single-client second with one
sudo invocation. The explicit Python path uses the virtual environment under
sudo; the root shell lasts until both commands finish.

~~~bash
sudo env DCMB_ROOT="$DCMB_ROOT" bash -c '
set -e
cd "$DCMB_ROOT/DC/Middlebox"
PY="$DCMB_ROOT/DC/Middlebox/.venv-native/bin/python"
"$PY" benchmarking/run.py --config benchmarking/configs_native_scalability.local.yml
"$PY" benchmarking/run.py --config benchmarking/configs_native_single_client.local.yml
'
~~~

310 repetitions take at least six hours from duration/warmup/cooldown alone;
allow roughly seven hours or more including startup/readiness/cleanup.
Afterwards, from the repository root, fix ownership of the bounded result tree:

~~~bash
sudo chown -R "$(id -un):$(id -gn)" "$DCMB_ROOT/DC/Middlebox/experiments"
~~~

Inspect summaries before plotting or merging. Plot each new campaign using the
same virtual environment and benchmarking/plot_latency.py. Full comparison
figures may be skipped when SGX/Direct series are absent; this is expected.
No old data should be copied into these native-only campaigns automatically.

## Full canonical methodology retained in GitHub

The five canonical files remain rerunnable for all strategies after local path
and platform setup, not only the native subset above.

| Campaign | Final settings |
| --- | --- |
| Single-client | Five repeats, 60 s, one persistent client, spin pacing; rates 1/2/5/10/25/50/100/250/500/750/1000/1250/1500/2000/3000/4000/5000 |
| Scalability | Five repeats, 60 s, timer pacing, clients 1/5/10/20/30/40/50; closed loop and 500 aggregate RPS |
| Handshake capacity | Five repeats, 60 s, spin pacing, ten logical clients; deployment-specific fresh/resumed rate sweeps |
| Latency dissection | Five repeats, 60 s, one client; fresh 1, persistent 10, resumed 5 RPS; full middlebox/worker traces |
| Startup | Six deployment kinds, five repeats each, readiness only; no timed traffic |

Single-client canonical also retains 35 low-load fresh/resumed reference points:
460 total repetitions, of which 425 are persistent. Other canonical totals are
280 scalability, 140 handshake, 60 latency, and 30 startup.

Handshake offered rates are unchanged from the final campaign:

| Deployment | Fresh RPS | Resumed RPS |
| --- | --- | --- |
| Direct | 3000/4000/5000 | — |
| Shared | 1500/2000/3000/4000 | — |
| Shared SGX | 100/250/500/750 | — |
| Container | 2/3/4/5 | 1250/1500/2000/3000 |
| Container SGX-Go | 1/1.5/2/2.5/3 | 125/250/500/750 |

Single-client/scalability cooldown is 10 s, handshake 5 s, latency 2 s;
startup cooldown is 10 s. Fresh-request max_in_flight stays 1024 in the
throughput/capacity matrices and 16 in latency dissection. Throughput trace
buffers retain 100000 events of capacity with drop-on-full; latency dissection
keeps drop-on-full false. No additional GOGC override is introduced.

Do not apply two-workers/scale-one indiscriminately:

- Persistent throughput pools: prewarm_for_clients true, scale_up_by 10.
  Effective min_ready=max_workers=clients+1, wait_for_full_pool true: two workers
  for single-client; 2/6/11/21/31/41/51 for scalability.
- Fresh handshake containers: min_ready=max_workers 2, scale_up_by 1,
  prewarm_for_clients false, wait_for_full_pool true, worker_reuse_dc false,
  delete_after_use true.
- Resumed handshake containers: ten ready/max workers for ten clients through
  prewarming, scale_up_by 10, full-pool wait. Keep workers after use; native
  worker_reuse_dc false, SGX-Go true; shared ticket key dcmb-handshake-capacity.
- Latency-dissection container pools: min_ready 2, scale_up_by 1, full-pool wait,
  no max_workers override. Startup container pools also retain min_ready 2 and
  scale_up_by 1.

The three throughput campaigns use shared -trace buffers / worker buffers;
latency keeps full tracing and startup keeps its original trace settings.
Quotes are disabled: shared MBX_EMIT_QUOTE="0", workers worker_emit_quote="0",
and manifests sgx.remote_attestation="none". Keep them disabled on the new host.
Re-enabling attestation later requires appropriate runtime and manifest settings.

Standard shared Gramine defaults are 2G enclave, 256 threads, GOMAXPROCS 32,
GOMEMLIMIT 1300MiB. SGX-Go keeps its separate defaults (shared 128M/10 threads,
container 64M/16 threads), not the standard shared allocation.

## Optional later SGX-Go setup

This is not needed for the native reruns. On the old server, replace the SSH
login and destination path, then copy the compiler and sources:

~~~bash
ssh user@new-server 'mkdir -p /home/user/sgx_tests/go-sgx-mod'
rsync -a --exclude='.git' /home/andreotti/sgx_tests/go-sgx-mod/ \
  user@new-server:/home/user/sgx_tests/go-sgx-mod/
~~~

On the new server:

~~~bash
export SGX_GOROOT=/home/user/sgx_tests/go-sgx-mod
"$SGX_GOROOT/bin/go" version
~~~

Expected old-host compiler version: go1.22.0-devel-cf-sgx linux/amd64. Its .git
file references a missing former submodule location; exclude that broken
metadata. This directory is separate from DC/go. Check Linux/architecture
compatibility before using the copied executable; rebuild from its sources if
necessary.

## Local adaptations and ETSI repositories

| Location | New-host action |
| --- | --- |
| Ignored benchmarking/configs*.local.yml | Substitute repository root, host IP, socket/runtime; published templates use Docker |
| cmd/client/main.go | Locally set defaultCA before rebuilding, as above |
| compile.sh environment | Set SGX_GOROOT for later SGX builds; no host default-path commit |
| Gramine *.manifest.template | For later SGX, adapt certificate/trace paths and OS library mounts; this host uses Rocky /lib64 mappings, published templates retain the previous OS layout |
| ETSI/Benchmarking/Makefile | Override PREFIX with the new absolute .tlmsp installation prefix |
| ETSI/Benchmarking/tlmsp_loadgen.c | Pass the existing --config option instead of changing its default config path |
| ETSI/httpd_tlmsp_local.conf | Update LoadModule, certificate/key, and TLMSPConfigFile paths to the local .tlmsp/repository locations |
| scripts/cpu-performance-mode.local-state | Generated per host, ignored; never transfer |
| Local plotter exceptions | Sparse-EOF acceptance and historical SGX2 per-client-rate reinterpretation are not published defaults |

ETSI wrappers and configs belong to the parent dcmb repo. The submodules are:

- [tlmsp-tools](https://github.com/davideandreotti/tlmsp-tools)
- [tlmsp-curl](https://github.com/davideandreotti/tlmsp-curl)
- [tlmsp-openssl](https://github.com/ricnava00/tlmsp-openssl)
- [tlmsp-apache-httpd](https://forge.etsi.org/rep/cyber/tlmsp-apache-httpd)

No tracked submodule edits or changed gitlinks were found in this review.
Apache has untracked build products only. No separate ETSI push is needed now:
the dcmb push carries wrappers and existing submodule pointers. For a later
TLMSP build, initialize ETSI/TLMSP submodules and follow [ETSI/README.md](../ETSI/README.md).
If submodule sources are modified later, push those to their owning repository
first, then commit the updated parent gitlink.

The optional SGX EPC collector remains gated by campaign.sgx_epc_monitor_enabled
(default false) and SGX deployment detection. Its page-in return-value fix does
not activate it. It needs compatible kernel probe symbols and bpftrace only when
enabled; its occupancy counters describe the host, not one isolated enclave.
SGX1/SGX2 comparison plots activate only when both platforms have data.
