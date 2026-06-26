package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"path"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	benchtrace "dc/middlebox/internal/trace"
)

const (
	defaultDockerSocket        = "/var/run/docker.sock"
	defaultDockerWorkerImage   = "dcmiddlebox-worker:baseline"
	defaultDockerWorkerNetwork = "dcmb-middlebox-net"
	defaultDockerNamePrefix    = "dcmb-worker"
	defaultDockerCertsPath     = "/home/bonsai/dcmb/certs_external"
)

var errDockerNotFound = errors.New("docker object not found")

func isDockerAPIStatus(err error, statusCode int) bool {
	var apiErr dockerAPIError
	return errors.As(err, &apiErr) && apiErr.statusCode == statusCode
}

type dockerBackend struct {
	api *dockerAPIClient

	image         string
	networkName   string
	createNetwork bool
	namePrefix    string
	runID         string
	certsHostPath string
	certsDestPath string
	serverHost    string

	operatorMode                string
	operatorDefaultSNI          string
	operatorTarget              string
	operatorCertURL             string
	operatorExitAfterRequest    string
	operatorConsumeAfterRequest string
	workerTraceEnabled          bool
	workerTraceHostDir          string
	workerTraceContainerDir     string

	minReady       int
	scaleUpBy      int
	maxWorkers     int
	deleteAfterUse bool

	readyTimeout     time.Duration
	readyPoll        time.Duration
	readyDialTimeout time.Duration
	refillPeriod     time.Duration

	mu       sync.Mutex
	workers  map[string]*dockerWorker
	ready    []string
	leased   map[string]struct{}
	creating int
	counter  atomic.Int64
	createWG sync.WaitGroup

	shuttingDown bool

	refillTrigger chan struct{}
}

type dockerWorker struct {
	id        string
	name      string
	ip        string
	createdAt time.Time
}

type dockerAPIClient struct {
	httpClient *http.Client
}

type dockerAPIError struct {
	statusCode int
	body       string
}

func (e dockerAPIError) Error() string {
	return fmt.Sprintf("docker api returned HTTP %d: %s", e.statusCode, e.body)
}

type dockerNetworkCreateRequest struct {
	Name       string            `json:"Name"`
	Driver     string            `json:"Driver"`
	Attachable bool              `json:"Attachable"`
	Labels     map[string]string `json:"Labels,omitempty"`
}

type dockerContainerCreateRequest struct {
	Image            string                 `json:"Image"`
	Cmd              []string               `json:"Cmd,omitempty"`
	Env              []string               `json:"Env,omitempty"`
	Labels           map[string]string      `json:"Labels,omitempty"`
	HostConfig       dockerHostConfig       `json:"HostConfig,omitempty"`
	NetworkingConfig dockerNetworkingConfig `json:"NetworkingConfig,omitempty"`
}

type dockerHostConfig struct {
	NetworkMode string   `json:"NetworkMode,omitempty"`
	Binds       []string `json:"Binds,omitempty"`
	ExtraHosts  []string `json:"ExtraHosts,omitempty"`
}

type dockerNetworkingConfig struct {
	EndpointsConfig map[string]map[string]any `json:"EndpointsConfig,omitempty"`
}

type dockerContainerCreateResponse struct {
	ID       string   `json:"Id"`
	Warnings []string `json:"Warnings"`
}

type dockerContainerInspectResponse struct {
	State struct {
		Running bool `json:"Running"`
	} `json:"State"`
	NetworkSettings struct {
		Networks map[string]struct {
			IPAddress string `json:"IPAddress"`
		} `json:"Networks"`
	} `json:"NetworkSettings"`
}

type dockerContainerListItem struct {
	ID string `json:"Id"`
}

