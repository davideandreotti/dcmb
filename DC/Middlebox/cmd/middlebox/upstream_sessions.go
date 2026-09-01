package main

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"errors"
	"fmt"
	"net"
	"net/http"
	"net/url"
	"sync"
	"time"

	benchtrace "dc/middlebox/internal/trace"
	"dc/middlebox/internal/tracebind"
)

type upstreamSessionContextKey struct{}

type upstreamSessionManager struct {
	address    string
	serverName string
	roots      *x509.CertPool

	mu       sync.RWMutex
	sessions map[uint64]*upstreamSession
}

type upstreamSession struct {
	id         string
	address    string
	serverName string
	roots      *x509.CertPool

	prepareOnce sync.Once
	prepareErr  error

	mu        sync.Mutex
	conn      net.Conn
	available bool
	closed    bool

	transport *http.Transport
}

func newUpstreamSessionManager(remote *url.URL, roots *x509.CertPool) (*upstreamSessionManager, error) {
	if remote == nil || remote.Scheme != "https" {
		return nil, fmt.Errorf("upstream target must use https")
	}
	address := remote.Host
	if remote.Port() == "" {
		address = net.JoinHostPort(remote.Hostname(), "443")
	}
	return &upstreamSessionManager{
		address:    address,
		serverName: expectedSNI,
		roots:      roots,
		sessions:   make(map[uint64]*upstreamSession),
	}, nil
}

func (m *upstreamSessionManager) accept(connectionKey uint64, sessionID string) *upstreamSession {
	session := &upstreamSession{
		id:         sessionID,
		address:    m.address,
		serverName: m.serverName,
		roots:      m.roots,
	}
	session.transport = &http.Transport{
		DialTLSContext:        session.dialTLSContext,
		ForceAttemptHTTP2:     false,
		MaxIdleConns:          1,
		MaxIdleConnsPerHost:   1,
		MaxConnsPerHost:       1,
		IdleConnTimeout:       90 * time.Second,
		TLSHandshakeTimeout:   3 * time.Second,
		ExpectContinueTimeout: time.Second,
	}

	m.mu.Lock()
	m.sessions[connectionKey] = session
	m.mu.Unlock()
	benchtrace.Mark(benchtrace.MiddleboxUpstreamConnectionBind, sessionID, connectionKey)
	return session
}

func (m *upstreamSessionManager) prepareClientHello(chi *tls.ClientHelloInfo) error {
	connectionKey := tracebind.ConnectionKey(chi.Conn.LocalAddr(), chi.Conn.RemoteAddr())
	m.mu.RLock()
	session := m.sessions[connectionKey]
	m.mu.RUnlock()
	if session == nil {
		return fmt.Errorf("upstream session missing for downstream connection")
	}
	return session.prepare(chi.Context())
}

func (m *upstreamSessionManager) RoundTrip(req *http.Request) (*http.Response, error) {
	session, _ := req.Context().Value(upstreamSessionContextKey{}).(*upstreamSession)
	if session == nil {
		return nil, errors.New("request has no upstream session")
	}
	return session.transport.RoundTrip(req)
}

func (m *upstreamSessionManager) closeConnection(conn net.Conn) {
	connectionKey := tracebind.ConnectionKey(conn.LocalAddr(), conn.RemoteAddr())
	m.mu.Lock()
	session := m.sessions[connectionKey]
	delete(m.sessions, connectionKey)
	m.mu.Unlock()
	if session != nil {
		session.close()
	}
}

func (m *upstreamSessionManager) closeAll() {
	m.mu.Lock()
	sessions := make([]*upstreamSession, 0, len(m.sessions))
	for key, session := range m.sessions {
		sessions = append(sessions, session)
		delete(m.sessions, key)
	}
	m.mu.Unlock()
	for _, session := range sessions {
		session.close()
	}
}

func (s *upstreamSession) prepare(ctx context.Context) error {
	s.prepareOnce.Do(func() {
		if ctx == nil {
			ctx = context.Background()
		}
		ctx, cancel := context.WithTimeout(ctx, 3*time.Second)
		defer cancel()

		dialer := &net.Dialer{Timeout: 3 * time.Second, KeepAlive: 30 * time.Second}
		benchtrace.Mark(benchtrace.MiddleboxUpstreamDialStart, s.id, 0)
		conn, err := dialer.DialContext(ctx, "tcp", s.address)
		if err != nil {
			benchtrace.Mark(benchtrace.MiddleboxUpstreamDialDone, s.id, 1)
			s.prepareErr = err
			return
		}
		benchtrace.Mark(benchtrace.MiddleboxUpstreamDialDone, s.id, 0)

		tlsConn := tls.Client(conn, &tls.Config{RootCAs: s.roots, ServerName: s.serverName})
		benchtrace.Mark(benchtrace.MiddleboxUpstreamTLSStart, s.id, 0)
		if err := tlsConn.HandshakeContext(ctx); err != nil {
			_ = conn.Close()
			benchtrace.Mark(benchtrace.MiddleboxUpstreamTLSDone, s.id, 1)
			s.prepareErr = err
			return
		}
		benchtrace.Mark(benchtrace.MiddleboxUpstreamTLSDone, s.id, 0)

		s.mu.Lock()
		if s.closed {
			s.mu.Unlock()
			_ = tlsConn.Close()
			s.prepareErr = net.ErrClosed
			return
		}
		s.conn = tlsConn
		s.available = true
		s.mu.Unlock()
	})
	return s.prepareErr
}

func (s *upstreamSession) dialTLSContext(ctx context.Context, _, _ string) (net.Conn, error) {
	if err := s.prepare(ctx); err != nil {
		return nil, err
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.closed {
		return nil, net.ErrClosed
	}
	if !s.available || s.conn == nil {
		return nil, errors.New("dedicated upstream connection is unavailable")
	}
	s.available = false
	return s.conn, nil
}

func (s *upstreamSession) close() {
	s.transport.CloseIdleConnections()
	s.mu.Lock()
	s.closed = true
	conn := s.conn
	s.conn = nil
	s.available = false
	s.mu.Unlock()
	if conn != nil {
		_ = conn.Close()
	}
}
