// middlebox.go
package main

import (
	"bytes"
	"context"
	"crypto/sha256"
	"crypto/tls"
	"crypto/x509"
	"encoding/base64"
	"encoding/json"
	"encoding/pem"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"net/http/httptrace"
	"net/http/httputil"
	"net/url"
	"os"
	"os/signal"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"time"

	"dc/middlebox/internal/ticketidentity"
	benchtrace "dc/middlebox/internal/trace"
	"dc/middlebox/internal/tracebind"
)

const (
	operatorTLSAddr  = ":8443"
	operatorHTTPAddr = ":18080"

	defaultOperatorTarget  = "https://localhost:8000"
	defaultOperatorCertURL = "http://localhost:5000"
	expectedSNI            = "server"
	defaultCA              = "/home/bonsai/dcmb/certs_external/ca.crt"
)

var (
	reuseDC         bool
	minimalLogs     bool
	logLevel        string
	operatorTarget  string
	operatorCertURL string
)

type certResponse struct {
	CertB64   string `json:"cert_b64"`
	KeyB64    string `json:"key_b64"` // Legacy server field; intentionally ignored.
	DCCredB64 string `json:"dc_cred_b64"`
	DCKeyB64  string `json:"dc_key_b64"`
}

type delegationMaterial struct {
	cert tls.Certificate
}

type traceIDContextKey struct{}
type validationResultContextKey struct{}
type connectionKeyContextKey struct{}

type validationResult struct {
	user        string
	messageType any
}

type traceResponseWriter struct {
	http.ResponseWriter
	traceID string
	wrote   bool
}

func (w *traceResponseWriter) markFirst() {
	if w.wrote {
		return
	}
	w.wrote = true
	benchtrace.Mark(benchtrace.MiddleboxDownstreamResponseFirst, w.traceID, 0)
}

func (w *traceResponseWriter) WriteHeader(statusCode int) {
	w.markFirst()
	w.ResponseWriter.WriteHeader(statusCode)
}

func (w *traceResponseWriter) Write(p []byte) (int, error) {
	w.markFirst()
	return w.ResponseWriter.Write(p)
}

func (w *traceResponseWriter) Flush() {
	w.markFirst()
	if flusher, ok := w.ResponseWriter.(http.Flusher); ok {
		flusher.Flush()
	}
}

func (w *traceResponseWriter) Unwrap() http.ResponseWriter {
	return w.ResponseWriter
}

type operatorState struct {
	id               string
	mode             string
	ready            atomic.Bool
	delegationReady  atomic.Bool
	busy             atomic.Bool
	assigned         atomic.Int64
	totalReq         atomic.Int64
	consumed         atomic.Bool
	consumeOnce      bool
	exitAfterRequest bool
}

type ticketSessionStore struct {
	key        []byte
	operatorID string
	issued     atomic.Bool

	mu       sync.RWMutex
	identity []byte
	state    []byte
}

var (
	certCache         = make(map[string]*tls.Certificate)
	certMu            sync.RWMutex
	delegationFetchMu sync.Mutex
	firstDCDiscarded  atomic.Bool
	delegationCounter atomic.Uint64
	connectionCounter atomic.Uint64
	operatorTraceID   string
)

func nextDelegationID() string {
	return fmt.Sprintf("%s-dc-%d", operatorTraceID, delegationCounter.Add(1))
}

func errorArg(err error) uint64 {
	if err != nil {
		return 1
	}
	return 0
}

func newTicketSessionStore(identityKey string, operatorID string) *ticketSessionStore {
	identityKey = strings.TrimSpace(identityKey)
	if identityKey == "" {
		return nil
	}
	return &ticketSessionStore{
		key:        []byte(identityKey),
		operatorID: operatorID,
	}
}

func (s *ticketSessionStore) wrapSession(cs tls.ConnectionState, ss *tls.SessionState) ([]byte, error) {
	serviceID := cs.ServerName
	if strings.TrimSpace(serviceID) == "" {
		serviceID = expectedSNI
	}

	identity, err := ticketidentity.Derive(s.key, s.operatorID, serviceID)
	if err != nil {
		return nil, err
	}
	state, err := ss.Bytes()
	if err != nil {
		return nil, err
	}

	s.mu.Lock()
	s.identity = cloneBytes(identity)
	s.state = cloneBytes(state)
	s.mu.Unlock()
	s.issued.Store(true)

	return identity, nil
}