func newDockerBackendFromEnv() (*dockerBackend, error) {
	api := newDockerAPIClient(dockerSocketPath(), envDurationMs("DOCKER_API_TIMEOUT_MS", 2000))
	if err := api.do(context.Background(), http.MethodGet, "/_ping", nil, nil); err != nil {
		return nil, err
	}

	minReady := getEnvIntWithFallback("DOCKER_MIN_READY_OPERATORS", getEnvInt("MIN_READY_OPERATORS", 1))
	scaleUpBy := getEnvIntWithFallback("DOCKER_SCALE_UP_BY", getEnvInt("SCALE_UP_BY", minReady))
	if scaleUpBy <= 0 {
		scaleUpBy = 1
	}
	if minReady < 0 {
		minReady = 0
	}

	return &dockerBackend{
		api: api,

		image:         getEnv("DOCKER_WORKER_IMAGE", defaultDockerWorkerImage),
		networkName:   getEnv("DOCKER_WORKER_NETWORK", defaultDockerWorkerNetwork),
		createNetwork: getEnvBool("DOCKER_CREATE_NETWORK", true),
		namePrefix:    getEnv("DOCKER_WORKER_NAME_PREFIX", defaultDockerNamePrefix),
		runID:         fmt.Sprintf("%d-%d", time.Now().UnixNano(), os.Getpid()),
		certsHostPath: getEnv("DOCKER_WORKER_CERTS_HOST_PATH", defaultDockerCertsPath),
		certsDestPath: getEnv("DOCKER_WORKER_CERTS_CONTAINER_PATH", defaultDockerCertsPath),
		serverHost:    strings.TrimSpace(os.Getenv("SERVER_HOST")),

		operatorMode:                getEnv("DOCKER_WORKER_OPERATOR_MODE", getEnv("OPERATOR_MODE", "warm")),
		operatorDefaultSNI:          getEnv("DOCKER_WORKER_OPERATOR_DEFAULT_SNI", getEnv("OPERATOR_DEFAULT_SNI", "server")),
		operatorTarget:              strings.TrimSpace(getEnv("DOCKER_WORKER_OPERATOR_TARGET", os.Getenv("OPERATOR_TARGET"))),
		operatorCertURL:             strings.TrimSpace(getEnv("DOCKER_WORKER_OPERATOR_CERT_URL", os.Getenv("OPERATOR_CERT_URL"))),
		operatorExitAfterRequest:    getEnv("DOCKER_WORKER_EXIT_AFTER_REQUEST", getEnv("OPERATOR_EXIT_AFTER_REQUEST", "false")),
		operatorConsumeAfterRequest: getEnv("DOCKER_WORKER_CONSUME_AFTER_REQUEST", getEnv("OPERATOR_CONSUME_AFTER_REQUEST", "false")),
		workerTraceEnabled:          getEnvBool("DOCKER_WORKER_TRACE_ENABLED", false),
		workerTraceHostDir:          strings.TrimSpace(getEnv("DOCKER_WORKER_TRACE_HOST_DIR", "/tmp/dcmb-traces")),
		workerTraceContainerDir:     strings.TrimSpace(getEnv("DOCKER_WORKER_TRACE_CONTAINER_DIR", "/trace")),

		minReady:       minReady,
		scaleUpBy:      scaleUpBy,
		maxWorkers:     getEnvInt("DOCKER_MAX_OPERATORS", getEnvInt("MAX_OPERATORS", 0)),
		deleteAfterUse: getEnvBool("DOCKER_DELETE_AFTER_USE", true),

		readyTimeout:     envDurationMs("DOCKER_READY_TIMEOUT_MS", 5000),
		readyPoll:        envDurationMs("DOCKER_READY_POLL_MS", 2),
		readyDialTimeout: envDurationMs("DOCKER_READY_DIAL_TIMEOUT_MS", 100),
		refillPeriod:     envDurationMs("DOCKER_REFILL_PERIOD_MS", 50),

		workers:       make(map[string]*dockerWorker),
		leased:        make(map[string]struct{}),
		refillTrigger: make(chan struct{}, 1),
	}, nil
}

func dockerSocketPath() string {
	dockerHost := strings.TrimSpace(os.Getenv("DOCKER_HOST"))
	if strings.HasPrefix(dockerHost, "unix://") {
		return strings.TrimPrefix(dockerHost, "unix://")
	}
	return getEnv("DOCKER_SOCKET", defaultDockerSocket)
}

func newDockerAPIClient(socketPath string, timeout time.Duration) *dockerAPIClient {
	dialer := &net.Dialer{Timeout: timeout}
	transport := &http.Transport{
		DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
			return dialer.DialContext(ctx, "unix", socketPath)
		},
	}

	return &dockerAPIClient{
		httpClient: &http.Client{
			Transport: transport,
			Timeout:   timeout,
		},
	}
}

