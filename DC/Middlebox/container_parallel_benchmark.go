package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"os/exec"
	"strconv"
	"strings"
	"sync"
	"time"
)

const (
	defaultImage         = "dcmiddlebox-worker:baseline"
	defaultCertsMount    = "/home/bonsai/dcmb/certs_external"
	defaultContainerPort = 8443
	defaultReadyPort     = 18080
	defaultReadyPath     = "/ready"
)

type stringList []string

func (s *stringList) String() string {
	return strings.Join(*s, ",")
}

func (s *stringList) Set(value string) error {
	*s = append(*s, value)
	return nil
}

type config struct {
	maxContainers int
	image         string
	containerPort int
	readiness     string
	readyPort     int
	readyPath     string
	timeout       time.Duration
	pollInterval  time.Duration
	certsMount    string
	network       string
	namePrefix    string
	keep          bool
	extraRunArgs  stringList
}

type containerResult struct {
	index    int
	id       string
	name     string
	hostPort string
	err      error
}

func main() {
	cfg := parseFlags()

	if cfg.maxContainers < 1 {
		exitf("-n must be at least 1")
	}
	if cfg.timeout <= 0 {
		exitf("-timeout must be greater than 0")
	}
	if cfg.pollInterval <= 0 {
		exitf("-poll must be greater than 0")
	}
	if cfg.readiness != "tcp" && cfg.readiness != "http" {
		exitf("-readiness must be tcp or http")
	}

	runID := fmt.Sprintf("%d-%d", time.Now().UnixNano(), os.Getpid())

	fmt.Println("containers,ms")
	for count := 1; count <= cfg.maxContainers; count++ {
		elapsed, results, err := benchmarkCount(cfg, runID, count)
		if cleanupErr := cleanupContainers(cfg, results); cleanupErr != nil {
			fmt.Fprintf(os.Stderr, "cleanup warning: %v\n", cleanupErr)
		}
		if err != nil {
			printContainerFailures(results)
			exitf("benchmark failed for %d containers: %v", count, err)
		}

		fmt.Printf("%d,%d\n", count, elapsed.Milliseconds())
	}
}

func parseFlags() config {
	cfg := config{}

	flag.IntVar(&cfg.maxContainers, "n", 10, "maximum number of containers to benchmark")
	flag.StringVar(&cfg.image, "image", defaultImage, "Docker image to run")
	flag.IntVar(&cfg.containerPort, "port", defaultContainerPort, "container TCP port used for tcp readiness")
	flag.StringVar(&cfg.readiness, "readiness", "tcp", "readiness check: tcp or http")
	flag.IntVar(&cfg.readyPort, "ready-port", defaultReadyPort, "container HTTP readiness port")
	flag.StringVar(&cfg.readyPath, "ready-path", defaultReadyPath, "HTTP readiness path")
	flag.DurationVar(&cfg.timeout, "timeout", 30*time.Second, "readiness timeout for each parallel benchmark step")
	flag.DurationVar(&cfg.pollInterval, "poll", time.Millisecond, "readiness polling interval")
	flag.StringVar(&cfg.certsMount, "certs", defaultCertsMount, "host certs_external path to mount read-only; empty disables the mount")
	flag.StringVar(&cfg.network, "network", "", "optional Docker network")
	flag.StringVar(&cfg.namePrefix, "name-prefix", "dcmb-worker-bench", "container name prefix")
	flag.BoolVar(&cfg.keep, "keep", false, "keep containers after each benchmark step")
	flag.Var(&cfg.extraRunArgs, "docker-arg", "extra docker run argument; repeat for multiple arguments")
	flag.Parse()

	cfg.readiness = strings.ToLower(strings.TrimSpace(cfg.readiness))
	if cfg.readyPath != "" && !strings.HasPrefix(cfg.readyPath, "/") {
		cfg.readyPath = "/" + cfg.readyPath
	}
	return cfg
}

func benchmarkCount(cfg config, runID string, count int) (time.Duration, []containerResult, error) {
	ctx, cancel := context.WithTimeout(context.Background(), cfg.timeout)
	defer cancel()

	startGate := make(chan struct{})
	results := make([]containerResult, count)
	var wg sync.WaitGroup

	for i := 0; i < count; i++ {
		wg.Add(1)
		go func(index int) {
			defer wg.Done()
			<-startGate
			results[index] = runAndWaitReady(ctx, cfg, runID, count, index+1)
		}(i)
	}

	start := time.Now()
	close(startGate)
	wg.Wait()
	elapsed := time.Since(start)

	var errs []error
	for _, result := range results {
		if result.err != nil {
			errs = append(errs, fmt.Errorf("%s: %w", result.name, result.err))
		}
	}

	return elapsed, results, errors.Join(errs...)
}

