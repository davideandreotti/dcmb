//middlebox sgx
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
	"strings"
	"sync"
	"time"
)

const (
	port      = ":8443"
	targetUrl = "https://server:8000"
	certUrl   = "http://server:5000"
	certsDir  = "/certs"
)

// Toggle rapido per confronto sperimentale:
// - true  -> warmup scrittura leggero su file temporanei in /certs
// - false -> nessun warmup (comportamento originale)
var enableLightWriteWarmup = false

var (
	certCache        = make(map[string]*tls.Certificate)
	certMu           sync.RWMutex
	upstreamCertPool   *x509.CertPool
	upstreamCertPoolMu sync.RWMutex
)

func clearDelegatedState() {
	certMu.Lock()
	clear(certCache)
	certMu.Unlock()

	upstreamCertPoolMu.Lock()
	upstreamCertPool = nil
	upstreamCertPoolMu.Unlock()

	info("[MB-SGX] delegated state cleared after response")
}

type certResponse struct {
	// base certificate/key are convenient for debugging or initial handshake
	CertB64   string `json:"cert_b64"`
	KeyB64    string `json:"key_b64"`
	DCCredB64 string `json:"dc_cred_b64"`
	DCKeyB64  string `json:"dc_key_b64"`
}

type contextKey string

const (
	ctxUserKey        contextKey = "mb_user"
	ctxMessageTypeKey contextKey = "mb_message_type"
)

func info(msg string) {
	fmt.Fprintln(os.Stderr, msg)
}

func isCertificateRequest(r *http.Request) bool {
	return strings.Contains(r.URL.Path, "/certs")
}

func warmupWriteLight() {
	info("[MB] Warmup write light: START")

	if err := os.MkdirAll(certsDir, 0700); err != nil {
		info(fmt.Sprintf("[MB] Warmup write light skipped (mkdir failed): %v", err))
		return
	}

	// Usiamo file dedicati al warmup per non interferire con le vere deleghe.
	warmupCredFile := certsDir + "/warmup_dc.tmp"
	warmupKeyFile := certsDir + "/warmup_dckey.tmp"

	if err := os.WriteFile(warmupCredFile, []byte("warmup-dc"), 0600); err != nil {
		info(fmt.Sprintf("[MB] Warmup write light skipped (cred write failed): %v", err))
		return
	}
	if err := os.WriteFile(warmupKeyFile, []byte("warmup-key"), 0600); err != nil {
		_ = os.Remove(warmupCredFile)
		info(fmt.Sprintf("[MB] Warmup write light skipped (key write failed): %v", err))
		return
	}

	_ = os.Remove(warmupCredFile)
	_ = os.Remove(warmupKeyFile)

	info("[MB] Warmup write light: END")
}

func getOrFetchCertificate(chi *tls.ClientHelloInfo) (*tls.Certificate, error) {

	// t2: record arrival of ClientHello at middlebox
	info(fmt.Sprintf("t2: [MIDDLEBOX] - ClientHelloLatency = %d ns", time.Now().UnixNano()))

	sni := chi.ServerName
	if sni == "" {
		return nil, fmt.Errorf("client hello without SNI")
	}

	//info("[MB][TLS] ClientHello received SNI=" + sni)
	// t3: about to contact server based on SNI
	info(fmt.Sprintf("t3: [MIDDLEBOX] - ClientHello = %d ns", time.Now().UnixNano()))

	certMu.RLock()
	cert, ok := certCache[sni]
	certMu.RUnlock()

	if ok {
		info("[MB][TLS] Delegated certificate found in cache")
		return cert, nil
	}

	//info("[MB][TLS] Delegated certificate not in cache → requesting server")

	fetchedCert, err := requestDelegatedCertificatesFromServer(sni)
	if err != nil {
		return nil, err
	}

	info(fmt.Sprintf("t18: [MIDDLEBOX] --- = %d ns", time.Now().UnixNano()))

	//certMu.RLock()
	cert = certCache[sni]
	//certMu.RUnlock()

	info(fmt.Sprintf("t19: [MIDDLEBOX] --- = %d ns", time.Now().UnixNano()))

	if fetchedCert == nil && cert == nil {
		return nil, fmt.Errorf("certificate not cached after fetch")
	}
	info(fmt.Sprintf("\n !!! CONTROLLO !!! [MB] fetched certificate for SNI %s loaded in cache \n", sni))
	//info("[MB][TLS] Delegated certificate ready")
	// t9: just before sending ServerHello back to client
	info(fmt.Sprintf("t9: [MIDDLEBOX] - ServerHello = %d ns", time.Now().UnixNano()))

	if cert != nil {
		return cert, nil
	}

	return fetchedCert, nil
}

