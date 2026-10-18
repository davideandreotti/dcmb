package main

import (
	"context"
	"crypto/x509"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"net/http/httputil"
	"net/url"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
)

func TestUpstreamSessionReusesOneConnection(t *testing.T) {
	var connections atomic.Int64
	largeBody := strings.Repeat("response", responseCopyBufferSize)
	server := httptest.NewUnstartedServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/large" {
			_, _ = io.WriteString(w, largeBody)
		} else {
			_, _ = io.WriteString(w, "ok")
		}
	}))
	server.EnableHTTP2 = false
	server.Config.ConnState = func(_ net.Conn, state http.ConnState) {
		if state == http.StateNew {
			connections.Add(1)
		}
	}
	server.StartTLS()
	defer server.Close()

	remote, err := url.Parse(server.URL)
	if err != nil {
		t.Fatal(err)
	}
	roots := x509.NewCertPool()
	roots.AddCert(server.Certificate())
	manager, err := newUpstreamSessionManager(remote, roots)
	if err != nil {
		t.Fatal(err)
	}
	manager.serverName = remote.Hostname()
	t.Cleanup(manager.closeAll)
	session := manager.accept(1, "test-session")
	proxy := httputil.NewSingleHostReverseProxy(remote)
	proxy.Transport = manager
	session.configureProxy(proxy)
	if session.proxy.Transport != manager {
		t.Fatal("connection proxy changed the existing transport")
	}
	if err := session.prepare(context.Background()); err != nil {
		t.Fatal(err)
	}
	if session.responseBuffer.buf != nil {
		t.Fatal("buffer allocated before the first response copy")
	}

	var firstBuffer *byte
	for _, response := range []struct{ path, body string }{{"/large", largeBody}, {"/short", "ok"}} {
		req, err := http.NewRequest(http.MethodGet, server.URL+response.path, nil)
		if err != nil {
			t.Fatal(err)
		}
		req = req.WithContext(context.WithValue(req.Context(), upstreamSessionContextKey{}, session))
		recorder := httptest.NewRecorder()
		session.proxy.ServeHTTP(recorder, req)
		if recorder.Code != http.StatusOK || recorder.Body.String() != response.body {
			t.Fatalf("response %s: status=%d, body length=%d", response.path, recorder.Code, recorder.Body.Len())
		}
		if firstBuffer == nil {
			firstBuffer = &session.responseBuffer.buf[0]
		} else if firstBuffer != &session.responseBuffer.buf[0] {
			t.Fatal("response-copy buffer was not reused")
		}
	}

	manager.closeAll()
	if got := connections.Load(); got != 1 {
		t.Fatalf("upstream connections = %d, want 1", got)
	}
	if len(manager.sessions) != 0 || !session.closed || session.conn != nil {
		t.Fatal("connection session was not cleaned up")
	}
}

func TestConnectionResponseBufferLazyReuse(t *testing.T) {
	var buffer connectionResponseBuffer
	if buffer.buf != nil {
		t.Fatal("new buffer should not allocate storage")
	}
	first := buffer.Get()
	if len(first) != responseCopyBufferSize {
		t.Fatalf("buffer length = %d, want %d", len(first), responseCopyBufferSize)
	}
	buffer.Put(first)
	if &buffer.Get()[0] != &first[0] {
		t.Fatal("Get did not return the connection's original buffer")
	}
	if allocations := testing.AllocsPerRun(100, func() { buffer.Put(buffer.Get()) }); allocations != 0 {
		t.Fatalf("buffer reuse allocated %v objects per call", allocations)
	}
}

type bufferTestRoundTripper func(*http.Request) (*http.Response, error)

func (f bufferTestRoundTripper) RoundTrip(r *http.Request) (*http.Response, error) {
	return f(r)
}

func TestConnectionProxiesCopyResponsesInParallel(t *testing.T) {
	remote, err := url.Parse("https://server")
	if err != nil {
		t.Fatal(err)
	}
	template := httputil.NewSingleHostReverseProxy(remote)
	template.Transport = bufferTestRoundTripper(func(r *http.Request) (*http.Response, error) {
		body := r.Header.Get("X-Test-Connection")
		if r.URL.Path == "/large" {
			body = strings.Repeat(body, responseCopyBufferSize+17)
		}
		return &http.Response{
			StatusCode: http.StatusOK, Header: make(http.Header),
			Body: io.NopCloser(strings.NewReader(body)), ContentLength: int64(len(body)), Request: r,
		}, nil
	})
	var modified atomic.Int64
	template.ModifyResponse = func(*http.Response) error { modified.Add(1); return nil }

	const count = 8
	sessions := make([]*upstreamSession, count)
	for i := range sessions {
		sessions[i] = &upstreamSession{}
		sessions[i].configureProxy(template)
		if sessions[i].proxy == template || sessions[i].responseBuffer.buf != nil {
			t.Fatal("connection proxy must be distinct and its buffer lazy")
		}
	}
	if template.BufferPool != nil {
		t.Fatal("connection configuration mutated the shared proxy")
	}

	start := make(chan struct{})
	var workers sync.WaitGroup
	for i, session := range sessions {
		workers.Add(1)
		go func(i int, session *upstreamSession) {
			defer workers.Done()
			<-start
			id := string(rune('A' + i))
			for _, path := range []string{"/large", "/short", "/large"} {
				req := httptest.NewRequest(http.MethodGet, "https://server"+path, nil)
				req.Header.Set("X-Test-Connection", id)
				recorder := httptest.NewRecorder()
				session.proxy.ServeHTTP(recorder, req)
				expected := id
				if path == "/large" {
					expected = strings.Repeat(id, responseCopyBufferSize+17)
				}
				if recorder.Code != http.StatusOK || recorder.Body.String() != expected {
					t.Errorf("connection %d response %s differs", i, path)
				}
			}
		}(i, session)
	}
	close(start)
	workers.Wait()
	if modified.Load() != count*3 {
		t.Fatal("connection proxies did not preserve the response callback")
	}
	for i := 1; i < count; i++ {
		if &sessions[i].responseBuffer.buf[0] == &sessions[0].responseBuffer.buf[0] {
			t.Fatal("different connections share buffer storage")
		}
	}
}
