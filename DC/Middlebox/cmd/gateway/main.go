// middlebox gateway
package main

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"math/rand"
	"net"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"time"

	"dc/middlebox/internal/ticketidentity"
	"dc/middlebox/internal/tlshello"
	benchtrace "dc/middlebox/internal/trace"
	"dc/middlebox/internal/tracebind"
)

const (
	gatewayListenAddr           = ":9443"
	operatorTLSPort             = "8443"
	operatorReadinessPort       = "18080"
	defaultClientQueueSize      = 100
	defaultClientQueueTimeoutMs = 5000
	defaultClientQueueRetryMs   = 1
	clientHelloPeekTimeout      = 2 * time.Second
	maxClientHelloPeekBytes     = 64 * 1024
)

type gatewayState struct {
	startedAt      time.Time
	accepted       atomic.Int64
	forwarded      atomic.Int64
	dropped        atomic.Int64
	lastBackend    atomic.Value
	lastResolution atomic.Value
	affinity       *ticketAffinity
}

type backendPool struct {
	taskHost string
	rng      *rand.Rand
	mu       sync.Mutex
	policy   string
	lastGood string
	leased   map[string]struct{}
	bound    map[string]struct{}
	k8s      *kubernetesBackend
	docker   *dockerBackend
}

type backendTarget struct {
	name string
	ip   string
}

type clientQueueConfig struct {
	size    int
	timeout time.Duration
	retry   time.Duration
}

type queuedClient struct {
	id       string
	conn     net.Conn
	deadline time.Time
	preface  []byte
	hello    *tlshello.Info
}

type ticketAffinity struct {
	key []byte

	mu     sync.RWMutex
	routes map[string]backendTarget
}

type kubernetesBackend struct {
	namespace      string
	labelSelector  string
	deleteAfterUse bool
	apiServer      string
	apiTimeout     time.Duration
	httpClient     *http.Client
	bearerToken    string
}

type kubernetesPodList struct {
	Items []kubernetesPod `json:"items"`
}

type kubernetesPod struct {
	Metadata struct {
		Name              string `json:"name"`
		UID               string `json:"uid"`
		CreationTimestamp string `json:"creationTimestamp"`
	} `json:"metadata"`
	Status struct {
		Phase      string `json:"phase"`
		PodIP      string `json:"podIP"`
		Conditions []struct {
			Type   string `json:"type"`
			Status string `json:"status"`
		} `json:"conditions"`
	} `json:"status"`
}

func info(msg string) {
	fmt.Fprintln(os.Stderr, msg)
}

func getEnv(key string, fallback string) string {
	if value := strings.TrimSpace(os.Getenv(key)); value != "" {
		return value
	}
	return fallback
}

func getEnvInt(key string, fallback int) int {
	raw := strings.TrimSpace(os.Getenv(key))
	if raw == "" {
		return fallback
	}
	parsed, err := strconv.Atoi(raw)
	if err != nil {
		return fallback
	}
	return parsed
}

func getEnvBool(key string, fallback bool) bool {
	raw := strings.TrimSpace(strings.ToLower(os.Getenv(key)))
	if raw == "" {
		return fallback
	}
	switch raw {
	case "1", "true", "yes", "y", "on":
		return true
	case "0", "false", "no", "n", "off":
		return false
	default:
		return fallback
	}
}

func newTicketAffinityFromEnv() *ticketAffinity {
	key := strings.TrimSpace(os.Getenv("DCMB_TICKET_IDENTITY_KEY"))
	if key == "" {
		return nil
	}
	return &ticketAffinity{
		key:    []byte(key),
		routes: make(map[string]backendTarget),
	}
}

func (a *ticketAffinity) lookup(hello *tlshello.Info) (backendTarget, bool) {
	if a == nil || hello == nil {
		return backendTarget{}, false
	}
	a.mu.RLock()
	defer a.mu.RUnlock()
	for _, identity := range hello.PSKIdentities {
		if !ticketidentity.IsIdentity(identity) {
			continue
		}
		backend, ok := a.routes[string(identity)]
		if ok {
			return backend, true
		}
	}
	return backendTarget{}, false
}

func (a *ticketAffinity) bind(backend backendTarget, serverName string) error {
	if a == nil || backend.name == "" || strings.TrimSpace(serverName) == "" {
		return nil
	}
	identity, err := ticketidentity.Derive(a.key, backend.name, serverName)
	if err != nil {
		return err
	}

	a.mu.Lock()
	a.routes[string(identity)] = backend
	a.mu.Unlock()
	return nil
}

