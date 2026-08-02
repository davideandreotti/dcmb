package main

import (
	"context"
	"crypto/sha256"
	"crypto/tls"
	"crypto/x509"
	"encoding/base64"
	"encoding/json"
	"encoding/pem"
	"flag"
	"fmt"
	"log"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"strings"
	"syscall"
	"time"

	benchtrace "dc/middlebox/internal/trace"
)

const (
	defaultCertPath = "/home/bonsai/dcmb/certs_external/server/cert.pem"
	defaultKeyPath  = "/home/bonsai/dcmb/certs_external/server/key.pem"
	defaultAddr     = ":5000"
	defaultQuoteTag = "middlebox-attestation-test"
)

var logLevel = "error"

func debugf(format string, args ...any) {
	if logLevel == "debug" {
		log.Printf("[SERVER] "+format, args...)
	}
}

type certRequest struct {
	SNI          string `json:"sni"`
	Quote        string `json:"quote"`
	QuoteB64     string `json:"quote_b64"`
	DelegationID string `json:"delegation_id"`
}

func (r certRequest) traceID() string {
	if strings.TrimSpace(r.DelegationID) != "" {
		return r.DelegationID
	}
	return r.SNI
}

type certResponse struct {
	CertB64   string `json:"cert_b64"`
	KeyB64    string `json:"key_b64"`
	DCCredB64 string `json:"dc_cred_b64"`
	DCKeyB64  string `json:"dc_key_b64"`
}

type serverState struct {
	cert      tls.Certificate
	certB64   string
	keyB64    string
	duration  time.Duration
	sigScheme tls.SignatureScheme

	reportData []byte
}

type quoteVerificationInfo struct {
	DCAPReturn              uint32
	CollateralExpiration    uint32
	QuoteVerificationResult uint32
	AcceptedNonTerminal     bool
}

func expectedReportData(tag string) []byte {
	if strings.TrimSpace(tag) == "" {
		return nil
	}
	sum := sha256.Sum256([]byte(tag))
	reportData := make([]byte, 64)
	copy(reportData, sum[:])
	return reportData
}

func signatureScheme(name string) (tls.SignatureScheme, error) {
	switch strings.TrimSpace(name) {
	case "Ed25519":
		return tls.Ed25519, nil
	case "ECDSAWithP256AndSHA256":
		return tls.ECDSAWithP256AndSHA256, nil
	case "ECDSAWithP384AndSHA384":
		return tls.ECDSAWithP384AndSHA384, nil
	case "ECDSAWithP521AndSHA512":
		return tls.ECDSAWithP521AndSHA512, nil
	default:
		return 0, fmt.Errorf("unsupported signature scheme %q", name)
	}
}

func loadState(certPath, keyPath, sigName string, duration time.Duration) (*serverState, error) {
	cert, err := tls.LoadX509KeyPair(certPath, keyPath)
	if err != nil {
		return nil, err
	}
	cert.Leaf, err = x509.ParseCertificate(cert.Certificate[0])
	if err != nil {
		return nil, err
	}

	certBytes, err := os.ReadFile(certPath)
	if err != nil {
		return nil, err
	}
	keyBytes, err := os.ReadFile(keyPath)
	if err != nil {
		return nil, err
	}

	sig, err := signatureScheme(sigName)
	if err != nil {
		return nil, err
	}

	return &serverState{
		cert:      cert,
		certB64:   base64.StdEncoding.EncodeToString(certBytes),
		keyB64:    base64.StdEncoding.EncodeToString(keyBytes),
		duration:  duration,
		sigScheme: sig,
	}, nil
}

func (s *serverState) generateDelegation() ([]byte, []byte, error) {
	validTime := time.Since(s.cert.Leaf.NotBefore) + s.duration
	dc, priv, err := tls.NewDelegatedCredential(&s.cert, s.sigScheme, validTime, false)
	if err != nil {
		return nil, nil, err
	}

	dcBytes, err := dc.Marshal()
	if err != nil {
		return nil, nil, err
	}

	keyDER, err := x509.MarshalPKCS8PrivateKey(priv)
	if err != nil {
		return nil, nil, err
	}
	keyPEM := pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: keyDER})

	return dcBytes, keyPEM, nil
}