func (s *ticketSessionStore) unwrapSession(identity []byte, _ tls.ConnectionState) (*tls.SessionState, error) {
	s.mu.RLock()
	knownIdentity := cloneBytes(s.identity)
	state := cloneBytes(s.state)
	s.mu.RUnlock()

	if len(knownIdentity) == 0 || !ticketidentity.Equal(identity, knownIdentity) {
		return nil, nil
	}
	return tls.ParseSessionState(state)
}

func (s *ticketSessionStore) ticketIssued() bool {
	return s != nil && s.issued.Load()
}

func cloneBytes(in []byte) []byte {
	if len(in) == 0 {
		return nil
	}
	out := make([]byte, len(in))
	copy(out, in)
	return out
}

func cacheFetchedDelegation(sni string, cert *tls.Certificate) bool {
	// Comment out this block to keep the first fetched delegated credential.
	if firstDCDiscarded.CompareAndSwap(false, true) {
		debugf("discarding first fetched delegated credential")
		return false
	}

	certMu.Lock()
	certCache[sni] = cert
	certMu.Unlock()
	return true
}

func writeAttestation(tag string, delegationID string) ([]byte, error) {
	started := time.Now()
	benchtrace.Mark(benchtrace.MiddleboxAttestationStart, tag, 0)
	benchtrace.Mark(benchtrace.MiddleboxAttestationStartByID, delegationID, 0)
	attestationType, err := os.ReadFile("/dev/attestation/attestation_type")
	if err != nil {
		benchtrace.Mark(benchtrace.MiddleboxAttestationDone, tag, 1)
		benchtrace.Mark(benchtrace.MiddleboxAttestationDoneByID, delegationID, 1)
		return nil, fmt.Errorf("failed to read /dev/attestation/attestation_type: %w", err)
	}
	debugf("attestation started delegation_id=%s type=%s", delegationID, strings.TrimSpace(string(attestationType)))

	// SGX REPORTDATA is 64 bytes.
	// For a first test, bind a fixed tag into the quote.
	// Later, replace this with a verifier challenge or a public-key hash.
	var reportData [64]byte
	sum := sha256.Sum256([]byte(tag))
	copy(reportData[:], sum[:])

	if err := os.WriteFile("/dev/attestation/user_report_data", reportData[:], 0); err != nil {
		benchtrace.Mark(benchtrace.MiddleboxAttestationDone, tag, 2)
		benchtrace.Mark(benchtrace.MiddleboxAttestationDoneByID, delegationID, 2)
		return nil, fmt.Errorf("failed to write /dev/attestation/user_report_data: %w", err)
	}

	quote, err := os.ReadFile("/dev/attestation/quote")
	if err != nil {
		benchtrace.Mark(benchtrace.MiddleboxAttestationDone, tag, 3)
		benchtrace.Mark(benchtrace.MiddleboxAttestationDoneByID, delegationID, 3)
		return nil, fmt.Errorf("failed to read /dev/attestation/quote: %w", err)
	}

	benchtrace.Mark(benchtrace.MiddleboxAttestationDone, tag, uint64(len(quote)))
	benchtrace.Mark(benchtrace.MiddleboxAttestationDoneByID, delegationID, uint64(len(quote)))
	debugf(
		"attestation quote generated delegation_id=%s bytes=%d generation_ms=%.3f",
		delegationID,
		len(quote),
		float64(time.Since(started).Nanoseconds())/1_000_000,
	)
	return quote, nil
}

func debugf(format string, args ...any) {
	if logLevel == "debug" {
		log.Printf("[OPERATOR] "+format, args...)
	}
}

func info(msg string) {
	if logLevel != "debug" || minimalLogs {
		return
	}
	fmt.Fprintln(os.Stderr, msg)
}

func loadCertPool(path string) (*x509.CertPool, error) {
	caPEM, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}

	pool := x509.NewCertPool()
	if !pool.AppendCertsFromPEM(caPEM) {
		return nil, fmt.Errorf("unable to parse CA certificate %s", path)
	}

	return pool, nil
}

