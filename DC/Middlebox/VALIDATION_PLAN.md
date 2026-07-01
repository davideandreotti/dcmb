# Middlebox Validation Status And Remaining Plan

## Goal

Enable the existing full `middleboxHandler` validation path in the active
middlebox request flow, while keeping the `emptyhandler` build tag as the
no-validation baseline. The implementation should remain testing-friendly and
benchmark-friendly.

## Implemented

### Request Validation Flow

- `main.go` now calls `processRequest(r)` before proxying.
- The request-side trace markers wrap the real validation path:

```go
benchtrace.Mark(benchtrace.MiddleboxValidationStart, traceID, 0)
valid, user, messageType := processRequest(r)
benchtrace.Mark(benchtrace.MiddleboxValidationDone, traceID, ...)
```

- Invalid requests are rejected with:

```go
http.Error(w, "forbidden", http.StatusForbidden)
```

- The same call site works for both builds:
  - full/default build: `middleboxHandler.go` performs validation
  - `emptyhandler` build: `emptyHandler.go` returns valid immediately

### Response Processing Flow

- `main.go` stores the validation result in the proxied request context:

```go
type validationResult struct {
    user        string
    messageType any
}
```

- `proxy.ModifyResponse` retrieves that result and calls:

```go
processResponse(resp, result.user, result.messageType)
```

- Response-side validation/session update is traced with:

```go
benchtrace.Mark(benchtrace.MiddleboxResponseValidationStart, traceID, 0)
processResponse(resp, result.user, result.messageType)
benchtrace.Mark(benchtrace.MiddleboxResponseValidationDone, traceID, 0)
```

### Session Identity

Session identity now prefers `X-Client-ID`:

1. `X-Client-ID`, if present.
2. Validated user/email, if available.
3. `unknown`.

This avoids collapsing dummy-token benchmark clients into `test@example.com`.

### Session Storage And Cleanup

- The plain global session map was replaced with per-session entries:

```go
type sessionEntry struct {
    mu       sync.Mutex
    session  Session
    lastSeen time.Time
}

var sessions sync.Map // key string -> *sessionEntry
var sessionCleanupCounter atomic.Uint64
```

- Each session key has its own lock, so unrelated clients do not block each
  other.
- Opportunistic TTL cleanup is implemented in `processRequest()`:

```go
const SessionTTL = 2 * time.Second
const SessionCleanupEvery = 1000
```

- Every 1000 processed requests, expired sessions are removed.

### Client Behavior

- Fresh mode now uses a unique `X-Client-ID` per measured request:

```text
baseID-requestID
```

- Persistent mode remains unchanged:

```text
baseID-clientIndex
```

- Warmup uses a distinct client ID:

```text
baseID-warmup
```

- No new client flag was added. Existing `-H` still passes validation/test
  headers:

```bash
-H "Authorization: Bearer token"
-H "X-Testing: 1"
```

### Testing-Friendly Policy

The current testing-friendly constants remain:

```go
AllowTestingHeader = true
UseDummyTokenFallback = true
```

Benchmarks can therefore use dummy bearer tokens while still exercising the
validation path.

### Build Outputs

`compile.sh` now builds both middlebox variants:

```text
middlebox                # full validation build
middlebox_emptyhandler   # no-validation baseline, built with emptyhandler tag
```

Both keep the trace build tag.

### Experiment Selection

No new YAML flag is needed. The existing `command` field can select the desired
binary:

```yaml
deployments:
  - name: baremetal_validation
    kind: baremetal
    command: ["./middlebox", "-log_level", "debug", "-minimal_logs=false"]

  - name: baremetal_empty
    kind: baremetal
    command: ["./middlebox_emptyhandler", "-log_level", "debug", "-minimal_logs=false"]
```

The controller appends trace flags after the configured command.

## Verified

The following checks were run after implementation:

```bash
gofmt -w internal/trace/events.go cmd/middlebox/main.go cmd/middlebox/middleboxHandler.go cmd/client/main.go
bash -n compile.sh
GOROOT=/home/bonsai/dcmb/DC/go PATH=/home/bonsai/dcmb/DC/go/bin:$PATH GOTOOLCHAIN=local go test ./cmd/middlebox ./cmd/client
GOROOT=/home/bonsai/dcmb/DC/go PATH=/home/bonsai/dcmb/DC/go/bin:$PATH GOTOOLCHAIN=local go test -tags trace ./cmd/middlebox ./cmd/client
GOROOT=/home/bonsai/dcmb/DC/go PATH=/home/bonsai/dcmb/DC/go/bin:$PATH GOTOOLCHAIN=local go test -tags emptyhandler ./cmd/middlebox
GOROOT=/home/bonsai/dcmb/DC/go PATH=/home/bonsai/dcmb/DC/go/bin:$PATH GOTOOLCHAIN=local go test -tags "trace emptyhandler" ./cmd/middlebox
git diff --check
```

## Missing / Still To Do

### Run Full Build

Run the full compile script in the target environment:

```bash
./compile.sh
```

This was not run during implementation because it also generates/signs Gramine
manifests and builds Docker images.

### Smoke Test Validation

Run a full validation smoke test:

```bash
./client -d 5 -rate 10 \
  -H "Authorization: Bearer token" \
  -H "X-Testing: 1" \
  -servername server \
  https://localhost:8443/function/init
```

Run the same test against `./middlebox_emptyhandler` to confirm the no-validation
baseline still behaves as expected.

### Benchmark Configs

Add concrete benchmark deployment entries for:

- full validation baremetal
- empty-handler baremetal
- any SGX/container variants needed for the paper/experiments

The controller already supports this through `command`.

### JSON Schema Benchmark Coverage

`/function/init` currently matches `initMessageType`, which has no schema.
Benchmarks against `/function/init` exercise token/header validation, endpoint
matching, session lookup, and response processing, but not JSON schema
validation.

To measure JSON schema validation, add a benchmark case that targets an endpoint
with schemas, for example one of the product/photo endpoints in
`messageTypes.go`, and provide a valid JSON body.

### Container No-Validation Variant

The current worker Dockerfile copies `./middlebox`, so Docker workers use the
full-validation binary after rebuilding. If containerized no-validation
experiments are needed, add a separate worker image or Dockerfile/runtime option
that copies/runs `./middlebox_emptyhandler`.

### Strict JWT Mode

The current code remains testing-friendly:

```go
AllowTestingHeader = true
UseDummyTokenFallback = true
```

If strict JWT validation is needed later, add a config/build option or edit
these constants. Strict mode will also require valid tokens and correct JWKS
availability.

### OIDC/JWKS Startup Caveat

The full handler initializes OIDC/JWKS state at process startup. If the cache is
missing or expired, startup may contact the configured OIDC provider. This may
matter for startup-latency experiments and should be checked before final runs.
