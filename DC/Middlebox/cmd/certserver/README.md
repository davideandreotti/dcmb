# SGX quote verification

The certserver uses Intel's granular QVL with authenticated collateral cached per
process. The first valid quote performs full Intel verification, establishes the
Intel root of trust and collateral expiration, and validates PCK, TCB and QE
identity collateral. Every subsequent quote still has its signatures, key
bindings, TCB/QE status and configured REPORTDATA checked by the wrapper and QVL.

The cache assumes one unchanged PCK certification chain during a benchmark run.
Expired collateral, a changed chain or a clock predating initialization rejects
with a restart diagnostic. There is no refresh timer or fallback verifier. A bad
first quote does not populate the cache; initialization can be retried. The
initialized context is immutable and permits concurrent verification.

The existing accepted nonterminal TCB results remain accepted, but expired
collateral is always rejected, including for nonterminal results. The Go adapter
continues to permit an empty expected REPORTDATA argument while still verifying
the quote cryptographically; the server supplies its existing fixed tag policy.
Requests without attestation retain the existing application behavior.

## Build

From `DC/Middlebox`:

```sh
DCAP_SOURCE=/home/bonsai/linux-sgx/external/dcap_source ./compile.sh certserver
```

The full `./compile.sh` also builds this verifier. `BUILD_DCAP_VERIFY` defaults to
`1`, which builds QVL and enables the `dcapverify` Go tag. For experiments without
attestation, use:

```sh
BUILD_DCAP_VERIFY=0 ./compile.sh certserver
```

The toggle also applies to the full `./compile.sh`. With `0`, QVL/CMake is skipped
and the existing non-DCAP stub is built. It does not change quote emission or
Gramine's runtime attestation settings; those must be disabled separately.

`DCAP_SOURCE` defaults to
`$HOME/linux-sgx/external/dcap_source`; use an initialized Intel checkout, including
its QVL sources. Tested with DCAP 1.23, commit
`808e4c7df2796bb2374d67ca95493bf0cb71450a`, installed DCAP 1.23 development libraries,
GCC 11, CMake, and Ubuntu's assembly-enabled OpenSSL 3.0.2 development package.

CMake compiles unmodified Intel sources into `build/quoteverify/libdcmb_qvl.so`,
privately linked to the system OpenSSL static archive. It copies the public QVL
header into that build directory for cgo. No Intel checkout, system library or
driver is modified. The certserver links to this private library and the installed
DCAP/OpenSSL libraries; retain `build/quoteverify` when running it. Rebuild after
moving the workspace, since the library search path includes the build location.
No file under `benchmarking/attestation_probe` or `experiments` is a build/runtime
dependency.

## Focused verification checks

Use a locally captured SGX v3 quote from this testbed with currently valid
collateral. No quote fixture or test-only hook is built into the server.

```sh
c++ -std=c++17 -O2 -Wall -Wextra -pthread -Ibuild/quoteverify/include \
  cmd/certserver/tests/quoteverify_test.cpp -Lbuild/quoteverify \
  -Wl,-rpath,"$PWD/build/quoteverify" -ldcmb_qvl -lsgx_dcap_quoteverify -lcrypto \
  -o build/quoteverify/quoteverify_test
build/quoteverify/quoteverify_test /path/to/quote.bin
```

The test exercises invalid warmup, concurrent initialization, signed-content and
signature mutations, REPORTDATA policy/binding, malformed evidence, changed
certification data, expiry, backwards time, concurrent diagnostics and recovery
after rejected requests. It requires access to the collateral provider, but does
not generate quotes or alter the clock. The normal benchmark runner exercises
the actual Go certserver and both SGX deployments.

Validated on 2026-09-26: all 40 focused checks passed. Separate 30-second runs
with a fresh quote per handshake completed 60 shared-Gramine and 59 SGX-Go
container requests without HTTP or transport errors. Mean verification after
warmup was 6.19 ms and 6.25 ms respectively; first-quote initialization took
approximately 55 ms. These are smoke-test measurements, not a full benchmark
campaign. Both DCAP and non-DCAP builds were checked.