/*func requestDelegatedCertificatesFromServer(sni string) error {

	info("[MB] fetching certificates from " + certUrl)
	payload := map[string]string{
		"sni": sni,
	}

	body, err := json.Marshal(payload)
	if err != nil {
		return err
	}

	resp, err := http.Post(
		certUrl+"/certs",
		"application/json",
		bytes.NewReader(body),
	)
	if err != nil {
		return fmt.Errorf("failed contacting server: %w", err)
	}

	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		b, _ := io.ReadAll(resp.Body)
		return fmt.Errorf("server returned %d: %s", resp.StatusCode, string(b))
	}

	var data certResponse

	if err := json.NewDecoder(resp.Body).Decode(&data); err != nil {
		return err
	}

	// base cert/key are normally returned by the server. if they
	// are omitted we fall back to whatever was last saved on disk so the
	// handshake can still complete after a restart.
	if data.DCCredB64 == "" || data.DCKeyB64 == "" {
		return fmt.Errorf("invalid certificate response from server")
	}

	var certBytes, keyBytes []byte
	if data.CertB64 != "" && data.KeyB64 != "" {
		var err error
		certBytes, err = base64.StdEncoding.DecodeString(data.CertB64)
		if err != nil {
			return err
		}
		keyBytes, err = base64.StdEncoding.DecodeString(data.KeyB64)
		if err != nil {
			return err
		}
	} else {
		// try disk cache
		var err error
		certBytes, err = os.ReadFile(certsDir + "/cert.pem")
		if err != nil {
			return fmt.Errorf("server did not return base certificate/key and none on disk: %w", err)
		}
		keyBytes, err = os.ReadFile(certsDir + "/key.pem")
		if err != nil {
			return fmt.Errorf("server did not return base certificate/key and none on disk: %w", err)
		}
		info("[MB] using cached base cert/key from disk")
	}

	// decode DC bytes
	dcBytes, err := base64.StdEncoding.DecodeString(data.DCCredB64)
	if err != nil {
		return err
	}
	dckeyBytes, err := base64.StdEncoding.DecodeString(data.DCKeyB64)
	if err != nil {
		return err
	}

	// build tls.Certificate with delegated credential
	baseCert, err := tls.X509KeyPair(certBytes, keyBytes)
	if err != nil {
		return fmt.Errorf("failed to parse base cert/key: %w", err)
	}
	dc, err := tls.UnmarshalDelegatedCredential(dcBytes)
	if err != nil {
		return fmt.Errorf("failed to unmarshal delegated credential: %w", err)
	}
	block, _ := pem.Decode(dckeyBytes)
	if block == nil {
		return fmt.Errorf("invalid PEM delegated private key")
	}
	priv, err := x509.ParsePKCS8PrivateKey(block.Bytes)
	if err != nil {
		priv, err = x509.ParsePKCS1PrivateKey(block.Bytes)
		if err != nil {
			return fmt.Errorf("unable to parse delegated private key: %w", err)
		}
	}
	baseCert.DelegatedCredentials = append(baseCert.DelegatedCredentials, tls.DelegatedCredentialPair{dc, priv})

	// cache result directly; no need to touch base cert/key on disk
	certMu.Lock()
	certCache[sni] = &baseCert
	certMu.Unlock()

	// still persist DC files for visibility/rotation
	if err := os.MkdirAll(certsDir, 0700); err != nil {
		return err
	}
	if err := os.WriteFile(delegatedFile, dcBytes, 0600); err != nil {
		return err
	}
	if err := os.WriteFile(delegatedKeyFile, dckeyBytes, 0600); err != nil {
		return err
	}

	t2_2 := time.Now()
	info(fmt.Sprintf("[MB] Received delegated certificate from server t2.2): %d ns", t2_2))
	info("[MB] Delegated credential saved to disk")

	return nil
} */