func (s *serverState) verifyAttestation(req certRequest) (int, string) {
	quoteB64 := strings.TrimSpace(req.Quote)
	if quoteB64 == "" {
		quoteB64 = strings.TrimSpace(req.QuoteB64)
	}

	if quoteB64 == "" {
		debugf("no attestation quote supplied; verification skipped trace_id=%s sni=%s", req.traceID(), req.SNI)
		return 0, ""
	}

	quoteBytes, err := base64.StdEncoding.DecodeString(quoteB64)
	if err != nil {
		benchtrace.Mark(benchtrace.CertServerError, req.SNI, 11)
		benchtrace.Mark(benchtrace.CertServerErrorByID, req.traceID(), 11)
		fmt.Printf("[SERVER] Invalid attestation quote: %v\n", err)
		return http.StatusBadRequest, "invalid attestation quote"
	}

	verificationStarted := time.Now()
	benchtrace.Mark(benchtrace.CertServerQuoteVerify, req.SNI, uint64(len(quoteBytes)))
	benchtrace.Mark(benchtrace.CertServerQuoteVerifyByID, req.traceID(), uint64(len(quoteBytes)))
	debugf("attestation quote received trace_id=%s sni=%s bytes=%d", req.traceID(), req.SNI, len(quoteBytes))
	info, err := verifyQuote(quoteBytes, s.reportData)
	doneArg := uint64(info.QuoteVerificationResult)
	if err != nil {
		doneArg = 1
	}
	benchtrace.Mark(benchtrace.CertServerQuoteDone, req.SNI, doneArg)
	benchtrace.Mark(benchtrace.CertServerQuoteDoneByID, req.traceID(), doneArg)
	verificationMS := float64(time.Since(verificationStarted).Nanoseconds()) / 1_000_000
	if err != nil {
		benchtrace.Mark(benchtrace.CertServerError, req.SNI, 12)
		benchtrace.Mark(benchtrace.CertServerErrorByID, req.traceID(), 12)
		fmt.Printf("[SERVER] Quote verification failed after %.3f ms: %v\n", verificationMS, err)
		return http.StatusForbidden, "quote verification failed"
	}
	debugf(
		"attestation quote accepted trace_id=%s dcap_return=0x%x qv_result=0x%x non_terminal=%t verification_ms=%.3f",
		req.traceID(),
		info.DCAPReturn,
		info.QuoteVerificationResult,
		info.AcceptedNonTerminal,
		verificationMS,
	)

	return 0, ""
}