func (c *dockerAPIClient) do(ctx context.Context, method string, path string, body any, out any) error {
	var reader io.Reader
	if body != nil {
		data, err := json.Marshal(body)
		if err != nil {
			return err
		}
		reader = bytes.NewReader(data)
	}

	req, err := http.NewRequestWithContext(ctx, method, "http://docker"+path, reader)
	if err != nil {
		return err
	}
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}

	resp, err := c.httpClient.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()

	if resp.StatusCode == http.StatusNotFound {
		_, _ = io.Copy(io.Discard, resp.Body)
		return errDockerNotFound
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		data, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
		return dockerAPIError{statusCode: resp.StatusCode, body: strings.TrimSpace(string(data))}
	}
	if out == nil {
		_, _ = io.Copy(io.Discard, resp.Body)
		return nil
	}
	return json.NewDecoder(resp.Body).Decode(out)
}

func getEnvIntWithFallback(key string, fallback int) int {
	raw := strings.TrimSpace(os.Getenv(key))
	if raw == "" {
		return fallback
	}
	value, err := strconv.Atoi(raw)
	if err != nil {
		return fallback
	}
	return value
}

func envDurationMs(key string, fallbackMs int) time.Duration {
	ms := getEnvInt(key, fallbackMs)
	if ms <= 0 {
		ms = fallbackMs
	}
	return time.Duration(ms) * time.Millisecond
}

func (d *dockerBackend) start(ctx context.Context) error {
	if d.networkName == "" {
		return errors.New("DOCKER_WORKER_NETWORK must not be empty")
	}
	if d.workerTraceEnabled && d.workerTraceHostDir != "" {
		if err := os.MkdirAll(d.workerTraceHostDir, 0o755); err != nil {
			return fmt.Errorf("create worker trace dir: %w", err)
		}
	}
	if err := d.ensureNetwork(ctx); err != nil {
		return err
	}

	go d.refillLoop(ctx)
	d.triggerRefill()
	return nil
}

func (d *dockerBackend) ensureNetwork(ctx context.Context) error {
	err := d.api.do(ctx, http.MethodGet, "/networks/"+url.PathEscape(d.networkName), nil, nil)
	if err == nil {
		return nil
	}
	if !errors.Is(err, errDockerNotFound) {
		return err
	}
	if !d.createNetwork {
		return fmt.Errorf("docker network %q not found", d.networkName)
	}

	req := dockerNetworkCreateRequest{
		Name:       d.networkName,
		Driver:     "bridge",
		Attachable: true,
		Labels: map[string]string{
			"dcmb.gateway.network": "true",
		},
	}
	return d.api.do(ctx, http.MethodPost, "/networks/create", req, nil)
}

func (d *dockerBackend) refillLoop(ctx context.Context) {
	ticker := time.NewTicker(d.refillPeriod)
	defer ticker.Stop()

	for {
		d.refillOnce()
		select {
		case <-ctx.Done():
			return
		case <-d.refillTrigger:
		case <-ticker.C:
		}
	}
}

func (d *dockerBackend) triggerRefill() {
	select {
	case d.refillTrigger <- struct{}{}:
	default:
	}
}

func (d *dockerBackend) refillOnce() {
	toCreate := d.reserveCreateSlots()
	for i := 0; i < toCreate; i++ {
		go func() {
			defer d.createWG.Done()
			d.createReadyWorker()
		}()
	}
}

func (d *dockerBackend) reserveCreateSlots() int {
	d.mu.Lock()
	defer d.mu.Unlock()

	if d.shuttingDown {
		return 0
	}

	readyCount := 0
	for _, worker := range d.workers {
		if _, leased := d.leased[worker.name]; !leased {
			readyCount++
		}
	}

	missing := d.minReady - readyCount - d.creating
	if missing <= 0 {
		return 0
	}

	toCreate := missing
	if toCreate > d.scaleUpBy {
		toCreate = d.scaleUpBy
	}
	if d.maxWorkers > 0 {
		capacity := d.maxWorkers - len(d.workers) - d.creating
		if capacity <= 0 {
			return 0
		}
		if toCreate > capacity {
			toCreate = capacity
		}
	}

	d.creating += toCreate
	d.createWG.Add(toCreate)
	return toCreate
}