func requestDelegatedCertificatesFromServer(sni string) (*tls.Certificate, error) {

	info("... in [MB]requestDelegatedCertificatesFromServer")

	info("[MB] fetching certificates from " + certUrl)
	payload := map[string]string{
		"sni": sni,
	}

	body, err := json.Marshal(payload)
	if err != nil {
		return nil, err
	}

	resp, err := http.Post(
		certUrl+"/certs",
		"application/json",
		bytes.NewReader(body),
	)
	if err != nil {
		return nil, fmt.Errorf("failed contacting server: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		b, _ := io.ReadAll(resp.Body)
		return nil, fmt.Errorf("server returned %d: %s", resp.StatusCode, string(b))
	}

	// read entire response so we can log raw JSON; some of our earlier
	// experiments accidentally returned unquoted numbers which caused the
	// strict struct decode below to fail with "cannot unmarshal number into
	// Go value of type string".  converting via a loose map avoids that.
	
	//TODO controollare questo dato
	dataBytes, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, err
	}
	info("\n !!! CONTROLLO !!! [MB] raw certificate response: " + string(dataBytes) + "\n")
	// t8: middlebox has received the certificates from server
	info(fmt.Sprintf("t8: [MIDDLEBOX] - CertsToMLatency = %d ns", time.Now().UnixNano()))
	//info(fmt.Sprintf("[MB] server reply: %s", string(dataBytes)))

	var raw map[string]interface{}
	if err := json.Unmarshal(dataBytes, &raw); err != nil {
		return nil, err
	}

	info(fmt.Sprintf("t11: [MB_sgx] --- = %d ns", time.Now().UnixNano()))

	getStr := func(k string) string {
		if v, ok := raw[k]; ok {
			switch t := v.(type) {
			case string:
				return t
			case float64:
				return fmt.Sprintf("%v", t)
			default:
				return fmt.Sprintf("%v", t)
			}
		}
		return ""
	}

	info(fmt.Sprintf("t12: [MB_sgx] --- = %d ns", time.Now().UnixNano()))

	certB64 := getStr("cert_b64")
	keyB64 := getStr("key_b64")
	dcB64 := getStr("dc_cred_b64")
	dcKeyB64 := getStr("dc_key_b64")

	if dcB64 == "" || dcKeyB64 == "" {
		return nil, fmt.Errorf("invalid certificate response from server")
	}

	var certBytes, keyBytes []byte
	if certB64 != "" && keyB64 != "" {
		certBytes, err = base64.StdEncoding.DecodeString(certB64)
		if err != nil {
			return nil, err
		}
		keyBytes, err = base64.StdEncoding.DecodeString(keyB64)
		if err != nil {
			return nil, err
		}
	} else {
		certBytes, err = os.ReadFile(certsDir + "/cert.pem")
		if err != nil {
			return nil, fmt.Errorf("server did not return base certificate/key and none on disk: %w", err)
		}
		keyBytes, err = os.ReadFile(certsDir + "/key.pem")
		if err != nil {
			return nil, fmt.Errorf("server did not return base certificate/key and none on disk: %w", err)
		}
		info("[MB] using cached base cert/key from disk")
	}

	info(fmt.Sprintf("t13: [MB_sgx] --- = %d ns", time.Now().UnixNano()))

	info("\n !!! CONTROLLO !!! [MB_sgx] certBytes: " + string(certBytes) + "\n")
	info("\n !!! CONTROLLO !!! [MB_sgx] keyBytes: " + string(keyBytes) + "\n")

	// trust the server's self-signed certificate for upstream connections
	pool := x509.NewCertPool()
	pool.AppendCertsFromPEM(certBytes)
	upstreamCertPoolMu.Lock()
	upstreamCertPool = pool
	upstreamCertPoolMu.Unlock()

	info(fmt.Sprintf("t14: [MB_sgx] --- = %d ns", time.Now().UnixNano()))

	dcBytes, err := base64.StdEncoding.DecodeString(dcB64)
	if err != nil {
		return nil, err
	}
	dckeyBytes, err := base64.StdEncoding.DecodeString(dcKeyB64)
	if err != nil {
		return nil, err
	}

	//info("\n !!! CONTROLLO !!! [MB] dcBytes: " + string(dcBytes) + "\n")
	//info("\n !!! CONTROLLO !!! [MB] dckeyBytes: " + string(dckeyBytes) + "\n")

	baseCert, err := tls.X509KeyPair(certBytes, keyBytes)
	if err != nil {
		return nil, fmt.Errorf("failed to parse base cert/key: %w", err)
	}
	dc, err := tls.UnmarshalDelegatedCredential(dcBytes)
	if err != nil {
		return nil, fmt.Errorf("failed to unmarshal delegated credential: %w", err)
	}
	block, _ := pem.Decode(dckeyBytes)
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
	baseCert.DelegatedCredentials = append(baseCert.DelegatedCredentials, tls.DelegatedCredentialPair{dc, priv})

	info(fmt.Sprintf("t15: [MB_sgx] --- = %d ns", time.Now().UnixNano()))

	//certMu.Lock()
	certCache[sni] = &baseCert
	//certMu.Unlock()

	info(fmt.Sprintf("t16: [MB_sgx] --- = %d ns", time.Now().UnixNano()))
	info(fmt.Sprintf("t17: [MB_sgx] --- = %d ns", time.Now().UnixNano()))
	return &baseCert, nil
}

