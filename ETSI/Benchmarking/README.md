# TLMSP Benchmarks

This directory provides a bare-metal TLMSP benchmark path for
`POST /function/init`. It can collect individual latency samples, run a
self-managed fixed-duration campaign, and emit standalone CSV files that the DCMB
plotter can discover after they are copied below a DC campaign.

The commands below use one portable repository variable. Set it once in the
shell that will build or run TLMSP:

```bash
export DCMB_ROOT=/path/to/dcmb
```

## Profiles

`full` uses `Configurations/local_init.ucl` and invokes
`NewMiddlebox/client` plus the Go listener for both request and response
validation.

`no_handler` uses `Configurations/local_init_no_handler.ucl`. It retains the
same contexts, read/write rights, regex matching, header-context re-emission,
response-body forwarding, and network path, but re-emits each match using an
in-process `${0}` template. It does not start the Go listener. Therefore,
Full minus No-handler measures the complete external validation path rather
than only the validation function itself.

## Rebuild

Rebuild and install `tlmsp-tools` after changing the middlebox or shared demo
code:

```bash
cd "$DCMB_ROOT/ETSI/TLMSP/tlmsp-tools"
make -j20
make install
```

Rebuild and install patched curl after changing its source:

```bash
cd "$DCMB_ROOT/ETSI/TLMSP/tlmsp-curl"
make -j20
make install
```

If the curl source tree has no `Makefile`, configure it once first:

```bash
cd "$DCMB_ROOT/ETSI/TLMSP/tlmsp-curl"
./buildconf
./configure \
  --prefix="$DCMB_ROOT/.tlmsp" \
  --with-ssl="$DCMB_ROOT/.tlmsp" \
  --with-tlmsp-tools="$DCMB_ROOT/.tlmsp" \
  --disable-silent-rules
make -j20
make install
```

These targeted commands do not rebuild OpenSSL, Apache, APR, or certificates.
Use `ETSI/TLMSP/tlmsp-tools/build/build.sh` only when one of those dependencies
must also be rebuilt.

Build the unified benchmark load generator:

```bash
cd "$DCMB_ROOT/ETSI/Benchmarking"
make
```

The Go application server must include plaintext support. Rebuild it after
changing `cmd/appserver`:

```bash
cd "$DCMB_ROOT/DC/Middlebox"
GOROOT="$DCMB_ROOT/DC/go" \
PATH="$DCMB_ROOT/DC/go/bin:$PATH" \
GOTOOLCHAIN=local \
go build -tags trace -o appserver ./cmd/appserver
```

## Manual Deployment

Run each component in its own terminal. Start the plaintext application:

```bash
cd "$DCMB_ROOT/DC/Middlebox"
./appserver -addr 127.0.0.1:7000 -tls=false -log_level error
```

Start TLMSP-enabled Apache:

```bash
. "$DCMB_ROOT/.tlmsp/share/tlmsp-tools/tlmsp-env.sh"
"$DCMB_ROOT/.tlmsp/bin/httpd" -X -e warn
```

For the Full profile, start the policy listener:

```bash
cd "$DCMB_ROOT/ETSI/NewMiddlebox"
./listener
```

Then start the Full middlebox from `NewMiddlebox`, because its UCL handler is
`./client`:

```bash
. "$DCMB_ROOT/.tlmsp/share/tlmsp-tools/tlmsp-env.sh"
cd "$DCMB_ROOT/ETSI/NewMiddlebox"
rm -f waiting.dat
TLMSP_BENCH_TRACE=1 "$DCMB_ROOT/.tlmsp/bin/tlmsp-mb" \
  -c "$DCMB_ROOT/ETSI/Configurations/local_init.ucl" -a \
  2> >(tee /tmp/tlmsp-middlebox.events.log >&2)
```

For No-handler, omit the listener and replace the config with:

```text
$DCMB_ROOT/ETSI/Configurations/local_init_no_handler.ucl
```

## Manual Smoke Tests

With the deployment running, collect two fresh requests:

```bash
cd "$DCMB_ROOT/ETSI/Benchmarking"
./run_latency.py fresh \
  --warmup 0 --samples 2 --interval-ms 100 \
  --middlebox-log /tmp/tlmsp-middlebox.events.log \
  --output /tmp/tlmsp-fresh.csv
```

Collect one connection-prime request and two measured persistent requests:

```bash
./run_latency.py persistent \
  --warmup 1 --warmup-pause-ms 1000 \
  --samples 2 --interval-ms 100 \
  --middlebox-log /tmp/tlmsp-middlebox.events.log \
  --output /tmp/tlmsp-persistent.csv
```

Every row should have `success=1`. Fresh rows should have `num_connects=1`;
measured persistent rows should have `num_connects=0`. Full rows also contain
`request_handler_ms` and `response_handler_ms`.

## Self-Managed Campaigns