func getEnvDefault(key string, fallback string) string {
	if value := strings.TrimSpace(os.Getenv(key)); value != "" {
		return value
	}
	return fallback
}

func clearDelegationState() {
	certMu.Lock()
	certCache = make(map[string]*tls.Certificate)
	certMu.Unlock()

	debugf("delegated credential state cleared")
}

func parseCertificateChain(certBytes []byte) ([][]byte, *x509.Certificate, error) {
	var certDERs [][]byte
	rest := certBytes
	for {
		var block *pem.Block
		block, rest = pem.Decode(rest)
		if block == nil {
			break
		}
		if block.Type != "CERTIFICATE" {
			continue
		}
		certDERs = append(certDERs, block.Bytes)
	}

	if len(certDERs) == 0 {
		if cert, err := x509.ParseCertificate(certBytes); err == nil {
			return [][]byte{cert.Raw}, cert, nil
		}
		return nil, nil, fmt.Errorf("no certificates found in server response")
	}

	leaf, err := x509.ParseCertificate(certDERs[0])
	if err != nil {
		return nil, nil, fmt.Errorf("failed to parse leaf certificate: %w", err)
	}

	return certDERs, leaf, nil
}

func parseDelegatedPrivateKey(keyPEM []byte) (any, error) {
	block, _ := pem.Decode(keyPEM)
	if block == nil {
		return nil, fmt.Errorf("invalid PEM delegated private key")
	}

	if priv, err := x509.ParsePKCS8PrivateKey(block.Bytes); err == nil {
		return priv, nil
	}
	if priv, err := x509.ParsePKCS1PrivateKey(block.Bytes); err == nil {
		return priv, nil
	}
	if priv, err := x509.ParseECPrivateKey(block.Bytes); err == nil {
		return priv, nil
	}

	return nil, fmt.Errorf("unable to parse delegated private key")
}

