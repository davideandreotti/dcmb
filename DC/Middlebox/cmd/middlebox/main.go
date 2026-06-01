// middlebox.go
package main

import (
	"bytes"
	"context"
	"crypto/tls"
	"crypto/x509"
	"encoding/base64"
	"encoding/json"
	"encoding/pem"
	"flag"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"net/http/httputil"
	"net/url"
	"os"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

const (
	operatorTLSAddr  = ":8443"
	operatorHTTPAddr = ":18080"
	operatorTarget   = "https://server:8000"
	operatorCertURL  = "http://server:5000"

	expectedSNI = "server"
	defaultCA   = "/home/bonsai/Desktop/MasterThesis/certs_external/ca.crt"
)

var (
	reuseDC     bool
	minimalLogs bool
	logLevel    string
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

var (
	certCache         = make(map[string]*tls.Certificate)
	certMu            sync.RWMutex
	delegationFetchMu sync.Mutex
)

func info(msg string) {
	if (minimalLogs && !strings.HasPrefix(msg, "t")) || (logLevel != "debug") {
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

func clearDelegationState() {
	certMu.Lock()
	certCache = make(map[string]*tls.Certificate)
	certMu.Unlock()

	info("[OPERATOR] Delegated credential state cleared")
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

func fetchDelegationMaterial(sni string) (*delegationMaterial, error) {
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

	dcBytes, err := base64.StdEncoding.DecodeString(data.DCCredB64)
	if err != nil {
		return nil, err
	}

	dcKeyBytes, err := base64.StdEncoding.DecodeString(data.DCKeyB64)
	if err != nil {
		return nil, err
	}

	certDERs, leaf, err := parseCertificateChain(certBytes)
	if err != nil {
		return nil, err
	}

	dc, err := tls.UnmarshalDelegatedCredential(dcBytes)
	if err != nil {
		return nil, fmt.Errorf("failed to unmarshal delegated credential: %w", err)
	}

	priv, err := parseDelegatedPrivateKey(dcKeyBytes)
	if err != nil {
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
			info("[OPERATOR] reuse_dc=true: using cached delegated credential")
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
			info("[OPERATOR] reuse_dc=true: using cached delegated credential")
			info(fmt.Sprintf("t9: [OPERATOR] - ServerHello = %d ns", time.Now().UnixNano()))
			return cert, nil
		}
	} else {
		info("[OPERATOR] reuse_dc=false: forcing fresh delegated credential")
	}

	material, err := fetchDelegationMaterial(sni)
	if err != nil {
		return nil, err
	}

	cert := &material.cert
	if reuseDC {
		certMu.Lock()
		certCache[sni] = cert
		certMu.Unlock()
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
		info("[OPERATOR] state server listening on " + operatorHTTPAddr)
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
		info("[OPERATOR] ready=true delegation_ready=false (warm)")
		return
	}

	if !reuseDC {
		st.ready.Store(true)
		st.delegationReady.Store(false)

		info(fmt.Sprintf("t35: [OPERATOR] - ready_for_assignment = %d ns", time.Now().UnixNano()))
		info("[OPERATOR] auth prefetch skipped because reuse_dc=false")
		return
	}

	if defaultSNI == "" {
		defaultSNI = expectedSNI
	}

	material, err := fetchDelegationMaterial(defaultSNI)
	if err != nil {
		info(fmt.Sprintf("[OPERATOR] auth prefetch failed: %v", err))
		return
	}

	certMu.Lock()
	certCache[defaultSNI] = &material.cert
	certMu.Unlock()

	st.ready.Store(true)
	st.delegationReady.Store(true)

	info(fmt.Sprintf("t35: [OPERATOR] - ready_for_assignment = %d ns", time.Now().UnixNano()))
	info("[OPERATOR] ready=true delegation_ready=true (auth-prefetch)")
}

func main() {
	reuseDCFlag := flag.Bool("reuse_dc", true, "reuse delegated credentials")
	operatorModeFlag := flag.String("operator_mode", "warm", "operator mode: warm/auth")
	operatorIDFlag := flag.String("operator_id", "operator", "operator identifier")
	defaultSNIFlag := flag.String("operator_default_sni", expectedSNI, "default SNI for prefetch")
	caPathFlag := flag.String("ca", defaultCA, "CA certificate for upstream server verification")
	consumeAfterRequestFlag := flag.Bool("consume_after_request", false, "consume operator after one request")
	exitAfterRequestFlag := flag.Bool("exit_after_request", false, "exit process after one request")
	minimalLogsFlag := flag.Bool("minimal_logs", true, "print only timestamp logs")
	logLevelFlag := flag.String("log_level", "error", "log level: debug/error")
	//TODO: Add: 1) conditional build to keep only minimal setup in SGX case;
	flag.Parse()

	reuseDC = *reuseDCFlag
	minimalLogs = *minimalLogsFlag
	logLevel = *logLevelFlag

	info(fmt.Sprintf("t34: [OPERATOR] - process_start = %d ns", time.Now().UnixNano()))
	info(fmt.Sprintf("[OPERATOR] reuse_dc=%v", reuseDC))

	mode := strings.ToLower(*operatorModeFlag)
	operatorID := *operatorIDFlag
	defaultSNI := *defaultSNIFlag

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

	//initializes the referse proxy with custom TLS transport
	proxy := httputil.NewSingleHostReverseProxy(remote)

	//custom TLS transport
	proxy.Transport = &http.Transport{
		//custom TLS dialer
		DialTLSContext: func(ctx context.Context, network, addr string) (net.Conn, error) {

			dialer := &net.Dialer{
				Timeout:   3 * time.Second,
				KeepAlive: 30 * time.Second,
			}

			// opens the TCP connection to the upstream server
			conn, err := dialer.DialContext(ctx, network, addr)
			if err != nil {
				return nil, err
			}

			// opens the TLS connection with the upstream server
			tlsConn := tls.Client(conn, &tls.Config{
				RootCAs:    upstreamRoots,
				ServerName: expectedSNI,
			})

			// performs the TLS handshake with the upstream server
			if err := tlsConn.HandshakeContext(ctx); err != nil {
				_ = conn.Close()
				return nil, err
			}

			return tlsConn, nil
		},
	}

	proxy.ErrorHandler = func(w http.ResponseWriter, r *http.Request, err error) {
		info(fmt.Sprintf("[OPERATOR] upstream error: %v", err))
		http.Error(w, "bad gateway", http.StatusBadGateway)
	}

	handler := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
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
			info("[OPERATOR] request arrived while delegation_ready=false; relying on handshake delegation")
		} else {
			info("[OPERATOR] serving with delegated credential already available")
		}

		proxy.ServeHTTP(w, r)

		if !reuseDC {
			info("[OPERATOR] reuse_dc=false: delegated credential was session-local")
			st.delegationReady.Store(false)
		}

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

	tlsConfig := &tls.Config{
		MinVersion: tls.VersionTLS13,
		// SupportDelegatedCredential: true,
		GetCertificate: func(chi *tls.ClientHelloInfo) (*tls.Certificate, error) {
			if !st.delegationReady.Load() {
				info("[OPERATOR] delegation/auth with server: START")
			}

			cert, err := getOrFetchCertificate(chi)
			if err != nil {
				return nil, err
			}

			if !st.delegationReady.Load() {
				st.delegationReady.Store(true)
				info("[OPERATOR] delegation/auth with server: DONE")
			}

			return cert, nil
		},
	}

	srv := &http.Server{
		Addr:      operatorTLSAddr,
		Handler:   handler,
		TLSConfig: tlsConfig,
	}

	info(fmt.Sprintf("[OPERATOR] %s mode=%s listening on %s", operatorID, mode, operatorTLSAddr))

	log.Fatal(srv.ListenAndServeTLS("", ""))
}