func (d *dockerBackend) createReadyWorker() {
	start := time.Now()
	worker, err := d.createAndWaitReady()

	var removeAfterCreate bool
	d.mu.Lock()
	d.creating--
	if err == nil {
		if d.shuttingDown {
			removeAfterCreate = true
		} else {
			d.workers[worker.name] = worker
			d.ready = append(d.ready, worker.name)
		}
	}
	d.mu.Unlock()

	if err != nil {
		info("[GATEWAY] docker worker create failed: " + err.Error())
	} else if removeAfterCreate {
		info(fmt.Sprintf("[GATEWAY] docker worker created during shutdown name=%s ip=%s startup_ms=%d", worker.name, worker.ip, time.Since(start).Milliseconds()))
		go d.removeWorker(worker, true)
	} else {
		info(fmt.Sprintf("[GATEWAY] docker worker ready name=%s ip=%s startup_ms=%d", worker.name, worker.ip, time.Since(start).Milliseconds()))
	}

	d.triggerRefill()
}

func (d *dockerBackend) createAndWaitReady() (*dockerWorker, error) {
	name := fmt.Sprintf("%s-%s-%d", d.namePrefix, d.runID, d.counter.Add(1))
	labels := map[string]string{
		"dcmb.gateway.worker": "true",
		"dcmb.gateway.run":    d.runID,
	}

	cmd := []string{
		"-operator_id", name,
		"-operator_mode", d.operatorMode,
		"-operator_default_sni", d.operatorDefaultSNI,
		"-exit_after_request=" + d.operatorExitAfterRequest,
		"-consume_after_request=" + d.operatorConsumeAfterRequest,
	}
	if d.workerTraceEnabled && d.workerTraceContainerDir != "" {
		cmd = append(cmd, "-trace", path.Join(d.workerTraceContainerDir, name+".bin"))
	}

	req := dockerContainerCreateRequest{
		Image:  d.image,
		Labels: labels,
		Cmd:    cmd,
		Env:    d.workerEnv(),
		HostConfig: dockerHostConfig{
			NetworkMode: d.networkName,
		},
		NetworkingConfig: dockerNetworkingConfig{
			EndpointsConfig: map[string]map[string]any{
				d.networkName: {},
			},
		},
	}
	if d.certsHostPath != "" && d.certsDestPath != "" {
		req.HostConfig.Binds = append(req.HostConfig.Binds, d.certsHostPath+":"+d.certsDestPath+":ro")
	}
	if d.workerTraceEnabled && d.workerTraceHostDir != "" && d.workerTraceContainerDir != "" {
		req.HostConfig.Binds = append(req.HostConfig.Binds, d.workerTraceHostDir+":"+d.workerTraceContainerDir)
	}
	if d.serverHost != "" {
		req.HostConfig.ExtraHosts = append(req.HostConfig.ExtraHosts, "server:"+d.serverHost)
	}

	var created dockerContainerCreateResponse
	createPath := "/containers/create?name=" + url.QueryEscape(name)
	benchtrace.Mark(benchtrace.GatewayContainerCreate, name, 0)
	if err := d.api.do(context.Background(), http.MethodPost, createPath, req, &created); err != nil {
		benchtrace.Mark(benchtrace.GatewayContainerCreate, name, 1)
		return nil, err
	}
	if created.ID == "" {
		benchtrace.Mark(benchtrace.GatewayContainerCreate, name, 2)
		return nil, errors.New("docker create returned empty container id")
	}

	if err := d.api.do(context.Background(), http.MethodPost, "/containers/"+created.ID+"/start", nil, nil); err != nil {
		benchtrace.Mark(benchtrace.GatewayContainerStartDone, name, 1)
		d.removeContainerID(created.ID, "start-failed")
		return nil, err
	}
	benchtrace.Mark(benchtrace.GatewayContainerStartDone, name, 0)

	ip, err := d.waitContainerIP(created.ID)
	if err != nil {
		d.removeContainerID(created.ID, "ip-timeout")
		return nil, err
	}
	benchtrace.Mark(benchtrace.GatewayContainerIPAssigned, name, 0)

	if err := d.waitTCPReady(created.ID, ip); err != nil {
		d.removeContainerID(created.ID, "readiness-failed")
		return nil, err
	}
	benchtrace.Mark(benchtrace.GatewayContainerReady, name, 0)

	return &dockerWorker{
		id:        created.ID,
		name:      name,
		ip:        ip,
		createdAt: time.Now(),
	}, nil
}