func (a *ticketAffinity) forgetBackend(name string) {
	if a == nil || name == "" {
		return
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	for identity, backend := range a.routes {
		if backend.name == name {
			delete(a.routes, identity)
		}
	}
}

func peekClientHello(conn net.Conn) ([]byte, *tlshello.Info, error) {
	if err := conn.SetReadDeadline(time.Now().Add(clientHelloPeekTimeout)); err != nil {
		return nil, nil, err
	}
	defer conn.SetReadDeadline(time.Time{})

	var raw []byte
	for len(raw) < maxClientHelloPeekBytes {
		if err := readExactly(conn, &raw, 5); err != nil {
			return raw, nil, err
		}
		recordLen := int(raw[len(raw)-2])<<8 | int(raw[len(raw)-1])
		if recordLen < 0 || len(raw)+recordLen > maxClientHelloPeekBytes {
			return raw, nil, errors.New("TLS ClientHello exceeds peek limit")
		}
		if err := readExactly(conn, &raw, recordLen); err != nil {
			return raw, nil, err
		}

		hello, err := tlshello.Parse(raw)
		if err == nil {
			return raw, hello, nil
		}
		if !errors.Is(err, tlshello.ErrIncomplete) {
			return raw, nil, err
		}
	}
	return raw, nil, errors.New("TLS ClientHello exceeds peek limit")
}

func readExactly(conn net.Conn, dst *[]byte, n int) error {
	buf := make([]byte, n)
	read := 0
	for read < n {
		count, err := conn.Read(buf[read:])
		if count > 0 {
			read += count
		}
		if err != nil {
			*dst = append(*dst, buf[:read]...)
			return err
		}
	}
	*dst = append(*dst, buf...)
	return nil
}

func newClientQueueConfigFromEnv(noReadyWaitSeconds int, noReadyRetryMs int) clientQueueConfig {
	size := getEnvInt("GATEWAY_CLIENT_QUEUE_SIZE", defaultClientQueueSize)
	if size < 0 {
		size = 0
	}

	timeoutFallback := time.Duration(defaultClientQueueTimeoutMs) * time.Millisecond
	if noReadyWaitSeconds > 0 {
		timeoutFallback = time.Duration(noReadyWaitSeconds) * time.Second
	}
	timeout := envDurationMsAllowZero("GATEWAY_CLIENT_QUEUE_TIMEOUT_MS", timeoutFallback)

	retryFallback := time.Duration(defaultClientQueueRetryMs) * time.Millisecond
	if retryFallback <= 0 {
		retryFallback = time.Duration(defaultClientQueueRetryMs) * time.Millisecond
	}
	retry := envDurationMsAllowZero("GATEWAY_CLIENT_QUEUE_RETRY_MS", retryFallback)
	if retry <= 0 {
		retry = time.Millisecond
	}

	return clientQueueConfig{size: size, timeout: timeout, retry: retry}
}

func envDurationMsAllowZero(key string, fallback time.Duration) time.Duration {
	raw := strings.TrimSpace(os.Getenv(key))
	if raw == "" {
		return fallback
	}
	ms, err := strconv.Atoi(raw)
	if err != nil || ms < 0 {
		return fallback
	}
	return time.Duration(ms) * time.Millisecond
}

func newKubernetesBackendFromEnv() (*kubernetesBackend, error) {
	labelSelector := strings.TrimSpace(getEnv("KUBERNETES_BACKEND_LABEL_SELECTOR", ""))
	if labelSelector == "" {
		return nil, nil
	}

	namespace := strings.TrimSpace(getEnv("KUBERNETES_NAMESPACE", "default"))
	host := strings.TrimSpace(os.Getenv("KUBERNETES_SERVICE_HOST"))
	port := strings.TrimSpace(os.Getenv("KUBERNETES_SERVICE_PORT"))
	if host == "" || port == "" {
		return nil, errors.New("kubernetes service host/port env missing")
	}

	tokenBytes, err := os.ReadFile("/var/run/secrets/kubernetes.io/serviceaccount/token")
	if err != nil {
		return nil, fmt.Errorf("read serviceaccount token: %w", err)
	}
	caBytes, err := os.ReadFile("/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")
	if err != nil {
		return nil, fmt.Errorf("read serviceaccount CA: %w", err)
	}
	pool := x509.NewCertPool()
	if !pool.AppendCertsFromPEM(caBytes) {
		return nil, errors.New("unable to parse kubernetes CA")
	}

	transport := &http.Transport{
		TLSClientConfig: &tls.Config{RootCAs: pool},
	}
	apiTimeout := time.Duration(getEnvInt("KUBERNETES_API_TIMEOUT_MS", 200)) * time.Millisecond
	if apiTimeout <= 0 {
		apiTimeout = 200 * time.Millisecond
	}
	deleteAfterUseDefault := true
	if strings.TrimSpace(os.Getenv("DCMB_TICKET_IDENTITY_KEY")) != "" {
		deleteAfterUseDefault = false
	}

	return &kubernetesBackend{
		namespace:      namespace,
		labelSelector:  labelSelector,
		deleteAfterUse: getEnvBool("KUBERNETES_DELETE_AFTER_USE", deleteAfterUseDefault),
		apiServer:      "https://" + net.JoinHostPort(host, port),
		apiTimeout:     apiTimeout,
		httpClient:     &http.Client{Timeout: apiTimeout, Transport: transport},
		bearerToken:    strings.TrimSpace(string(tokenBytes)),
	}, nil
}

func (k *kubernetesBackend) doRequest(ctx context.Context, method string, path string) (*http.Response, error) {
	req, err := http.NewRequestWithContext(ctx, method, k.apiServer+path, nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Authorization", "Bearer "+k.bearerToken)
	return k.httpClient.Do(req)
}

func (k *kubernetesBackend) listReadyBackends() ([]backendTarget, error) {
	ctx, cancel := context.WithTimeout(context.Background(), k.apiTimeout)
	defer cancel()

	path := "/api/v1/namespaces/" + k.namespace + "/pods?labelSelector=" + urlQueryEscape(k.labelSelector)
	resp, err := k.doRequest(ctx, http.MethodGet, path)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
		return nil, fmt.Errorf("list pods failed: %s: %s", resp.Status, strings.TrimSpace(string(body)))
	}

	var podList kubernetesPodList
	if err := json.NewDecoder(resp.Body).Decode(&podList); err != nil {
		return nil, err
	}

	backends := make([]backendTarget, 0, len(podList.Items))
	for _, pod := range podList.Items {
		if pod.Status.Phase != "Running" || pod.Status.PodIP == "" || !podReady(pod) {
			continue
		}
		backends = append(backends, backendTarget{name: pod.Metadata.Name, ip: pod.Status.PodIP})
	}
	return backends, nil
}

func (k *kubernetesBackend) deleteBackend(name string) error {
	ctx, cancel := context.WithTimeout(context.Background(), k.apiTimeout)
	defer cancel()

	path := "/api/v1/namespaces/" + k.namespace + "/pods/" + name
	resp, err := k.doRequest(ctx, http.MethodDelete, path)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK && resp.StatusCode != http.StatusAccepted && resp.StatusCode != http.StatusNotFound {
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
		return fmt.Errorf("delete pod failed: %s: %s", resp.Status, strings.TrimSpace(string(body)))
	}
	return nil
}

func podReady(pod kubernetesPod) bool {
	for _, condition := range pod.Status.Conditions {
		if condition.Type == "Ready" && condition.Status == "True" {
			return true
		}
	}
	return false
}

func urlQueryEscape(value string) string {
	replacer := strings.NewReplacer(
		"%", "%25",
		" ", "%20",
		"=", "%3D",
		",", "%2C",
	)
	return replacer.Replace(value)
}

func isReady(client *http.Client, ip string) bool {
	url := "http://" + net.JoinHostPort(ip, operatorReadinessPort) + "/ready"
	resp, err := client.Get(url)
	if err != nil {
		return false
	}
	defer resp.Body.Close()
	return resp.StatusCode == http.StatusOK
}

func markAssigned(ip string, timeout time.Duration) {
	client := &http.Client{Timeout: timeout}
	url := "http://" + net.JoinHostPort(ip, operatorReadinessPort) + "/assign"
	req, _ := http.NewRequest(http.MethodPost, url, nil)
	resp, err := client.Do(req)
	if err != nil {
		return
	}
	_ = resp.Body.Close()
}

func containsIP(ips []string, target string) bool {
	if target == "" {
		return false
	}
	for _, ip := range ips {
		if ip == target {
			return true
		}
	}
	return false
}

func copyWithOrder(ips []string, policy string) []string {
	ordered := make([]string, len(ips))
	copy(ordered, ips)

	switch policy {
	case "last":
		for i, j := 0, len(ordered)-1; i < j; i, j = i+1, j-1 {
			ordered[i], ordered[j] = ordered[j], ordered[i]
		}
	}

	return ordered
}

func (p *backendPool) pickReadyBackend() (backendTarget, error) {
	if p.docker != nil {
		return p.docker.pickReadyBackend()
	}
	if p.k8s != nil {
		return p.pickReadyKubernetesBackend()
	}

	ips, err := net.LookupHost(p.taskHost)
	if err != nil {
		return backendTarget{}, fmt.Errorf("dns lookup failed for %s: %w", p.taskHost, err)
	}
	if len(ips) == 0 {
		return backendTarget{}, errors.New("no operator IP found")
	}
	readyClient := &http.Client{Timeout: 400 * time.Millisecond}

	p.mu.Lock()
	lastGood := p.lastGood
	policy := p.policy
	order := copyWithOrder(ips, policy)
	if policy == "random" {
		p.rng.Shuffle(len(order), func(i, j int) {
			order[i], order[j] = order[j], order[i]
		})
	}
	p.mu.Unlock()

	if containsIP(ips, lastGood) && !p.isBound(lastGood) && isReady(readyClient, lastGood) {
		return backendTarget{name: lastGood, ip: lastGood}, nil
	}

	for _, ip := range order {
		if p.isBound(ip) {
			continue
		}
		if isReady(readyClient, ip) {
			p.mu.Lock()
			p.lastGood = ip
			p.mu.Unlock()
			return backendTarget{name: ip, ip: ip}, nil
		}
	}
	return backendTarget{}, errors.New("no ready operator")
}

func (p *backendPool) pickReadyKubernetesBackend() (backendTarget, error) {
	backends, err := p.k8s.listReadyBackends()
	if err != nil {
		return backendTarget{}, err
	}
	if len(backends) == 0 {
		return backendTarget{}, errors.New("no ready operator")
	}

	p.mu.Lock()
	defer p.mu.Unlock()

	readyByName := make(map[string]struct{}, len(backends))
	for _, backend := range backends {
		readyByName[backend.name] = struct{}{}
	}
	for leasedName := range p.leased {
		if _, stillReady := readyByName[leasedName]; !stillReady {
			delete(p.leased, leasedName)
		}
	}
	for boundName := range p.bound {
		if _, stillReady := readyByName[boundName]; !stillReady {
			delete(p.bound, boundName)
		}
	}

	order := make([]backendTarget, len(backends))
	copy(order, backends)
	if p.policy == "random" {
		p.rng.Shuffle(len(order), func(i, j int) {
			order[i], order[j] = order[j], order[i]
		})
	} else if p.policy == "last" {
		for i, j := 0, len(order)-1; i < j; i, j = i+1, j-1 {
			order[i], order[j] = order[j], order[i]
		}
	}

	if p.lastGood != "" {
		for _, backend := range order {
			if backend.name == p.lastGood {
				if _, leased := p.leased[backend.name]; !leased {
					if _, bound := p.bound[backend.name]; bound {
						break
					}
					p.leased[backend.name] = struct{}{}
					return backend, nil
				}
				break
			}
		}
	}

	for _, backend := range order {
		if _, leased := p.leased[backend.name]; leased {
			continue
		}
		if _, bound := p.bound[backend.name]; bound {
			continue
		}
		p.lastGood = backend.name
		p.leased[backend.name] = struct{}{}
		return backend, nil
	}

	return backendTarget{}, errors.New("no ready operator available (all leased)")
}

func (p *backendPool) isBound(name string) bool {
	p.mu.Lock()
	defer p.mu.Unlock()
	_, ok := p.bound[name]
	return ok
}

func (p *backendPool) bindBackend(name string) {
	if name == "" {
		return
	}
	if p.docker != nil {
		p.docker.bindBackend(name)
		return
	}
	p.mu.Lock()
	p.bound[name] = struct{}{}
	p.mu.Unlock()
}

func (p *backendPool) releaseLease(name string) {
	if name == "" {
		return
	}
	if p.docker != nil {
		p.docker.releaseBackend(name)
		return
	}
	p.mu.Lock()
	defer p.mu.Unlock()
	delete(p.leased, name)
}

func (p *backendPool) finishBackend(backend backendTarget, failed bool) {
	if backend.name == "" {
		return
	}
	if p.docker != nil {
		p.docker.finishBackend(backend.name, failed)
		return
	}

	p.releaseLease(backend.name)
	if p.k8s != nil && p.k8s.deleteAfterUse {
		if err := p.k8s.deleteBackend(backend.name); err != nil {
			info("[GATEWAY] delete backend failed: " + err.Error())
		} else {
			info("[GATEWAY] deleted backend pod " + backend.name)
		}
	}
}

func pickBackendWithPolicy(pool *backendPool, policy string, waitSeconds int, retryMs int) (backendTarget, error) {
	backend, err := pool.pickReadyBackend()
	if err == nil || policy != "wait" || waitSeconds <= 0 {
		return backend, err
	}

	deadline := time.Now().Add(time.Duration(waitSeconds) * time.Second)
	for time.Now().Before(deadline) {
		backend, err = pool.pickReadyBackend()
		if err == nil {
			return backend, nil
		}
		time.Sleep(time.Duration(retryMs) * time.Millisecond)
	}
	return backendTarget{}, err
}

func pickBackendForClient(pool *backendPool, affinity *ticketAffinity, hello *tlshello.Info, policy string, waitSeconds int, retryMs int) (backendTarget, bool, error) {
	if backend, ok := affinity.lookup(hello); ok {
		return backend, true, nil
	}

	backend, err := pickBackendWithPolicy(pool, policy, waitSeconds, retryMs)
	if err != nil {
		return backendTarget{}, false, err
	}
	bindTicketBackend(pool, affinity, backend, hello)
	return backend, false, nil
}

func pickBackendUntil(ctx context.Context, pool *backendPool, deadline time.Time, retry time.Duration) (backendTarget, error) {
	var lastErr error
	for {
		if ctx.Err() != nil {
			return backendTarget{}, ctx.Err()
		}

		backend, err := pool.pickReadyBackend()
		if err == nil {
			return backend, nil
		}
		lastErr = err

		wait := retry
		if !deadline.IsZero() {
			remaining := time.Until(deadline)
			if remaining <= 0 {
				return backendTarget{}, lastErr
			}
			if remaining < wait {
				wait = remaining
			}
		}
		select {
		case <-ctx.Done():
			return backendTarget{}, ctx.Err()
		case <-time.After(wait):
		}
	}
}

func pickBackendUntilForClient(ctx context.Context, pool *backendPool, affinity *ticketAffinity, hello *tlshello.Info, deadline time.Time, retry time.Duration) (backendTarget, bool, error) {
	if backend, ok := affinity.lookup(hello); ok {
		return backend, true, nil
	}

	backend, err := pickBackendUntil(ctx, pool, deadline, retry)
	if err != nil {
		return backendTarget{}, false, err
	}
	bindTicketBackend(pool, affinity, backend, hello)
	return backend, false, nil
}

func bindTicketBackend(pool *backendPool, affinity *ticketAffinity, backend backendTarget, hello *tlshello.Info) {
	if affinity == nil {
		return
	}
	pool.bindBackend(backend.name)
	if hello == nil {
		return
	}
	if err := affinity.bind(backend, hello.ServerName); err != nil {
		info("[GATEWAY] ticket affinity bind failed: " + err.Error())
	}
}

func handleClientConn(connID string, c net.Conn, preface []byte, hello *tlshello.Info, pool *backendPool, st *gatewayState, noReadyPolicy string, noReadyWaitSeconds int, noReadyRetryMs int) {
	info(fmt.Sprintf("t26: [GATEWAY] - operator_selection_start = %d ns", time.Now().UnixNano()))
	benchtrace.Mark(benchtrace.GatewayWorkerSelectStart, connID, 0)
	backend, _, err := pickBackendForClient(pool, st.affinity, hello, noReadyPolicy, noReadyWaitSeconds, noReadyRetryMs)
	if err != nil {
		_ = c.Close()
		st.dropped.Add(1)
		st.lastResolution.Store(err.Error())
		benchtrace.Mark(benchtrace.GatewayRequestDropped, connID, 1)
		logClientDrop(err.Error(), pool, nil)
		return
	}
	benchtrace.Mark(benchtrace.GatewayWorkerSelectDone, connID, 0)

	serveClientWithBackend(connID, c, preface, pool, st, backend)
}

func serveClientWithBackend(connID string, c net.Conn, preface []byte, pool *backendPool, st *gatewayState, backend backendTarget) {
	defer c.Close()

	st.lastBackend.Store(backend.name)
	st.lastResolution.Store("ok")
	info(fmt.Sprintf("t27: [GATEWAY] - operator_selected = %d ns", time.Now().UnixNano()))

	backendAddr := net.JoinHostPort(backend.ip, operatorTLSPort)
	benchtrace.Mark(benchtrace.GatewayBackendDialStart, connID, 0)
	backendConn, dialErr := net.DialTimeout("tcp", backendAddr, 2*time.Second)
	if dialErr != nil {
		pool.finishBackend(backend, true)
		st.affinity.forgetBackend(backend.name)
		st.dropped.Add(1)
		st.lastResolution.Store(dialErr.Error())
		benchtrace.Mark(benchtrace.GatewayBackendDialDone, connID, 1)
		benchtrace.Mark(benchtrace.GatewayRequestDropped, connID, 2)
		logClientDrop("backend dial failed: "+dialErr.Error(), pool, nil)
		return
	}
	benchtrace.Mark(benchtrace.GatewayBackendDialDone, connID, 0)
	benchtrace.Mark(
		benchtrace.GatewayBackendConnectionBind,
		connID,
		tracebind.ConnectionKey(backendConn.LocalAddr(), backendConn.RemoteAddr()),
	)
	defer backendConn.Close()
	finishedBackend := false
	finishBackend := func(failed bool) {
		if finishedBackend {
			return
		}
		finishedBackend = true
		pool.finishBackend(backend, failed)
		if failed {
			st.affinity.forgetBackend(backend.name)
		}
	}
	defer func() {
		finishBackend(false)
	}()

	st.forwarded.Add(1)
	info(fmt.Sprintf("t28: [GATEWAY] - gateway_to_operator_send = %d ns", time.Now().UnixNano()))
	if len(preface) > 0 {
		benchtrace.Mark(benchtrace.GatewayClientToBackendFirst, connID, uint64(len(preface)))
		if _, err := backendConn.Write(preface); err != nil {
			finishBackend(true)
			st.dropped.Add(1)
			st.lastResolution.Store(err.Error())
			benchtrace.Mark(benchtrace.GatewayRequestDropped, connID, 6)
			logClientDrop("backend write failed: "+err.Error(), pool, nil)
			return
		}
	}
	benchtrace.Mark(benchtrace.GatewaySpliceStart, connID, 0)
	splice(c, backendConn, connID, len(preface) > 0)
	benchtrace.Mark(benchtrace.GatewaySpliceDone, connID, 0)
}

func logContainerStatus(event string, pool *backendPool, queue <-chan queuedClient) {
	if pool == nil || pool.docker == nil {
		return
	}

	queueLen := -1
	queueCap := -1
	if queue != nil {
		queueLen = len(queue)
		queueCap = cap(queue)
	}
	info("[GATEWAY] " + event + " " + pool.docker.statusString(queueLen, queueCap))
}

func logClientDrop(reason string, pool *backendPool, queue <-chan queuedClient) {
	if pool == nil || pool.docker == nil {
		info("[GATEWAY] request_dropped reason=" + strconv.Quote(reason))
		return
	}
	logContainerStatus("request_dropped reason="+strconv.Quote(reason), pool, queue)
}

func runClientQueue(ctx context.Context, queue <-chan queuedClient, cfg clientQueueConfig, pool *backendPool, st *gatewayState) {
	for {
		var client queuedClient
		select {
		case <-ctx.Done():
			drainClientQueue(queue)
			return
		case client = <-queue:
		}

		info(fmt.Sprintf("t26: [GATEWAY] - operator_selection_start = %d ns", time.Now().UnixNano()))
		benchtrace.Mark(benchtrace.GatewayQueueLeave, client.id, uint64(len(queue)))
		benchtrace.Mark(benchtrace.GatewayWorkerSelectStart, client.id, 0)
		backend, _, err := pickBackendUntilForClient(ctx, pool, st.affinity, client.hello, client.deadline, cfg.retry)
		if err != nil {
			_ = client.conn.Close()
			st.dropped.Add(1)
			st.lastResolution.Store("client queue timeout: " + err.Error())
			benchtrace.Mark(benchtrace.GatewayQueueTimeout, client.id, 0)
			benchtrace.Mark(benchtrace.GatewayRequestDropped, client.id, 3)
			logClientDrop("client queue timeout: "+err.Error(), pool, queue)
			continue
		}
		benchtrace.Mark(benchtrace.GatewayWorkerSelectDone, client.id, 0)

		go serveClientWithBackend(client.id, client.conn, client.preface, pool, st, backend)
	}
}

func drainClientQueue(queue <-chan queuedClient) {
	for {
		select {
		case client := <-queue:
			_ = client.conn.Close()
			benchtrace.Mark(benchtrace.GatewayRequestDropped, client.id, 4)
		default:
			return
		}
	}
}

type firstTraceReader struct {
	source net.Conn
	event  uint32
	id     string
	marked bool
}

func (r *firstTraceReader) Read(p []byte) (int, error) {
	n, err := r.source.Read(p)
	if n > 0 && !r.marked {
		r.marked = true
		benchtrace.Mark(r.event, r.id, uint64(n))
	}
	return n, err
}

func splice(a net.Conn, b net.Conn, connID string, clientPrefaceMarked bool) {
	var wg sync.WaitGroup
	wg.Add(2)

	go func() {
		defer wg.Done()
		_, _ = io.Copy(a, &firstTraceReader{
			source: b,
			event:  benchtrace.GatewayBackendToClientFirst,
			id:     connID,
		})
		_ = a.SetDeadline(time.Now())
	}()

	go func() {
		defer wg.Done()
		_, _ = io.Copy(b, &firstTraceReader{
			source: a,
			event:  benchtrace.GatewayClientToBackendFirst,
			id:     connID,
			marked: clientPrefaceMarked,
		})
		_ = b.SetDeadline(time.Now())
	}()

	wg.Wait()
}

func startHealthServer(st *gatewayState) {
	mux := http.NewServeMux()
	mux.HandleFunc("/health", func(w http.ResponseWriter, _ *http.Request) {
		payload := map[string]any{
			"started_at":      st.startedAt.Format(time.RFC3339),
			"accepted":        st.accepted.Load(),
			"forwarded":       st.forwarded.Load(),
			"dropped":         st.dropped.Load(),
			"last_backend":    st.lastBackend.Load(),
			"last_resolution": st.lastResolution.Load(),
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(payload)
	})

	go func() {
		if err := http.ListenAndServe(":8088", mux); err != nil {
			log.Printf("gateway health server exited: %v", err)
		}
	}()
}

func main() {
	tracePathFlag := flag.String("trace", "", "trace output file; requires build tag trace")
	traceBufferFlag := flag.Int("trace-buffer-events", 100000, "trace buffer capacity in events")
	traceDropFlag := flag.Bool("trace-drop-on-full", true, "drop trace events instead of blocking when trace buffer is full")
	flag.Parse()

	if err := benchtrace.Start(*tracePathFlag, *traceBufferFlag, *traceDropFlag); err != nil {
		log.Fatalf("trace start: %v", err)
	}
	defer benchtrace.Stop()

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	backendMode := strings.ToLower(getEnv("GATEWAY_BACKEND_MODE", "auto"))
	taskHost := getEnv("OPERATOR_TASKS_HOST", "tasks.tlmsp_mb_operator_warm")
	noReadyPolicy := strings.ToLower(getEnv("NO_READY_OPERATOR_POLICY", "drop"))
	noReadyWaitSeconds := getEnvInt("NO_READY_OPERATOR_WAIT_SECONDS", 0)
	noReadyRetryMs := getEnvInt("NO_READY_OPERATOR_RETRY_MS", 50)
	if noReadyRetryMs <= 0 {
		noReadyRetryMs = 50
	}
	clientQueueCfg := newClientQueueConfigFromEnv(noReadyWaitSeconds, noReadyRetryMs)
	selectionPolicy := strings.ToLower(getEnv("OPERATOR_SELECTION_POLICY", "first"))
	if selectionPolicy != "first" && selectionPolicy != "last" && selectionPolicy != "random" {
		selectionPolicy = "first"
	}

	var k8sBackend *kubernetesBackend
	var dockerBackend *dockerBackend
	switch backendMode {
	case "auto":
		var err error
		k8sBackend, err = newKubernetesBackendFromEnv()
		if err != nil {
			log.Fatal(err)
		}
	case "kubernetes", "k8s":
		var err error
		k8sBackend, err = newKubernetesBackendFromEnv()
		if err != nil {
			log.Fatal(err)
		}
		if k8sBackend == nil {
			log.Fatal("GATEWAY_BACKEND_MODE=kubernetes requires KUBERNETES_BACKEND_LABEL_SELECTOR")
		}
	case "docker":
		var err error
		dockerBackend, err = newDockerBackendFromEnv()
		if err != nil {
			log.Fatal(err)
		}
		if err := dockerBackend.start(ctx); err != nil {
			log.Fatal(err)
		}
	case "swarm", "dns":
	default:
		log.Fatalf("unsupported GATEWAY_BACKEND_MODE %q; use auto, docker, kubernetes, swarm, or dns", backendMode)
	}

	st := &gatewayState{startedAt: time.Now(), affinity: newTicketAffinityFromEnv()}
	st.lastBackend.Store("")
	st.lastResolution.Store("")
	startHealthServer(st)

	pool := &backendPool{
		taskHost: taskHost,
		rng:      rand.New(rand.NewSource(time.Now().UnixNano())),
		policy:   selectionPolicy,
		leased:   make(map[string]struct{}),
		bound:    make(map[string]struct{}),
		k8s:      k8sBackend,
		docker:   dockerBackend,
	}

	var clientQueue chan queuedClient
	if clientQueueCfg.size > 0 {
		clientQueue = make(chan queuedClient, clientQueueCfg.size)
		go runClientQueue(ctx, clientQueue, clientQueueCfg, pool, st)
	}
	var gatewayConnCounter atomic.Int64

	ln, err := net.Listen("tcp", gatewayListenAddr)
	if err != nil {
		log.Fatal(err)
	}
	defer ln.Close()
	go func() {
		<-ctx.Done()
		_ = ln.Close()
	}()
	if dockerBackend != nil {
		defer func() {
			cleanupCtx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
			defer cancel()
			dockerBackend.shutdown(cleanupCtx)
		}()
	}

	if dockerBackend != nil {
		info("[GATEWAY] listening on " + gatewayListenAddr + " using docker backend image=" + dockerBackend.image + " network=" + dockerBackend.networkName + " min_ready=" + strconv.Itoa(dockerBackend.minReady) + " scale_up_by=" + strconv.Itoa(dockerBackend.scaleUpBy))
	} else if k8sBackend != nil {
		info("[GATEWAY] listening on " + gatewayListenAddr + " using kubernetes backend selector=" + k8sBackend.labelSelector + " selection_policy=" + selectionPolicy + " api_timeout=" + k8sBackend.apiTimeout.String())
	} else {
		info("[GATEWAY] listening on " + gatewayListenAddr + " using task host " + taskHost + " selection_policy=" + selectionPolicy)
	}
	if clientQueue != nil {
		info("[GATEWAY] client queue enabled size=" + strconv.Itoa(clientQueueCfg.size) + " timeout=" + clientQueueCfg.timeout.String() + " retry=" + clientQueueCfg.retry.String())
	}
	if st.affinity != nil {
		info("[GATEWAY] deterministic TLS ticket affinity enabled")
	}
	fmt.Fprintf(os.Stderr, "[GATEWAY_READY] listening=%s mode=%s\n", gatewayListenAddr, backendMode)

	for {
		clientConn, err := ln.Accept()
		if err != nil {
			if ctx.Err() != nil {
				info("[GATEWAY] shutting down")
				return
			}
			log.Printf("accept failed: %v", err)
			continue
		}

		st.accepted.Add(1)
		connID := fmt.Sprintf("gateway-conn-%d", gatewayConnCounter.Add(1))
		benchtrace.Mark(benchtrace.GatewayClientAccepted, connID, uint64(st.accepted.Load()))
		benchtrace.Mark(
			benchtrace.GatewayClientConnectionBind,
			connID,
			tracebind.ConnectionKey(clientConn.LocalAddr(), clientConn.RemoteAddr()),
		)
		info(fmt.Sprintf("t25: [GATEWAY] - client_to_gateway_received = %d ns", time.Now().UnixNano()))
		logContainerStatus("request_received", pool, clientQueue)

		var preface []byte
		var hello *tlshello.Info
		if st.affinity != nil {
			var peekErr error
			preface, hello, peekErr = peekClientHello(clientConn)
			if peekErr != nil {
				st.lastResolution.Store("clienthello peek: " + peekErr.Error())
			}
		}

		if clientQueue != nil {
			deadline := time.Time{}
			if clientQueueCfg.timeout > 0 {
				deadline = time.Now().Add(clientQueueCfg.timeout)
			}

			select {
			case clientQueue <- queuedClient{id: connID, conn: clientConn, deadline: deadline, preface: preface, hello: hello}:
				benchtrace.Mark(benchtrace.GatewayQueueEnter, connID, uint64(len(clientQueue)))
			default:
				_ = clientConn.Close()
				st.dropped.Add(1)
				st.lastResolution.Store("client queue full")
				benchtrace.Mark(benchtrace.GatewayQueueFull, connID, uint64(cap(clientQueue)))
				benchtrace.Mark(benchtrace.GatewayRequestDropped, connID, 5)
				logClientDrop("client queue full", pool, clientQueue)
			}
			continue
		}

		go handleClientConn(connID, clientConn, preface, hello, pool, st, noReadyPolicy, noReadyWaitSeconds, noReadyRetryMs)
	}
}
