package main

import (
	"context"
	"crypto/tls"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"sync/atomic"
	"syscall"
	"time"

	benchtrace "dc/middlebox/internal/trace"
)

const (
	defaultAddr     = ":8000"
	defaultCertPath = "/home/bonsai/dcmb/certs_external/server/cert.pem"
	defaultKeyPath  = "/home/bonsai/dcmb/certs_external/server/key.pem"
)

var requestCounter atomic.Uint64
var logLevel = "error"

func debugf(format string, args ...any) {
	if logLevel == "debug" {
		log.Printf("[REQUEST_SERVER] "+format, args...)
	}
}

type appHandler struct{}

func (appHandler) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	traceID := strings.TrimSpace(r.Header.Get("X-Trace-ID"))
	if traceID == "" {
		traceID = fmt.Sprintf("requestserver-%d", requestCounter.Add(1))
	}

	body, err := io.ReadAll(r.Body)
	if err != nil {
		writeResponse(w, traceID, http.StatusBadRequest, map[string]string{"error": "invalid request body"})
		return
	}
	benchtrace.Mark(benchtrace.RequestServerRequestStart, traceID, uint64(len(body)))
	debugf("request received trace_id=%s method=%s path=%s body_bytes=%d", traceID, r.Method, r.URL.Path, len(body))

	if r.URL.Path != "/function/init" {
		writeResponse(w, traceID, http.StatusNotFound, map[string]string{"error": "not found"})
		return
	}
	if r.Method != http.MethodGet && r.Method != http.MethodPost {
		writeResponse(w, traceID, http.StatusMethodNotAllowed, map[string]string{"error": "method not allowed"})
		return
	}
	if !hasBearerToken(r.Header.Get("Authorization")) {
		writeResponse(w, traceID, http.StatusUnauthorized, map[string]string{"error": "missing or invalid bearer token"})
		return
	}

	writeResponse(w, traceID, http.StatusOK, map[string]string{
		"status":  "ok",
		"message": "function initialized",
	})
}

func hasBearerToken(value string) bool {
	const prefix = "Bearer "
	return strings.HasPrefix(value, prefix) && strings.TrimSpace(strings.TrimPrefix(value, prefix)) != ""
}

func writeResponse(w http.ResponseWriter, traceID string, status int, payload map[string]string) {
	data, err := json.Marshal(payload)
	if err != nil {
		benchtrace.Mark(benchtrace.RequestServerResponseDone, traceID, 1)
		http.Error(w, "response encoding failed", http.StatusInternalServerError)
		return
	}

	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Content-Length", fmt.Sprintf("%d", len(data)))
	benchtrace.Mark(benchtrace.RequestServerResponseStart, traceID, uint64(status))
	w.WriteHeader(status)
	_, err = w.Write(data)
	benchtrace.Mark(benchtrace.RequestServerResponseDone, traceID, errorArg(err))
	debugf("response sent trace_id=%s status=%d body_bytes=%d write_error=%v", traceID, status, len(data), err)
}

func errorArg(err error) uint64 {
	if err != nil {
		return 1
	}
	return 0
}

func main() {
	addr := flag.String("addr", defaultAddr, "listen address")
	certPath := flag.String("cert-path", defaultCertPath, "TLS certificate path")
	keyPath := flag.String("key-path", defaultKeyPath, "TLS private key path")
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

	srv := &http.Server{
		Addr:              *addr,
		Handler:           appHandler{},
		ReadHeaderTimeout: 5 * time.Second,
		IdleTimeout:       90 * time.Second,
		TLSConfig: &tls.Config{
			MinVersion: tls.VersionTLS13,
			NextProtos: []string{"http/1.1"},
		},
		TLSNextProto: make(map[string]func(*http.Server, *tls.Conn, http.Handler)),
	}

	certificate, err := tls.LoadX509KeyPair(*certPath, *keyPath)
	if err != nil {
		log.Fatal(err)
	}
	srv.TLSConfig.Certificates = []tls.Certificate{certificate}
	listener, err := net.Listen("tcp", *addr)
	if err != nil {
		log.Fatal(err)
	}
	tlsListener := tls.NewListener(listener, srv.TLSConfig)

	errCh := make(chan error, 1)
	go func() {
		errCh <- srv.Serve(tlsListener)
	}()
	fmt.Println("[REQUEST_SERVER_READY] listening=" + *addr + " protocol=http/1.1")

	stopCh := make(chan os.Signal, 1)
	signal.Notify(stopCh, os.Interrupt, syscall.SIGTERM)
	select {
	case sig := <-stopCh:
		fmt.Printf("[REQUEST_SERVER] shutdown signal received: %s\n", sig)
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		if err := srv.Shutdown(ctx); err != nil {
			log.Printf("[REQUEST_SERVER] shutdown failed: %v", err)
		}
	case err := <-errCh:
		if err != nil && err != http.ErrServerClosed {
			log.Fatal(err)
		}
	}
}