func main() {

	info("[MB_sgx] proxy target is " + targetUrl)
	if enableLightWriteWarmup {
		warmupWriteLight()
	}
	remote, err := url.Parse(targetUrl)
	if err != nil {
		log.Fatal(err)
	}

	tlsConfig := &tls.Config{
		GetCertificate: getOrFetchCertificate,
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

	proxy.ModifyResponse = func(r *http.Response) error {
		info("[MB_sgx] Response received from server")
		user, _ := r.Request.Context().Value(ctxUserKey).(string)
		messageType, _ := r.Request.Context().Value(ctxMessageTypeKey).(*MessageType)
		if user != "" && messageType != nil {
			processResponse(r, user, messageType)
		}
		return nil
	}

	handler := func(p *httputil.ReverseProxy) http.HandlerFunc {
		return func(w http.ResponseWriter, r *http.Request) {

			info(fmt.Sprintf("[MB_sgx] Request %s %s", r.Method, r.URL))
			valid, user, messageType := processRequest(r)
			if !valid {
				http.Error(w, "request rejected by middlebox validation", http.StatusUnauthorized)
				return
			}

			ctxWithValidation := context.WithValue(r.Context(), ctxUserKey, user)
			ctxWithValidation = context.WithValue(ctxWithValidation, ctxMessageTypeKey, messageType)
			r = r.WithContext(ctxWithValidation)

			p.ServeHTTP(w, r)
			// Simulate a fresh middlebox instance per request.
			// The next TLS handshake must fetch a new delegated credential.
			clearDelegatedState()
		}
	}

	router := http.NewServeMux()
	router.HandleFunc("/", handler(proxy))

	srv := &http.Server{
		Addr:      port,
		Handler:   router,
		TLSConfig: tlsConfig,
	}

	info("[MB_sgx] Middlebox listening on " + port)

	log.Fatal(srv.ListenAndServeTLS("", ""))
}
