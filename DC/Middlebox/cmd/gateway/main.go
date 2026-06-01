// middlebox gateway
package main

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"math/rand"
	"net"
	"net/http"
	"os"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

const (
	gatewayListenAddr     = ":9443"
	operatorTLSPort       = "8443"
	operatorReadinessPort = "18080"
)

type gatewayState struct {
	startedAt      time.Time
	accepted       atomic.Int64
	forwarded      atomic.Int64
	dropped        atomic.Int64
	lastBackend    atomic.Value
	lastResolution atomic.Value
}

type backendPool struct {
	taskHost string
	rng      *rand.Rand
	mu       sync.Mutex
	policy   string
	lastGood string
	leased   map[string]struct{}
	k8s      *kubernetesBackend
}

type backendTarget struct {
	name string
	ip   string
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

	return &kubernetesBackend{
		namespace:      namespace,
		labelSelector:  labelSelector,
		deleteAfterUse: getEnvBool("KUBERNETES_DELETE_AFTER_USE", true),
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

	if containsIP(ips, lastGood) && isReady(readyClient, lastGood) {
		return backendTarget{name: lastGood, ip: lastGood}, nil
	}

	for _, ip := range order {
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
		p.lastGood = backend.name
		p.leased[backend.name] = struct{}{}
		return backend, nil
	}

	return backendTarget{}, errors.New("no ready operator available (all leased)")
}

func (p *backendPool) releaseLease(name string) {
	if name == "" {
		return
	}
	p.mu.Lock()
	defer p.mu.Unlock()
	delete(p.leased, name)
}

func splice(a net.Conn, b net.Conn) {
	var wg sync.WaitGroup
	wg.Add(2)

	go func() {
		defer wg.Done()
		_, _ = io.Copy(a, b)
		_ = a.SetDeadline(time.Now())
	}()

	go func() {
		defer wg.Done()
		_, _ = io.Copy(b, a)
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
	taskHost := getEnv("OPERATOR_TASKS_HOST", "tasks.tlmsp_mb_operator_warm")
	noReadyPolicy := strings.ToLower(getEnv("NO_READY_OPERATOR_POLICY", "drop"))
	noReadyWaitSeconds := getEnvInt("NO_READY_OPERATOR_WAIT_SECONDS", 0)
	noReadyRetryMs := getEnvInt("NO_READY_OPERATOR_RETRY_MS", 50)
	if noReadyRetryMs <= 0 {
		noReadyRetryMs = 50
	}
	selectionPolicy := strings.ToLower(getEnv("OPERATOR_SELECTION_POLICY", "first"))
	if selectionPolicy != "first" && selectionPolicy != "last" && selectionPolicy != "random" {
		selectionPolicy = "first"
	}

	k8sBackend, err := newKubernetesBackendFromEnv()
	if err != nil {
		log.Fatal(err)
	}

	st := &gatewayState{startedAt: time.Now()}
	st.lastBackend.Store("")
	st.lastResolution.Store("")
	startHealthServer(st)

	pool := &backendPool{
		taskHost: taskHost,
		rng:      rand.New(rand.NewSource(time.Now().UnixNano())),
		policy:   selectionPolicy,
		leased:   make(map[string]struct{}),
		k8s:      k8sBackend,
	}

	ln, err := net.Listen("tcp", gatewayListenAddr)
	if err != nil {
		log.Fatal(err)
	}
	defer ln.Close()

	if k8sBackend != nil {
		info("[GATEWAY] listening on " + gatewayListenAddr + " using kubernetes backend selector=" + k8sBackend.labelSelector + " selection_policy=" + selectionPolicy + " api_timeout=" + k8sBackend.apiTimeout.String())
	} else {
		info("[GATEWAY] listening on " + gatewayListenAddr + " using task host " + taskHost + " selection_policy=" + selectionPolicy)
	}

	for {
		clientConn, err := ln.Accept()
		if err != nil {
			log.Printf("accept failed: %v", err)
			continue
		}

		st.accepted.Add(1)
		go func(c net.Conn) {
			defer c.Close()
			info(fmt.Sprintf("t25: [GATEWAY] - client_to_gateway_received = %d ns", time.Now().UnixNano()))

			info(fmt.Sprintf("t26: [GATEWAY] - operator_selection_start = %d ns", time.Now().UnixNano()))
			backend, err := pool.pickReadyBackend()
			if err != nil && noReadyPolicy == "wait" && noReadyWaitSeconds > 0 {
				deadline := time.Now().Add(time.Duration(noReadyWaitSeconds) * time.Second)
				for time.Now().Before(deadline) {
					backend, err = pool.pickReadyBackend()
					if err == nil {
						break
					}
					time.Sleep(time.Duration(noReadyRetryMs) * time.Millisecond)
				}
			}

			if err != nil {
				st.dropped.Add(1)
				st.lastResolution.Store(err.Error())
				return
			}

			st.lastBackend.Store(backend.name)
			st.lastResolution.Store("ok")
			info(fmt.Sprintf("t27: [GATEWAY] - operator_selected = %d ns", time.Now().UnixNano()))

			backendAddr := net.JoinHostPort(backend.ip, operatorTLSPort)
			backendConn, dialErr := net.DialTimeout("tcp", backendAddr, 2*time.Second)
			if dialErr != nil {
				pool.releaseLease(backend.name)
				st.dropped.Add(1)
				st.lastResolution.Store(dialErr.Error())
				if pool.k8s != nil && pool.k8s.deleteAfterUse {
					if err := pool.k8s.deleteBackend(backend.name); err != nil {
						info("[GATEWAY] delete backend after dial failure: " + err.Error())
					}
				}
				return
			}
			defer backendConn.Close()
			defer func() {
				pool.releaseLease(backend.name)
				if pool.k8s != nil && pool.k8s.deleteAfterUse {
					if err := pool.k8s.deleteBackend(backend.name); err != nil {
						info("[GATEWAY] delete backend failed: " + err.Error())
					} else {
						info("[GATEWAY] deleted backend pod " + backend.name)
					}
				}
			}()

			st.forwarded.Add(1)
			info(fmt.Sprintf("t28: [GATEWAY] - gateway_to_operator_send = %d ns", time.Now().UnixNano()))
			splice(c, backendConn)
		}(clientConn)
	}
}
