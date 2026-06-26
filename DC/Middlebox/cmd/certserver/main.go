package main

import (
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
	"path/filepath"
	"strings"
	"time"
)

const (
	defaultCertPath = "/home/bonsai/dcmb/certs_external/server/cert.pem"
	defaultKeyPath  = "/home/bonsai/dcmb/certs_external/server/key.pem"
	defaultAddr     = ":5000"
)

type certRequest struct {
	SNI string `json:"sni"`
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
}

func nowNS() int64 {
	return time.Now().UnixNano()
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

func (s *serverState) handleCerts(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}

	fmt.Printf("t4: [SERVER] - ClientHelloLatency = %d ns\n", nowNS())

	var req certRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, "invalid json", http.StatusBadRequest)
		return
	}
	fmt.Printf("[SERVER] SNI requested: %s\n", req.SNI)
	if strings.TrimSpace(req.SNI) == "" {
		http.Error(w, "missing sni", http.StatusBadRequest)
		return
	}

	fmt.Printf("t5: [SERVER] - BeginAutoGenCerts = %d ns\n", nowNS())
	start := time.Now()
	dcBytes, keyPEM, err := s.generateDelegation()
	if err != nil {
		log.Printf("[SERVER] generate DC failed: %v", err)
		http.Error(w, "dc generation failed", http.StatusInternalServerError)
		return
	}
	fmt.Printf("[SERVER] generate in-process ms=%.3f\n", float64(time.Since(start).Microseconds())/1000)
	fmt.Printf("t6: [SERVER] - EndAutoGenCerts = %d ns\n", nowNS())

	resp := certResponse{
		CertB64:   s.certB64,
		KeyB64:    s.keyB64,
		DCCredB64: base64.StdEncoding.EncodeToString(dcBytes),
		DCKeyB64:  base64.StdEncoding.EncodeToString(keyPEM),
	}

	fmt.Printf("t7: [SERVER] - Sending to middlebox: %d ns\n", nowNS())
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
	flag.Parse()

	state, err := loadState(*certPath, *keyPath, *sigName, *duration)
	if err != nil {
		log.Fatal(err)
	}

	http.HandleFunc("/certs", state.handleCerts)

	fmt.Println("[SERVER] Go cert service listening on " + *addr)
	log.Fatal(http.ListenAndServe(*addr, nil))
}
