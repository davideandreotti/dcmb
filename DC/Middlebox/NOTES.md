# Middlebox Notes

## Done So Far

- Cleaned up delegated credential handling in the middlebox.
- `reuse_dc=true` caches delegated credentials by SNI.
- `reuse_dc=false` fetches delegation material per TLS session without storing it globally.
- The middlebox ignores the server private key material even if the cert server still sends it.
- Removed shared `upstreamCertPool` state.
- Removed `InsecureSkipVerify` from the client and middlebox paths.
- Client now verifies the middlebox-presented certificate/delegated credential chain using `certs_external/ca.crt`.
- Middlebox now verifies the upstream server certificate using `certs_external/ca.crt`.
- Client advertises delegated credential support.
- Middlebox requires TLS 1.3 for the client-facing TLS server.
- Certificate paths were made coherent around `certs_external`.

## Current Certificate Model

- The server CA is `certs_external/ca.crt`.
- The server certificate/key/delegated credential material lives in `certs_external/server`.
- The same server certificate identity is currently used through delegation for the client-to-middlebox side.
- The active server certificate contains SANs for `server`, `localhost`, and `127.0.0.1`.

## Important Reminder

- If the client connects to a hostname such as `mb_gateway`, certificate verification will fail unless:
  - the client uses `-servername server`, or
  - the certificate SANs are regenerated to include that hostname.
- Sample runs should use `server` as the TLS server name.

## Still To Do

- Clean up remaining Docker/script certificate mount paths.
- Add a capacity/busy bound for deployed middlebox pods, so `/ready` and `/assign` can tell the gateway when a pod is already serving a client.
- Revisit connection reuse so client-middlebox and middlebox-server TLS sessions stay open when appropriate.
- Support two client experiment modes:
  - fresh mode: one request per new TCP/TLS session, then close;
  - pool mode: keep N HTTP/1.1 TLS connections open in parallel, and send sequential requests on each connection.
- Compare fresh mode and pool mode using the same global offered request rate, not the same per-connection rate.
- Make the gateway route all connections/requests for the same logical client/session to the same middlebox pod when pool mode is used.
- Consider implementing the alternative delegation protocol where the middlebox generates the delegated key locally and sends only the public delegation material to the server for signing.