func fetchDelegationMaterial(sni string, delegationID string, connectionKey uint64) (_ *delegationMaterial, resultErr error) {
	benchtrace.Mark(benchtrace.MiddleboxDelegationFetch, sni, 0)
	benchtrace.Mark(benchtrace.MiddleboxDelegationFetchByID, delegationID, 0)
	if connectionKey != 0 {
		benchtrace.Mark(benchtrace.MiddleboxDelegationConnectionBind, delegationID, connectionKey)
	}
	defer func() {
		arg := uint64(0)
		if resultErr != nil {
			arg = 1
		}
		benchtrace.Mark(benchtrace.MiddleboxDelegationFetchedByID, delegationID, arg)
	}()
	payload := map[string]string{"sni": sni, "delegation_id": delegationID}

	if os.Getenv("MBX_EMIT_QUOTE") == "1" {
		att, err := writeAttestation("middlebox-attestation-test", delegationID)
		if err != nil {
			fmt.Fprintf(os.Stderr, "attestation failed: %v\n", err)
			os.Exit(1)
		}
		payload["quote"] = base64.StdEncoding.EncodeToString(att)
	} else {
		debugf("attestation disabled; requesting delegation without quote delegation_id=%s", delegationID)
	}
	body, err := json.Marshal(payload)
	if err != nil {
		benchtrace.Mark(benchtrace.MiddleboxDelegationFetched, sni, 1)
		return nil, err
	}

	resp, err := http.Post(operatorCertURL+"/certs", "application/json", bytes.NewReader(body))
	if err != nil {
		benchtrace.Mark(benchtrace.MiddleboxDelegationFetched, sni, 2)
		return nil, fmt.Errorf("failed contacting server: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		b, _ := io.ReadAll(resp.Body)
		benchtrace.Mark(benchtrace.MiddleboxDelegationFetched, sni, uint64(resp.StatusCode))
		return nil, fmt.Errorf("server returned %d: %s", resp.StatusCode, string(b))
	}

	var data certResponse
	if err := json.NewDecoder(resp.Body).Decode(&data); err != nil {
		benchtrace.Mark(benchtrace.MiddleboxDelegationFetched, sni, 3)
		return nil, err
	}

	if data.DCCredB64 == "" || data.DCKeyB64 == "" {
		benchtrace.Mark(benchtrace.MiddleboxDelegationFetched, sni, 4)
		return nil, fmt.Errorf("invalid certificate response from server")
	}
	debugf("delegation response received delegation_id=%s", delegationID)

	certBytes, err := base64.StdEncoding.DecodeString(data.CertB64)
	if err != nil {
		benchtrace.Mark(benchtrace.MiddleboxDelegationFetched, sni, 5)
		return nil, err
	}

	dcBytes, err := base64.StdEncoding.DecodeString(data.DCCredB64)
	if err != nil {
		benchtrace.Mark(benchtrace.MiddleboxDelegationFetched, sni, 6)
		return nil, err
	}

	dcKeyBytes, err := base64.StdEncoding.DecodeString(data.DCKeyB64)
	if err != nil {
		benchtrace.Mark(benchtrace.MiddleboxDelegationFetched, sni, 7)
		return nil, err
	}

	certDERs, leaf, err := parseCertificateChain(certBytes)
	if err != nil {
		benchtrace.Mark(benchtrace.MiddleboxDelegationFetched, sni, 8)
		return nil, err
	}

	dc, err := tls.UnmarshalDelegatedCredential(dcBytes)
	if err != nil {
		benchtrace.Mark(benchtrace.MiddleboxDelegationFetched, sni, 9)
		return nil, fmt.Errorf("failed to unmarshal delegated credential: %w", err)
	}

	priv, err := parseDelegatedPrivateKey(dcKeyBytes)
	if err != nil {
		benchtrace.Mark(benchtrace.MiddleboxDelegationFetched, sni, 10)
		return nil, err
	}

	baseCert := tls.Certificate{
		Certificate: certDERs,
		Leaf:        leaf,
	}
	baseCert.DelegatedCredentials = append(
		baseCert.DelegatedCredentials,
		tls.DelegatedCredentialPair{DC: dc, PrivateKey: priv},
	)

	benchtrace.Mark(benchtrace.MiddleboxDelegationFetched, sni, 0)
	return &delegationMaterial{cert: baseCert}, nil
}

func getOrFetchCertificate(chi *tls.ClientHelloInfo) (*tls.Certificate, error) {
	info(fmt.Sprintf("t29: [OPERATOR] - gateway_to_operator_received = %d ns", time.Now().UnixNano()))
	info(fmt.Sprintf("t2: [OPERATOR] - ClientHelloLatency = %d ns", time.Now().UnixNano()))

	sni := chi.ServerName
	if sni == "" {
		sni = expectedSNI
	}

	if sni != expectedSNI {
		return nil, fmt.Errorf("unexpected SNI %q: expected %q", sni, expectedSNI)
	}

	info(fmt.Sprintf("t3: [OPERATOR] - ClientHello = %d ns", time.Now().UnixNano()))

	if reuseDC {
		certMu.RLock()
		cert, ok := certCache[sni]
		certMu.RUnlock()

		if ok && cert != nil {
			benchtrace.Mark(benchtrace.MiddleboxDelegationCacheHit, sni, 0)
			debugf("reuse_dc=true: using cached delegated credential")
			info(fmt.Sprintf("t9: [OPERATOR] - ServerHello = %d ns", time.Now().UnixNano()))
			return cert, nil
		}

		// Serializza il primo fetch: evita che handshake concorrenti iniziali
		// facciano due richieste /certs quando la cache e' ancora vuota.
		delegationFetchMu.Lock()
		defer delegationFetchMu.Unlock()

		// Double-check dopo aver acquisito il lock: un altro handshake potrebbe
		// aver gia' popolato la cache mentre aspettavamo.
		certMu.RLock()
		cert, ok = certCache[sni]
		certMu.RUnlock()

		if ok && cert != nil {
			benchtrace.Mark(benchtrace.MiddleboxDelegationCacheHit, sni, 0)
			debugf("reuse_dc=true: using cached delegated credential")
			info(fmt.Sprintf("t9: [OPERATOR] - ServerHello = %d ns", time.Now().UnixNano()))
			return cert, nil
		}
	} else {
		debugf("reuse_dc=false: forcing fresh delegated credential")
	}

	benchtrace.Mark(benchtrace.MiddleboxDelegationMiss, sni, 0)
	connectionKey, _ := chi.Context().Value(connectionKeyContextKey{}).(uint64)
	material, err := fetchDelegationMaterial(sni, nextDelegationID(), connectionKey)
	if err != nil {
		return nil, err
	}

	cert := &material.cert
	if reuseDC {
		cacheFetchedDelegation(sni, cert)
	}

	info(fmt.Sprintf("t8: [OPERATOR] - CertsToMLatency = %d ns", time.Now().UnixNano()))
	info(fmt.Sprintf("t9: [OPERATOR] - ServerHello = %d ns", time.Now().UnixNano()))

	return cert, nil
}

func assignHandler(st *operatorState) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
			return
		}

		if st.consumed.Load() {
			http.Error(w, "operator consumed", http.StatusGone)
			return
		}

		if !st.ready.Load() {
			http.Error(w, "operator not ready", http.StatusServiceUnavailable)
			return
		}

		if st.busy.Load() {
			http.Error(w, "operator busy", http.StatusConflict)
			return
		}

		st.assigned.Add(1)
		info(fmt.Sprintf("t36: [OPERATOR] - assigned_by_gateway = %d ns", time.Now().UnixNano()))

		w.WriteHeader(http.StatusAccepted)
		_, _ = w.Write([]byte("assigned"))
	}
}