func (d *dockerBackend) workerEnv() []string {
	env := make([]string, 0, 2)
	if d.operatorTarget != "" {
		env = append(env, "OPERATOR_TARGET="+d.operatorTarget)
	}
	if d.operatorCertURL != "" {
		env = append(env, "OPERATOR_CERT_URL="+d.operatorCertURL)
	}
	return env
}

func (d *dockerBackend) waitContainerIP(containerID string) (string, error) {
	deadline := time.Now().Add(d.readyTimeout)
	for time.Now().Before(deadline) {
		inspect, err := d.inspectContainer(containerID)
		if err != nil {
			return "", err
		}

		if !inspect.State.Running {
			return "", errors.New("container exited before IP assignment")
		}
		if networkState, ok := inspect.NetworkSettings.Networks[d.networkName]; ok && networkState.IPAddress != "" {
			return networkState.IPAddress, nil
		}
		time.Sleep(d.readyPoll)
	}
	return "", fmt.Errorf("timed out waiting for container IP on network %s", d.networkName)
}

func (d *dockerBackend) waitTCPReady(containerID string, ip string) error {
	deadline := time.Now().Add(d.readyTimeout)
	for time.Now().Before(deadline) {
		dialer := net.Dialer{Timeout: d.readyDialTimeout}
		conn, err := dialer.Dial("tcp", net.JoinHostPort(ip, operatorTLSPort))
		if err == nil {
			_ = conn.Close()
			return nil
		}

		running, statusErr := d.containerRunning(containerID)
		if statusErr == nil && !running {
			return fmt.Errorf("container exited before tcp readiness on %s:%s", ip, operatorTLSPort)
		}
		time.Sleep(d.readyPoll)
	}
	return fmt.Errorf("timed out waiting for tcp readiness on %s:%s", ip, operatorTLSPort)
}

func (d *dockerBackend) inspectContainer(containerID string) (dockerContainerInspectResponse, error) {
	var inspect dockerContainerInspectResponse
	err := d.api.do(context.Background(), http.MethodGet, "/containers/"+containerID+"/json", nil, &inspect)
	return inspect, err
}

func (d *dockerBackend) containerRunning(containerID string) (bool, error) {
	inspect, err := d.inspectContainer(containerID)
	if err != nil {
		return false, err
	}
	return inspect.State.Running, nil
}

func (d *dockerBackend) pickReadyBackend() (backendTarget, error) {
	d.mu.Lock()
	defer d.mu.Unlock()

	if d.shuttingDown {
		return backendTarget{}, errors.New("docker backend shutting down")
	}

	for len(d.ready) > 0 {
		name := d.ready[0]
		d.ready = d.ready[1:]
		worker, ok := d.workers[name]
		if !ok {
			continue
		}
		if _, leased := d.leased[name]; leased {
			continue
		}

		d.leased[name] = struct{}{}
		d.triggerRefill()
		return backendTarget{name: worker.name, ip: worker.ip}, nil
	}

	d.triggerRefill()
	return backendTarget{}, errors.New("no ready docker worker")
}

func (d *dockerBackend) releaseBackend(name string) {
	d.finishBackend(name, false)
}

func (d *dockerBackend) statusString(queueLen int, queueCap int) string {
	d.mu.Lock()
	defer d.mu.Unlock()

	readyCount := 0
	leasedCount := 0
	for _, worker := range d.workers {
		if _, leased := d.leased[worker.name]; leased {
			leasedCount++
			continue
		}
		readyCount++
	}

	status := fmt.Sprintf(
		"containers total=%d ready=%d ready_queue=%d leased=%d creating=%d min_ready=%d scale_up_by=%d max=%d",
		len(d.workers),
		readyCount,
		len(d.ready),
		leasedCount,
		d.creating,
		d.minReady,
		d.scaleUpBy,
		d.maxWorkers,
	)
	if queueCap >= 0 {
		status += fmt.Sprintf(" client_queue=%d/%d", queueLen, queueCap)
	}
	return status
}

func (d *dockerBackend) finishBackend(name string, failed bool) {
	d.mu.Lock()
	worker, ok := d.workers[name]
	if ok {
		delete(d.leased, name)
	}
	shouldDelete := ok && (failed || d.deleteAfterUse || d.shuttingDown)
	if shouldDelete {
		delete(d.workers, name)
	}
	if ok && !shouldDelete {
		d.ready = append(d.ready, name)
	}
	d.mu.Unlock()

	if shouldDelete {
		go d.removeWorker(worker, failed)
	}
	d.triggerRefill()
}