func (s *serverState) handleCerts(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}

	benchtrace.Mark(benchtrace.CertServerRequest, "certs", 0)

	var req certRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		benchtrace.Mark(benchtrace.CertServerError, "certs", 1)
		log.Printf("[SERVER] invalid delegation request JSON: %v", err)
		http.Error(w, "invalid json", http.StatusBadRequest)
		return
	}
	benchtrace.Mark(benchtrace.CertServerRequestByID, req.traceID(), 0)
	debugf("delegation request received trace_id=%s sni=%s", req.traceID(), req.SNI)
	if strings.TrimSpace(req.SNI) == "" {
		benchtrace.Mark(benchtrace.CertServerError, "certs", 2)
		benchtrace.Mark(benchtrace.CertServerErrorByID, req.traceID(), 2)
		log.Printf("[SERVER] delegation request missing SNI trace_id=%s", req.traceID())
		http.Error(w, "missing sni", http.StatusBadRequest)
		return
	}
	if status, message := s.verifyAttestation(req); status != 0 {
		http.Error(w, message, status)
		return
	}

	benchtrace.Mark(benchtrace.CertServerGenerate, req.SNI, 0)
	benchtrace.Mark(benchtrace.CertServerGenerateByID, req.traceID(), 0)
	dcBytes, keyPEM, err := s.generateDelegation()
	if err != nil {
		benchtrace.Mark(benchtrace.CertServerGenerateDone, req.SNI, 1)
		benchtrace.Mark(benchtrace.CertServerGenerateDoneByID, req.traceID(), 1)
		benchtrace.Mark(benchtrace.CertServerError, req.SNI, 3)
		benchtrace.Mark(benchtrace.CertServerErrorByID, req.traceID(), 3)
		log.Printf("[SERVER] generate DC failed: %v", err)
		http.Error(w, "dc generation failed", http.StatusInternalServerError)
		return
	}
	benchtrace.Mark(benchtrace.CertServerGenerateDone, req.SNI, 0)
	benchtrace.Mark(benchtrace.CertServerGenerateDoneByID, req.traceID(), 0)
	debugf("delegated credential generated trace_id=%s credential_bytes=%d key_bytes=%d", req.traceID(), len(dcBytes), len(keyPEM))

	resp := certResponse{
		CertB64:   s.certB64,
		KeyB64:    s.keyB64,
		DCCredB64: base64.StdEncoding.EncodeToString(dcBytes),
		DCKeyB64:  base64.StdEncoding.EncodeToString(keyPEM),
	}

	benchtrace.Mark(benchtrace.CertServerResponse, req.SNI, 0)
	benchtrace.Mark(benchtrace.CertServerResponseByID, req.traceID(), 0)
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(resp)
}

func main() {
	defaultCertDir := os.Getenv("CERTS_DIR")
	certPathDefault := defaultCertPath
	keyPathDefault := defaultKeyPath
	if defaultCertDir != "" {
		certPathDefault = filepath.Join(defaultCertDir, "cert.pem")
		keyPathDefault = filepath.Join(defaultCertDir, "key.pem")
	}

	addr := flag.String("addr", defaultAddr, "listen address")
	certPath := flag.String("cert-path", certPathDefault, "base certificate path")
	keyPath := flag.String("key-path", keyPathDefault, "base private key path")
	sigName := flag.String("signature-scheme", "Ed25519", "delegated credential signature scheme")
	duration := flag.Duration("duration", 168*time.Hour, "delegated credential duration")
	tracePath := flag.String("trace", "", "binary trace output path")
	traceBuffer := flag.Int("trace-buffer-events", 100000, "trace buffer capacity in events")
	traceDrop := flag.Bool("trace-drop-on-full", true, "drop trace events when the buffer is full")
	logLevelFlag := flag.String("log_level", "error", "log level: error or debug")
	flag.Parse()

	logLevel = strings.ToLower(strings.TrimSpace(*logLevelFlag))
	if logLevel != "error" && logLevel != "debug" {
		log.Fatalf("invalid -log_level %q: expected error or debug", *logLevelFlag)
	}

	if err := benchtrace.Start(*tracePath, *traceBuffer, *traceDrop); err != nil {
		log.Fatal(err)
	}
	defer benchtrace.Stop()

	state, err := loadState(*certPath, *keyPath, *sigName, *duration)
	if err != nil {
		log.Fatal(err)
	}
	state.reportData = expectedReportData(defaultQuoteTag)

	mux := http.NewServeMux()
	mux.HandleFunc("/certs", state.handleCerts)

	fmt.Println("[SERVER] Go cert service listening on " + *addr)
	srv := &http.Server{Addr: *addr, Handler: mux}
	errCh := make(chan error, 1)
	go func() {
		errCh <- srv.ListenAndServe()
	}()

	stopCh := make(chan os.Signal, 1)
	signal.Notify(stopCh, os.Interrupt, syscall.SIGTERM)

	select {
	case sig := <-stopCh:
		fmt.Printf("[SERVER] shutdown signal received: %s\n", sig)
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		if err := srv.Shutdown(ctx); err != nil {
			log.Printf("[SERVER] shutdown failed: %v", err)
		}
	case err := <-errCh:
		if err != nil && err != http.ErrServerClosed {
			log.Fatal(err)
		}
	}
}
