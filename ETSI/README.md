# ETSI TLMSP implementation

This directory contains the TLMSP side of the encrypted-FaaS middlebox
prototype. The current reproducible path runs on one Linux host and connects a
patched TLMSP client, `tlmsp-mb`, TLMSP-enabled Apache, and the Go application
server used by DCMB.

## Layout

| Path | Purpose |
| --- | --- |
| `TLMSP/` | Pinned OpenSSL, curl, Apache, and `tlmsp-tools` submodules |
| `Configurations/` | UCL policies, including Full and No-handler profiles |
| `NewMiddlebox/` | Go policy handler and listener |
| `Benchmarking/` | Load generator, campaign controller, and detailed instructions |
| `vm/` | Earlier Vagrant provisioning scripts |

Initialize the dependencies from the repository root:

```bash
git submodule update --init --recursive
```

The TLMSP build is installed locally under `.tlmsp/`. Rebuild the complete
stack through `ETSI/TLMSP/tlmsp-tools/build/build.sh`, or use the targeted
rebuild commands in [`Benchmarking/README.md`](Benchmarking/README.md) when
only `tlmsp-tools` or curl changed.

The benchmark README defines `DCMB_ROOT` once and uses repository-relative
paths throughout; no checkout-specific absolute path needs to be committed.

Build the Go policy executables with:

```bash
cd ETSI/NewMiddlebox
./compile.sh
```

Build the benchmark load generator with:

```bash
cd ETSI/Benchmarking
make
```

## Benchmark profiles

- **Full** uses `Configurations/local_init.ucl` and invokes the external Go
  request and response validation path.
- **No-handler** uses `Configurations/local_init_no_handler.ucl`, preserves the
  same TLMSP contexts and network path, and replaces external validation with
  an in-process pass-through baseline.

The self-managed controller starts and stops the application server, Apache,
listener, and TLMSP middlebox for latency, persistent-throughput, and
fresh-handshake campaigns. Exact commands, ports, output formats, CPU scope,
and integration with DCMB plots are documented in
[`Benchmarking/README.md`](Benchmarking/README.md).

Generated binaries, the `.tlmsp/` installation, logs, and `Benchmarking/results/`
are intentionally ignored. Source files under `Benchmarking/` belong to the
main `dcmb` repository, not to a submodule.