func readinessHandler(st *operatorState) http.HandlerFunc {
	return func(w http.ResponseWriter, _ *http.Request) {
		if st.consumed.Load() || !st.ready.Load() || st.busy.Load() {
			http.Error(w, "not-ready", http.StatusServiceUnavailable)
			return
		}

		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("ready"))
	}
}

func healthHandler(st *operatorState) http.HandlerFunc {
	return func(w http.ResponseWriter, _ *http.Request) {
		payload := map[string]any{
			"id":               st.id,
			"mode":             st.mode,
			"ready":            st.ready.Load(),
			"delegation_ready": st.delegationReady.Load(),
			"busy":             st.busy.Load(),
			"consumed":         st.consumed.Load(),
			"assigned_total":   st.assigned.Load(),
			"requests_total":   st.totalReq.Load(),
			"reuse_dc":         reuseDC,
		}

		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(payload)
	}
}

// TODO: Avoid starting if in SGX
func startHTTPStateServer(st *operatorState) {
	mux := http.NewServeMux()
	mux.HandleFunc("/health", healthHandler(st))
	mux.HandleFunc("/ready", readinessHandler(st))
	mux.HandleFunc("/assign", assignHandler(st))

	go func() {
		debugf("state server listening on %s", operatorHTTPAddr)
		if err := http.ListenAndServe(operatorHTTPAddr, mux); err != nil {
			log.Fatalf("state server failed: %v", err)
		}
	}()
}

func prefetchIfNeeded(st *operatorState, defaultSNI string) {
	if st.mode != "auth" {
		st.ready.Store(true)
		st.delegationReady.Store(false)

		info(fmt.Sprintf("t35: [OPERATOR] - ready_for_assignment = %d ns", time.Now().UnixNano()))
		debugf("ready=true delegation_ready=false mode=warm")
		return
	}

	if !reuseDC {
		st.ready.Store(true)
		st.delegationReady.Store(false)

		info(fmt.Sprintf("t35: [OPERATOR] - ready_for_assignment = %d ns", time.Now().UnixNano()))
		debugf("auth prefetch skipped because reuse_dc=false")
		return
	}

	if defaultSNI == "" {
		defaultSNI = expectedSNI
	}

	material, err := fetchDelegationMaterial(defaultSNI, nextDelegationID(), 0)
	if err != nil {
		log.Printf("[OPERATOR] auth prefetch failed: %v", err)
		return
	}

	if !cacheFetchedDelegation(defaultSNI, &material.cert) {
		st.ready.Store(true)
		st.delegationReady.Store(false)

		info(fmt.Sprintf("t35: [OPERATOR] - ready_for_assignment = %d ns", time.Now().UnixNano()))
		debugf("auth prefetch discarded first delegated credential")
		return
	}

	st.ready.Store(true)
	st.delegationReady.Store(true)

	info(fmt.Sprintf("t35: [OPERATOR] - ready_for_assignment = %d ns", time.Now().UnixNano()))
	debugf("ready=true delegation_ready=true mode=auth-prefetch")
}