func (d *dockerBackend) shutdown(ctx context.Context) {
	info("[GATEWAY] cleaning up docker workers for run " + d.runID)

	d.mu.Lock()
	d.shuttingDown = true
	workers := make([]*dockerWorker, 0, len(d.workers))
	for _, worker := range d.workers {
		workers = append(workers, worker)
	}
	d.workers = make(map[string]*dockerWorker)
	d.ready = nil
	d.leased = make(map[string]struct{})
	d.mu.Unlock()

	done := make(chan struct{})
	go func() {
		d.createWG.Wait()
		close(done)
	}()

	createWait := d.readyTimeout + time.Second
	if createWait > 5*time.Second {
		createWait = 5 * time.Second
	}
	select {
	case <-done:
	case <-time.After(createWait):
		info("[GATEWAY] docker worker cleanup continuing while create operations finish")
	case <-ctx.Done():
		info("[GATEWAY] docker worker cleanup continuing after create wait timeout: " + ctx.Err().Error())
	}

	seen := make(map[string]struct{})
	for _, worker := range workers {
		if worker == nil || worker.id == "" {
			continue
		}
		seen[worker.id] = struct{}{}
		d.removeContainerIDContext(ctx, worker.id, "shutdown")
	}

	ids, err := d.listRunContainerIDs(ctx)
	if err != nil {
		info("[GATEWAY] docker cleanup list failed: " + err.Error())
		return
	}
	for _, id := range ids {
		if _, ok := seen[id]; ok {
			continue
		}
		d.removeContainerIDContext(ctx, id, "shutdown-label")
	}
}

func (d *dockerBackend) listRunContainerIDs(ctx context.Context) ([]string, error) {
	filters := map[string][]string{
		"label": {"dcmb.gateway.run=" + d.runID},
	}
	filterBytes, err := json.Marshal(filters)
	if err != nil {
		return nil, err
	}

	var containers []dockerContainerListItem
	path := "/containers/json?all=true&filters=" + url.QueryEscape(string(filterBytes))
	if err := d.api.do(ctx, http.MethodGet, path, nil, &containers); err != nil {
		return nil, err
	}

	ids := make([]string, 0, len(containers))
	for _, container := range containers {
		if container.ID != "" {
			ids = append(ids, container.ID)
		}
	}
	return ids, nil
}

func (d *dockerBackend) removeWorker(worker *dockerWorker, failed bool) {
	reason := "after-use"
	if failed {
		reason = "failed"
	}
	d.removeContainerID(worker.id, reason)
}

func (d *dockerBackend) removeContainerID(containerID string, reason string) {
	d.removeContainerIDContext(context.Background(), containerID, reason)
}

func (d *dockerBackend) removeContainerIDContext(ctx context.Context, containerID string, reason string) {
	benchtrace.Mark(benchtrace.GatewayContainerRemove, containerID, 0)
	stopPath := "/containers/" + containerID + "/stop?t=1"
	stopErr := d.api.do(ctx, http.MethodPost, stopPath, nil, nil)
	if stopErr != nil && !errors.Is(stopErr, errDockerNotFound) && !isDockerAPIStatus(stopErr, http.StatusNotModified) {
		info("[GATEWAY] docker graceful stop failed reason=" + reason + " id=" + containerID + " err=" + stopErr.Error())
	}

	err := d.api.do(ctx, http.MethodDelete, "/containers/"+containerID+"?v=true", nil, nil)
	if err != nil && !errors.Is(err, errDockerNotFound) {
		info("[GATEWAY] docker graceful remove failed reason=" + reason + " id=" + containerID + " err=" + err.Error())
		err = d.api.do(ctx, http.MethodDelete, "/containers/"+containerID+"?force=true&v=true", nil, nil)
	}
	info("[GATEWAY] docker remove container id=" + containerID + " reason=" + reason)
	if err != nil && !errors.Is(err, errDockerNotFound) {
		benchtrace.Mark(benchtrace.GatewayContainerRemoved, containerID, 1)
		info("[GATEWAY] docker remove failed reason=" + reason + " id=" + containerID + " err=" + err.Error())
		return
	}
	benchtrace.Mark(benchtrace.GatewayContainerRemoved, containerID, 0)
}
