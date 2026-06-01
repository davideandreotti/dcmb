//middlebox_operator
package main

import (
	"bytes"
	"context"
	"crypto/tls"
	"crypto/x509"
	"encoding/base64"
	"encoding/json"
	"encoding/pem"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"net/http/httputil"
	"net/url"
	"os"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

const (
	operatorTLSAddr  = ":9443"
	operatorHTTPAddr = ":18080"
	operatorTarget   = "https://server:8000"
	operatorCertURL  = "http://server:5000"

	expectedSNI      = "server" // SNI that must match the client hello; derived from operatorTarget hostname
)

type certResponse struct {
	CertB64   string `json:"cert_b64"`
	KeyB64    string `json:"key_b64"`
	DCCredB64 string `json:"dc_cred_b64"`
	DCKeyB64  string `json:"dc_key_b64"`
}

type operatorState struct {
	id               string
	mode             string
	ready            atomic.Bool
	authenticated    atomic.Bool
	busy             atomic.Bool
	assigned         atomic.Int64
	totalReq         atomic.Int64
	consumed         atomic.Bool
	consumeOnce      bool
	exitAfterRequest bool
}

var (
	certCache          = make(map[string]*tls.Certificate)
	certMu             sync.RWMutex
	upstreamCertPool   *x509.CertPool
	upstreamCertPoolMu sync.RWMutex
	minimalLogs        = true
)

func info(msg string) {
	if minimalLogs && !strings.HasPrefix(msg, "t") {
		return
	}
	fmt.Fprintln(os.Stderr, msg)
}

func getEnv(key string, fallback string) string {
	if value := strings.TrimSpace(os.Getenv(key)); value != "" {
		return value
	}
	return fallback
}

func getEnvBool(key string, fallback bool) bool {
	value := strings.TrimSpace(strings.ToLower(os.Getenv(key)))
	if value == "" {
		return fallback
	}
	parsed, err := strconv.ParseBool(value)
	if err != nil {
		return fallback
	}
	return parsed
}

func requestDelegatedCertificatesFromServer(sni string) (*tls.Certificate, error) {
	payload := map[string]string{"sni": sni}
	body, err := json.Marshal(payload)
	if err != nil {
		return nil, err
	}

	resp, err := http.Post(operatorCertURL+"/certs", "application/json", bytes.NewReader(body))
	if err != nil {
		return nil, fmt.Errorf("failed contacting server: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		b, _ := io.ReadAll(resp.Body)
		return nil, fmt.Errorf("server returned %d: %s", resp.StatusCode, string(b))
	}

	var data certResponse
	if err := json.NewDecoder(resp.Body).Decode(&data); err != nil {
		return nil, err
	}
	if data.DCCredB64 == "" || data.DCKeyB64 == "" {
		return nil, fmt.Errorf("invalid certificate response from server")
	}

	certBytes, err := base64.StdEncoding.DecodeString(data.CertB64)
	if err != nil {
		return nil, err
	}
	keyBytes, err := base64.StdEncoding.DecodeString(data.KeyB64)
	if err != nil {
		return nil, err
	}

	dcBytes, err := base64.StdEncoding.DecodeString(data.DCCredB64)
	if err != nil {
		return nil, err
	}
	dcKeyBytes, err := base64.StdEncoding.DecodeString(data.DCKeyB64)
	if err != nil {
		return nil, err
	}

	baseCert, err := tls.X509KeyPair(certBytes, keyBytes)
	if err != nil {
		return nil, fmt.Errorf("failed to parse base cert/key: %w", err)
	}

	dc, err := tls.UnmarshalDelegatedCredential(dcBytes)
	if err != nil {
		return nil, fmt.Errorf("failed to unmarshal delegated credential: %w", err)
	}

	block, _ := pem.Decode(dcKeyBytes)
	if block == nil {
		return nil, fmt.Errorf("invalid PEM delegated private key")
	}
	priv, err := x509.ParsePKCS8PrivateKey(block.Bytes)
	if err != nil {
		priv, err = x509.ParsePKCS1PrivateKey(block.Bytes)
		if err != nil {
			return nil, fmt.Errorf("unable to parse delegated private key: %w", err)
		}
	}

	baseCert.DelegatedCredentials = append(
		baseCert.DelegatedCredentials,
		tls.DelegatedCredentialPair{dc, priv},
	)

	pool := x509.NewCertPool()
	pool.AppendCertsFromPEM(certBytes)
	upstreamCertPoolMu.Lock()
	upstreamCertPool = pool
	upstreamCertPoolMu.Unlock()

	certMu.Lock()
	certCache[sni] = &baseCert
	certMu.Unlock()

	return &baseCert, nil
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

	certMu.RLock()
	cert, ok := certCache[sni]
	certMu.RUnlock()
	if ok && cert != nil {
		info(fmt.Sprintf("t9: [OPERATOR] - ServerHello = %d ns", time.Now().UnixNano()))
		return cert, nil
	}

	fetched, err := requestDelegatedCertificatesFromServer(sni)
	if err != nil {
		return nil, err
	}

	info(fmt.Sprintf("t8: [OPERATOR] - CertsToMLatency = %d ns", time.Now().UnixNano()))
	info(fmt.Sprintf("t9: [OPERATOR] - ServerHello = %d ns", time.Now().UnixNano()))
	return fetched, nil
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
			"id":             st.id,
			"mode":           st.mode,
			"ready":          st.ready.Load(),
			"authenticated":  st.authenticated.Load(),
			"busy":           st.busy.Load(),
			"consumed":       st.consumed.Load(),
			"assigned_total": st.assigned.Load(),
			"requests_total": st.totalReq.Load(),
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(payload)
	}
}

func startHTTPStateServer(st *operatorState) {
	mux := http.NewServeMux()
	mux.HandleFunc("/health", healthHandler(st))
	mux.HandleFunc("/ready", readinessHandler(st))
	mux.HandleFunc("/assign", assignHandler(st))

	go func() {
		info("[OPERATOR] state server listening on " + operatorHTTPAddr)
		if err := http.ListenAndServe(operatorHTTPAddr, mux); err != nil {
			log.Fatalf("state server failed: %v", err)
		}
	}()
}

func prefetchIfNeeded(st *operatorState, defaultSNI string) {
	if st.mode != "auth" {
		// Warm semantics: operator is schedulable but still unauthenticated.
		st.ready.Store(true)
		st.authenticated.Store(false)
		info(fmt.Sprintf("t35: [OPERATOR] - ready_for_assignment = %d ns", time.Now().UnixNano()))
		info("[OPERATOR] ready=true authenticated=false (warm)")
		return
	}
	if defaultSNI == "" {
		defaultSNI = "server"
	}
	if _, err := requestDelegatedCertificatesFromServer(defaultSNI); err != nil {
		info(fmt.Sprintf("[OPERATOR] auth prefetch failed: %v", err))
		return
	}
	st.ready.Store(true)
	st.authenticated.Store(true)
	info(fmt.Sprintf("t35: [OPERATOR] - ready_for_assignment = %d ns", time.Now().UnixNano()))
	info("[OPERATOR] ready=true authenticated=true (auth-prefetch)")
}

func main() {
	info(fmt.Sprintf("t34: [OPERATOR] - process_start = %d ns", time.Now().UnixNano()))
	mode := strings.ToLower(getEnv("OPERATOR_MODE", "warm"))
	operatorID := getEnv("OPERATOR_ID", "operator")
	defaultSNI := getEnv("OPERATOR_DEFAULT_SNI", "server")
	consumeAfterRequest := getEnvBool("OPERATOR_CONSUME_AFTER_REQUEST", false)
	exitAfterRequest := getEnvBool("OPERATOR_EXIT_AFTER_REQUEST", false)
	minimalLogs = getEnvBool("MB_MINIMAL_LOGS", true)

	st := &operatorState{
		id:               operatorID,
		mode:             mode,
		consumeOnce:      consumeAfterRequest,
		exitAfterRequest: exitAfterRequest,
	}

	startHTTPStateServer(st)
	prefetchIfNeeded(st, defaultSNI)

	remote, err := url.Parse(operatorTarget)
	if err != nil {
		log.Fatal(err)
	}

	proxy := httputil.NewSingleHostReverseProxy(remote)
	proxy.Transport = &http.Transport{
		DialTLSContext: func(ctx context.Context, network, addr string) (net.Conn, error) {
			upstreamCertPoolMu.RLock()
			pool := upstreamCertPool
			upstreamCertPoolMu.RUnlock()
			return tls.Dial(network, addr, &tls.Config{
				RootCAs:    pool,
				ServerName: "server",
			})
		},
	}

	proxy.ErrorHandler = func(w http.ResponseWriter, r *http.Request, err error) {
		info(fmt.Sprintf("[OPERATOR] upstream error: %v", err))
		http.Error(w, "bad gateway", http.StatusBadGateway)
	}

	handler := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if st.consumed.Load() {
			http.Error(w, "operator consumed", http.StatusServiceUnavailable)
			return
		}
		if !st.ready.Load() {
			http.Error(w, "operator not ready", http.StatusServiceUnavailable)
			return
		}
		if st.busy.Swap(true) {
			http.Error(w, "operator busy", http.StatusTooManyRequests)
			return
		}
		defer st.busy.Store(false)

		st.totalReq.Add(1)

		if !st.authenticated.Load() {
			// First successful delegated fetch happens during TLS handshake
			// (GetCertificate path) before this handler is reached.
			info("[OPERATOR] request arrived while authenticated=false; relying on handshake delegation")
		} else {
			info("[OPERATOR] serving with delegated credential already available")
		}
		proxy.ServeHTTP(w, r)

		if st.consumeOnce {
			st.consumed.Store(true)
			st.ready.Store(false)
		}

		if st.exitAfterRequest {
			info("[OPERATOR] single-use mode: exiting after request")
			go func() {
				time.Sleep(50 * time.Millisecond)
				os.Exit(0)
			}()
		}
	})

	tlsConfig := &tls.Config{GetCertificate: getOrFetchCertificate}
	tlsConfig.GetCertificate = func(chi *tls.ClientHelloInfo) (*tls.Certificate, error) {
		if !st.authenticated.Load() {
			info("[OPERATOR] delegation/auth with server: START")
		}
		cert, err := getOrFetchCertificate(chi)
		if err != nil {
			return nil, err
		}
		if !st.authenticated.Load() {
			st.authenticated.Store(true)
			info("[OPERATOR] delegation/auth with server: DONE")
		}
		return cert, nil
	}
	srv := &http.Server{Addr: operatorTLSAddr, Handler: handler, TLSConfig: tlsConfig}

	info(fmt.Sprintf("[OPERATOR] %s mode=%s listening on %s", operatorID, mode, operatorTLSAddr))
	log.Fatal(srv.ListenAndServeTLS("", ""))
}