func runAndWaitReady(ctx context.Context, cfg config, runID string, count, index int) containerResult {
	name := fmt.Sprintf("%s-%s-%d-%d", cfg.namePrefix, runID, count, index)
	result := containerResult{
		index: index,
		name:  name,
	}

	readyContainerPort := cfg.containerPort
	if cfg.readiness == "http" {
		readyContainerPort = cfg.readyPort
	}

	args := []string{"run", "-d", "--name", name}
	args = append(args, "--label", "dcmb.parallel-benchmark=true")
	args = append(args, "--label", "dcmb.parallel-benchmark.run="+runID)
	if cfg.certsMount != "" {
		args = append(args, "-v", cfg.certsMount+":/home/bonsai/dcmb/certs_external:ro")
	}
	if cfg.network != "" {
		args = append(args, "--network", cfg.network)
	}
	args = append(args, cfg.extraRunArgs...)
	args = append(args, "-p", fmt.Sprintf("127.0.0.1::%d", readyContainerPort), cfg.image)

	id, err := dockerOutput(ctx, args...)
	if err != nil {
		result.err = err
		return result
	}
	result.id = strings.TrimSpace(id)

	hostPort, err := inspectHostPort(ctx, result.id, readyContainerPort)
	if err != nil {
		result.err = err
		return result
	}
	result.hostPort = hostPort

	if err := waitReady(ctx, cfg, result.id, hostPort); err != nil {
		result.err = err
	}

	return result
}

func inspectHostPort(ctx context.Context, containerID string, containerPort int) (string, error) {
	template := fmt.Sprintf(`{{(index (index .NetworkSettings.Ports "%d/tcp") 0).HostPort}}`, containerPort)
	out, err := dockerOutput(ctx, "inspect", "-f", template, containerID)
	if err != nil {
		return "", err
	}

	hostPort := strings.TrimSpace(out)
	if _, err := strconv.Atoi(hostPort); err != nil {
		return "", fmt.Errorf("invalid published host port %q: %w", hostPort, err)
	}

	return hostPort, nil
}

func waitReady(ctx context.Context, cfg config, containerID, hostPort string) error {
	ticker := time.NewTicker(cfg.pollInterval)
	defer ticker.Stop()

	for {
		var err error
		if cfg.readiness == "http" {
			err = checkHTTPReady(ctx, hostPort, cfg.readyPath)
		} else {
			err = checkTCPReady(ctx, hostPort)
		}
		if err == nil {
			return nil
		}

		if running, statusErr := containerRunning(ctx, containerID); statusErr == nil && !running {
			logs, _ := dockerOutput(context.Background(), "logs", "--tail", "40", containerID)
			return fmt.Errorf("container exited before ready; recent logs:\n%s", strings.TrimSpace(logs))
		}

		select {
		case <-ctx.Done():
			return fmt.Errorf("timed out waiting for readiness on localhost:%s: %w", hostPort, ctx.Err())
		case <-ticker.C:
		}
	}
}

func checkTCPReady(ctx context.Context, hostPort string) error {
	var d net.Dialer
	conn, err := d.DialContext(ctx, "tcp", net.JoinHostPort("127.0.0.1", hostPort))
	if err != nil {
		return err
	}
	_ = conn.Close()
	return nil
}

func checkHTTPReady(ctx context.Context, hostPort, readyPath string) error {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, "http://127.0.0.1:"+hostPort+readyPath, nil)
	if err != nil {
		return err
	}

	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	_, _ = io.Copy(io.Discard, resp.Body)

	if resp.StatusCode < http.StatusOK || resp.StatusCode >= http.StatusMultipleChoices {
		return fmt.Errorf("readiness returned HTTP %d", resp.StatusCode)
	}

	return nil
}

func containerRunning(ctx context.Context, containerID string) (bool, error) {
	out, err := dockerOutput(ctx, "inspect", "-f", "{{.State.Running}}", containerID)
	if err != nil {
		return false, err
	}
	return strings.TrimSpace(out) == "true", nil
}

func cleanupContainers(cfg config, results []containerResult) error {
	if cfg.keep {
		return nil
	}

	var errs []error
	for _, result := range results {
		if result.id == "" && result.name == "" {
			continue
		}

		target := result.id
		if target == "" {
			target = result.name
		}

		if _, err := dockerOutput(context.Background(), "rm", "-f", target); err != nil {
			errs = append(errs, err)
		}
	}

	return errors.Join(errs...)
}

func printContainerFailures(results []containerResult) {
	for _, result := range results {
		if result.err == nil {
			continue
		}
		fmt.Fprintf(os.Stderr, "container %d (%s) failed: %v\n", result.index, result.name, result.err)
	}
}

func dockerOutput(ctx context.Context, args ...string) (string, error) {
	cmd := exec.CommandContext(ctx, "docker", args...)
	out, err := cmd.CombinedOutput()
	if err != nil {
		return "", fmt.Errorf("docker %s failed: %w\n%s", strings.Join(args, " "), err, strings.TrimSpace(string(out)))
	}
	return string(out), nil
}

func exitf(format string, args ...any) {
	fmt.Fprintf(os.Stderr, format+"\n", args...)
	os.Exit(1)
}
