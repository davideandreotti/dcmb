# A Middlebox for Verification of Encrypted FaaS Traffic

This repository contains a research prototype for inspecting encrypted FaaS
traffic without terminating the protected connection at an ordinary proxy. It
implements and benchmarks two approaches:

- **DCMB** uses TLS delegated credentials in a Go middlebox, with bare-metal,
  Gramine SGX, and container-backed deployments.
- **TLMSP** uses the ETSI TLMSP stack and a policy middlebox, with controlled
  Full and No-handler benchmark profiles.

The current experiment code targets Linux and has been tested primarily on
Ubuntu 22.04. SGX and container experiments require additional host support;
the bare-metal paths can be built and exercised independently.

## Repository layout

| Path | Purpose |
| --- | --- |
| `DC/Middlebox/` | DCMB services, containers, experiment controller, and plotting |
| `DC/go/` | Custom Go toolchain with delegated-credential support |
| `ETSI/` | TLMSP configuration, policy components, and benchmarks |
| `certs_external/` | Local certificate generator; generated keys are ignored |
| `docs/` | Historical sequence diagrams and retained request examples |

The paper-output inventory is in
[`DC/Middlebox/PAPER_FIGURES_MEMO.md`](DC/Middlebox/PAPER_FIGURES_MEMO.md).

## Initial setup

Clone the repository together with its pinned dependencies:

```bash
git clone --recurse-submodules https://github.com/davideandreotti/dcmb.git
cd dcmb
git submodule update --init --recursive
```

Build the custom Go toolchain before building DCMB:

```bash
cd DC/go/src
./make.bash
cd ../../..
```

Generate local certificate material:

```bash
cd certs_external
./generate_server_certs.sh
cd ..
```

Generated private keys, delegated credentials, binaries, logs, and experiment
outputs are intentionally excluded from Git. See
[`certs_external/README.md`](certs_external/README.md) for certificate details.

## DCMB

The complete build script creates the Go binaries, Gramine manifests, signed
enclaves, and container images:

```bash
cd DC/Middlebox
./compile.sh
```

It expects the custom `DC/go` toolchain, an SGX-Go toolchain selected through
`SGX_GOROOT`, Gramine commands, an enclave signing key, and Docker. For a
bare-metal-only build, use the custom Go toolchain directly:

```bash
cd DC/Middlebox
export GOROOT="$PWD/../go"
export PATH="$GOROOT/bin:$PATH"
export GOTOOLCHAIN=local

go build -tags trace -o client ./cmd/client
go build -tags trace -o certserver ./cmd/certserver
go build -tags trace -o appserver ./cmd/appserver
go build -tags trace -o middlebox ./cmd/middlebox
```

The full middlebox caches Google's public OIDC signing keys in `jwks.dat`
relative to its working directory. The cache contains no private keys and is
regenerated automatically when it is missing, invalid, or expired. Start the
middlebox once from `DC/Middlebox` with internet access to create or refresh the
root cache before building container images, which copy that file into the
worker image.

Install Python 3.9 or newer and the dependencies used by the benchmark
controller, plotting tools, and retained TLMSP utilities from the repository
root:

```bash
python3 -m pip install -r requirements.txt
```

Before running a campaign, edit its host addresses and working directories for
the target machine. The five versioned campaign definitions are:

- `benchmarking/configs.yml`: primary single-client paper campaign
- `benchmarking/configs_clients_scalability.yml`: client scalability
- `benchmarking/configs_handshake_capacity.yml`: fresh-handshake capacity
- `benchmarking/configs_latency_dissection.yml`: traced latency dissection
- `benchmarking/configs_startup.yml`: process and worker startup

Run and plot a campaign from `DC/Middlebox`:

```bash
python3 benchmarking/run.py --config benchmarking/configs.yml
python3 benchmarking/plot_latency.py experiments/<campaign-directory>
```

The controller creates a timestamped directory under `experiments/`, copies
the exact campaign YAML into it, and embeds the effective configuration in
each run's metadata. Results remain local unless archived separately.

## TLMSP

TLMSP setup, component ownership, and the current execution path are described
in [`ETSI/README.md`](ETSI/README.md). The self-managed latency, throughput,
and handshake campaigns are documented in
[`ETSI/Benchmarking/README.md`](ETSI/Benchmarking/README.md).

TLMSP campaign directories can be copied unchanged beneath a DCMB campaign:

```text
DC/Middlebox/experiments/<dc-campaign>/tlmsp/latency/
DC/Middlebox/experiments/<dc-campaign>/tlmsp/throughput/
DC/Middlebox/experiments/<dc-campaign>/tlmsp/handshake/
```

The DCMB plotter discovers those directories automatically and adds the TLMSP
series to compatible figures and tables.

## Local and generated files

The following stay outside version control:

- raw experiment trees and generated plots;
- generated certificates and private keys;
- generated OIDC public-key caches (`jwks.dat`);
- compiled binaries, Gramine manifests, and signatures;
- one-off diagnostic, smoke, probe, and rerun configurations;
- local notes and backups under `.local/`.

Do not use `git clean -X` in this repository: ignored paths may contain the
only local copy of experiment results or certificate material.