func main() {
	reuseDCFlag := flag.Bool("reuse_dc", true, "reuse delegated credentials")
	operatorModeFlag := flag.String("operator_mode", "warm", "operator mode: warm/auth")
	operatorIDFlag := flag.String("operator_id", "operator", "operator identifier")
	defaultSNIFlag := flag.String("operator_default_sni", expectedSNI, "default SNI for prefetch")
	caPathFlag := flag.String("ca", defaultCA, "CA certificate for upstream server verification")
	consumeAfterRequestFlag := flag.Bool("consume_after_request", false, "consume operator after one request")
	exitAfterRequestFlag := flag.Bool("exit_after_request", false, "exit process after one request")
	minimalLogsFlag := flag.Bool("minimal_logs", true, "suppress legacy text timestamp logs")
	logLevelFlag := flag.String("log_level", "error", "log level: debug/error")
	tracePathFlag := flag.String("trace", "", "trace output file; requires build tag trace")
	traceBufferFlag := flag.Int("trace-buffer-events", 100000, "trace buffer capacity in events")
	traceDropFlag := flag.Bool("trace-drop-on-full", true, "drop trace events instead of blocking when trace buffer is full")
	//TODO: Add: 1) conditional build to keep only minimal setup in SGX case;
	flag.Parse()

	if err := benchtrace.Start(*tracePathFlag, *traceBufferFlag, *traceDropFlag); err != nil {
		log.Fatalf("trace start: %v", err)
	}
	defer func() {
		fmt.Fprintln(os.Stderr, "[OPERATOR] trace stop start")
		benchtrace.Stop()
		fmt.Fprintln(os.Stderr, "[OPERATOR] trace stop done")
	}()

	reuseDC = *reuseDCFlag
	minimalLogs = *minimalLogsFlag
	logLevel = strings.ToLower(strings.TrimSpace(*logLevelFlag))
	if logLevel != "error" && logLevel != "debug" {
		log.Fatalf("invalid -log_level %q: expected error or debug", *logLevelFlag)
	}
	operatorTarget = getEnvDefault("OPERATOR_TARGET", defaultOperatorTarget)
	operatorCertURL = getEnvDefault("OPERATOR_CERT_URL", defaultOperatorCertURL)

	info(fmt.Sprintf("t34: [OPERATOR] - process_start = %d ns", time.Now().UnixNano()))
	debugf("reuse_dc=%v", reuseDC)

	mode := strings.ToLower(*operatorModeFlag)
	operatorID := *operatorIDFlag
	operatorTraceID = operatorID
	defaultSNI := *defaultSNIFlag
	ticketStore := newTicketSessionStore(os.Getenv("DCMB_TICKET_IDENTITY_KEY"), operatorID)

	benchtrace.Mark(benchtrace.MiddleboxSchemaCompileStart, operatorID, 0)
	if err := initializeValidation(); err != nil {
		benchtrace.Mark(benchtrace.MiddleboxSchemaCompileDone, operatorID, 1)
		log.Fatalf("validation initialization failed: %v", err)
	}
	benchtrace.Mark(benchtrace.MiddleboxSchemaCompileDone, operatorID, 0)

	st := &operatorState{
		id:               operatorID,
		mode:             mode,
		consumeOnce:      *consumeAfterRequestFlag,
		exitAfterRequest: *exitAfterRequestFlag,
	}

	startHTTPStateServer(st)
	prefetchIfNeeded(st, defaultSNI)

	upstreamRoots, err := loadCertPool(*caPathFlag)
	if err != nil {
		log.Fatalf("failed to load upstream CA %s: %v", *caPathFlag, err)
	}

	//sets the target server to forward requests to
	remote, err := url.Parse(operatorTarget)
	if err != nil {
		log.Fatal(err)
	}

	upstreamSessions, err := newUpstreamSessionManager(remote, upstreamRoots)
	if err != nil {
		log.Fatal(err)
	}
	defer upstreamSessions.closeAll()

	// Initializes the reverse proxy with one upstream HTTP/1.1 connection per
	// downstream TLS session.
	proxy := httputil.NewSingleHostReverseProxy(remote)
	proxy.Transport = upstreamSessions

	proxy.ErrorHandler = func(w http.ResponseWriter, r *http.Request, err error) {
		traceID := r.Header.Get("X-Trace-ID")
		if traceID == "" {
			traceID = st.id
		}
		benchtrace.Mark(benchtrace.MiddleboxProxyError, traceID, 1)
		log.Printf("[OPERATOR] upstream error trace_id=%s: %v", traceID, err)
		http.Error(w, "bad gateway", http.StatusBadGateway)
	}

	proxy.ModifyResponse = func(resp *http.Response) error {
		traceID := resp.Request.Header.Get("X-Trace-ID")
		if traceID == "" {
			traceID = st.id
		}

		result, ok := resp.Request.Context().Value(validationResultContextKey{}).(validationResult)
		if !ok {
			return nil
		}

		benchtrace.Mark(benchtrace.MiddleboxResponseValidationStart, traceID, 0)
		processResponse(resp, result.user, result.messageType)
		benchtrace.Mark(benchtrace.MiddleboxResponseValidationDone, traceID, 0)
		return nil
	}

	handler := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		traceID := r.Header.Get("X-Trace-ID")
		if traceID == "" {
			traceID = st.id
		}
		connectionKey, _ := r.Context().Value(connectionKeyContextKey{}).(uint64)
		if connectionKey != 0 {
			benchtrace.Mark(benchtrace.MiddleboxTraceConnectionBind, traceID, connectionKey)
		}
		tracedWriter := &traceResponseWriter{ResponseWriter: w, traceID: traceID}
		w = tracedWriter
		defer benchtrace.Mark(benchtrace.MiddleboxDownstreamResponseDone, traceID, 0)
		benchtrace.Mark(benchtrace.MiddleboxRequestStart, traceID, 0)
		benchtrace.Mark(benchtrace.MiddleboxValidationStart, traceID, 0)
		valid, user, messageType := processRequest(r)
		if !valid {
			benchtrace.Mark(benchtrace.MiddleboxValidationDone, traceID, 1)
			benchtrace.Mark(benchtrace.MiddleboxRequestDone, traceID, 1)
			log.Printf("[OPERATOR] request validation failed trace_id=%s method=%s path=%s", traceID, r.Method, r.URL.Path)
			http.Error(w, "forbidden", http.StatusForbidden)
			return
		}
		benchtrace.Mark(benchtrace.MiddleboxValidationDone, traceID, 0)
		// if st.consumed.Load() {
		// 	http.Error(w, "operator consumed", http.StatusServiceUnavailable)
		// 	return
		// }
		//TODO: make it handle multiple requests in parallel, if from different clients.
		// if !st.ready.Load() {
		// 	http.Error(w, "operator not ready", http.StatusServiceUnavailable)
		// 	return
		// }

		// if st.busy.Swap(true) {
		// 	http.Error(w, "operator busy", http.StatusTooManyRequests)
		// 	return
		// }

		defer st.busy.Store(false)

		st.totalReq.Add(1)

		if !st.delegationReady.Load() {
			debugf("request arrived while delegation_ready=false; relying on handshake delegation")
		} else {
			debugf("serving with delegated credential already available")
		}

		r = r.WithContext(context.WithValue(r.Context(), traceIDContextKey{}, traceID))
		r = r.WithContext(context.WithValue(r.Context(), validationResultContextKey{}, validationResult{
			user:        user,
			messageType: messageType,
		}))
		r = r.WithContext(httptrace.WithClientTrace(r.Context(), &httptrace.ClientTrace{
			WroteRequest: func(info httptrace.WroteRequestInfo) {
				benchtrace.Mark(benchtrace.MiddleboxUpstreamRequestSent, traceID, errorArg(info.Err))
			},
			GotFirstResponseByte: func() {
				benchtrace.Mark(benchtrace.MiddleboxUpstreamResponseFirst, traceID, 0)
			},
		}))
		proxy.ServeHTTP(w, r)
		benchtrace.Mark(benchtrace.MiddleboxRequestDone, traceID, 0)

		if !reuseDC {
			debugf("reuse_dc=false: delegated credential was session-local")
			st.delegationReady.Store(false)
		}

		if st.consumeOnce {
			st.consumed.Store(true)
			st.ready.Store(false)
		}

		if st.exitAfterRequest && !ticketStore.ticketIssued() {
			debugf("single-use mode: exiting after request")
			go func() {
				time.Sleep(50 * time.Millisecond)
				os.Exit(0)
			}()
		} else if st.exitAfterRequest {
			debugf("ticket issued: staying warm for resumption")
		}
	})

	tlsConfig := &tls.Config{
		MinVersion: tls.VersionTLS13,
		GetConfigForClient: func(chi *tls.ClientHelloInfo) (*tls.Config, error) {
			if err := upstreamSessions.prepareClientHello(chi); err != nil {
				return nil, fmt.Errorf("prepare upstream session: %w", err)
			}
			return nil, nil
		},
		// SupportDelegatedCredential: true,
		GetCertificate: func(chi *tls.ClientHelloInfo) (*tls.Certificate, error) {
			sni := chi.ServerName
			if sni == "" {
				sni = expectedSNI
			}
			benchtrace.Mark(benchtrace.MiddleboxTLSGetCertStart, sni, 0)
			if !st.delegationReady.Load() {
				debugf("delegation/auth with server: start")
			}

			cert, err := getOrFetchCertificate(chi)
			if err != nil {
				benchtrace.Mark(benchtrace.MiddleboxTLSGetCertDone, sni, 1)
				return nil, err
			}

			if !st.delegationReady.Load() {
				st.delegationReady.Store(true)
				debugf("delegation/auth with server: done")
			}

			benchtrace.Mark(benchtrace.MiddleboxTLSGetCertDone, sni, 0)
			return cert, nil
		},
	}
	if ticketStore != nil {
		tlsConfig.WrapSession = ticketStore.wrapSession
		tlsConfig.UnwrapSession = ticketStore.unwrapSession
		debugf("deterministic TLS ticket identity enabled")
	}

	srv := &http.Server{
		Addr:      operatorTLSAddr,
		Handler:   handler,
		TLSConfig: tlsConfig,
		ConnContext: func(ctx context.Context, conn net.Conn) context.Context {
			connectionKey := tracebind.ConnectionKey(conn.LocalAddr(), conn.RemoteAddr())
			connectionID := fmt.Sprintf("%s-conn-%d", st.id, connectionCounter.Add(1))
			benchtrace.Mark(benchtrace.MiddleboxConnectionAccepted, connectionID, connectionKey)
			session := upstreamSessions.accept(connectionKey, connectionID)
			ctx = context.WithValue(ctx, connectionKeyContextKey{}, connectionKey)
			return context.WithValue(ctx, upstreamSessionContextKey{}, session)
		},
		ConnState: func(conn net.Conn, state http.ConnState) {
			if state == http.StateClosed || state == http.StateHijacked {
				upstreamSessions.closeConnection(conn)
			}
		},
	}

	ln, err := net.Listen("tcp", operatorTLSAddr)
	if err != nil {
		log.Fatal(err)
	}
	shutdownCtx, stopSignals := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stopSignals()

	debugf("id=%s mode=%s listening=%s", operatorID, mode, operatorTLSAddr)
	fmt.Fprintf(os.Stderr, "[OPERATOR_READY] listening=%s mode=%s id=%s\n", operatorTLSAddr, mode, operatorID)

	go func() {
		<-shutdownCtx.Done()
		fmt.Fprintln(os.Stderr, "[OPERATOR] shutdown signal received")

		ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
		defer cancel()
		if err := srv.Shutdown(ctx); err != nil {
			log.Printf("[OPERATOR] graceful shutdown failed: %v", err)
			_ = srv.Close()
		}
	}()

	if err := srv.ServeTLS(ln, "", ""); err != nil && !errors.Is(err, http.ErrServerClosed) {
		log.Fatal(err)
	}
}