`run_campaign.py` starts and stops the application server, Apache, listener
when needed, and `tlmsp-mb`. It refuses to reuse or terminate processes already
holding ports 4443, 4444, 7000, 8080, or 10001. Stop old manual components with
Ctrl-C before running it; `ss -ltnp` can identify a forgotten process. The
output directory must be new or empty so an interrupted rerun cannot mix stale
and current measurements.

Run the DC-aligned latency campaign for Full and No-handler:

```bash
cd "$DCMB_ROOT/ETSI/Benchmarking"
./run_campaign.py latency \
  --runs 5 --duration-seconds 60 \
  --output-dir results/tlmsp_latency
```

Defaults are one 30-second run, one second of component warmup, one isolated
fresh warmup request, a one-second pause, fresh at 1 request/s, and persistent
at 10 requests/s. Persistent mode additionally primes its retained connection
and waits one second before its measured window. The plot adapter drops the
first 10 measured rows, matching the current DC plotting rule.

The campaign starts Apache with `KeepAlive On` and
`MaxKeepAliveRequests 0`. This keeps the measured persistent series on one
connection instead of inheriting Apache's 100-request limit.

Run focused single-connection persistent throughput points. Full and
No-handler use different established capacity regions, so keep them as two
campaigns below one parent directory:

```bash
./run_campaign.py throughput \
  --runs 5 --duration-seconds 60 --profiles full \
  --rates 1,2,5,10,25,40,50 \
  --output-dir results/tlmsp_throughput/full

./run_campaign.py throughput \
  --runs 5 --duration-seconds 60 --profiles no_handler \
  --rates 25,50,100,150,200,250,300,325,350,400 \
  --output-dir results/tlmsp_throughput/no_handler
```

Run concurrent fresh-handshake capacity using No-handler and no listener:

```bash
./run_campaign.py handshake \
  --runs 5 --duration-seconds 60 \
  --rates 100,125,150,175 \
  --max-in-flight 1024 \
  --output-dir results/tlmsp_handshake
```

Duration/rate runs are fixed-rate open loop. A late slot is recorded as
`missed` and is not replayed in a catch-up burst. Fresh mode uses `curl_multi`
to allow independent handshakes to overlap; persistent mode deliberately uses
one serial HTTP/1.1 connection.

## Outputs And CPU

Each campaign has `metadata.json`, `summary.csv`, and profile folders. Each
point stores a per-request CSV, raw event log, CPU series, and component logs.
`summary.csv` records offered/completed work, failures, missed slots, achieved
rate, observed fresh concurrency, validity, and mean middlebox CPU.

The CPU column is explicitly the `tlmsp-mb` process plus CPU accumulated from
its reaped shell, `stdbuf`, and `client` children. It excludes Apache, the
application server, the load generator, and the Go listener. Full-profile
latency still includes listener work, so this is intentionally labelled
middlebox process-tree CPU rather than total policy-system CPU.

All timestamps and scheduling deadlines use wall-clock Unix nanoseconds so
client, middlebox, controller, and application-server boundaries share one
clock domain on the same host.

The per-request CSV includes:

```text
end_to_end_ms = response_done - transfer_start
handshake_ms  = handshake_done - handshake_start
request_ms    = response_done - Curl_http entry
```

The client-facing and server-facing handshake halves overlap and are retained
for explanatory text only. The complete client-observed TLMSP handshake should
remain one grey component in the figure.

Full-profile rows also contain the application-server interval, both external
handler intervals, and four intervening protocol/path intervals. A row with
`dissection_ok=1` satisfies:

```text
request_ms = path before request validation
           + request validation handler
           + path to application server
           + application processing
           + path to response validation
           + response validation handler
           + path back to client
```

The directional handler intervals directly provide the two orange validation
segments in the latency dissection. Full minus No-handler remains an aggregate
cross-check for the complete external validation-path overhead; it is not
needed to split the two directions.

## Add TLMSP To DC Plots

Place unchanged campaign directories under `tlmsp/` in the DC campaign that
will be plotted:

```text
DC/Middlebox/experiments/<dc-campaign>/tlmsp/latency/
DC/Middlebox/experiments/<dc-campaign>/tlmsp/throughput/
DC/Middlebox/experiments/<dc-campaign>/tlmsp/handshake/
```

Each directory must contain its own `summary.csv` and profile subdirectories.
The existing plotting command discovers them automatically; there is no TLMSP
command-line option:

```bash
cd "$DCMB_ROOT/DC/Middlebox"
python3 benchmarking/plot_latency.py experiments/<dc-campaign>
```

The adapter drops the first 10 measured rows from every TLMSP point. It adds
Full TLMSP to the bottom of P1/P2 and to the right of the P3 violins. Full and
No-handler persistent throughput campaigns are separate series in P5a and the
persistent capacity table; set `INCLUDE_TLMSP_IN_P5A = False` near the
top-level plotting constants if their scale makes that panel unhelpful. The
No-handler fresh sweep is added to the handshake-capacity outputs and is
labeled as open-loop rather than as a fixed client count.
