package main

import (
	"bufio"
	"encoding/csv"
	"errors"
	"fmt"
	"math"
	"os"
	"path/filepath"
	"runtime/metrics"
	"strconv"
	"strings"
	"sync"
	"time"
)

const (
	sharedGramineEnv     = "DCMB_GRAMINE_SHARED"
	memorySampleInterval = 200 * time.Millisecond
	memorySampleCapacity = 4096

	goTotalMemoryMetric    = "/memory/classes/total:bytes"
	goReleasedHeapMetric   = "/memory/classes/heap/released:bytes"
	goLiveHeapMemoryMetric = "/gc/heap/live:bytes"
)

type memoryUsageSample struct {
	timestampNS     int64
	goRetainedBytes uint64
	goLiveHeapBytes uint64
}

type gramineMemorySampler struct {
	seriesPath  string
	summaryPath string
	interval    time.Duration

	samples            []memoryUsageSample
	goRetainedPeak     uint64
	goLiveHeapPeak     uint64
	gramineVMPeakBytes uint64
	haveGramineVMPeak  bool
	sampleErr          error
	vmPeakErr          error

	stop     chan struct{}
	done     chan struct{}
	stopOnce sync.Once
}

func startGramineMemorySampler(tracePath string) (*gramineMemorySampler, error) {
	if os.Getenv(sharedGramineEnv) != "1" {
		return nil, nil
	}

	seriesPath, summaryPath, err := deriveMemoryLogPaths(tracePath)
	if err != nil {
		return nil, err
	}

	sampler := &gramineMemorySampler{
		seriesPath:  seriesPath,
		summaryPath: summaryPath,
		interval:    memorySampleInterval,
		samples:     make([]memoryUsageSample, 0, memorySampleCapacity),
		stop:        make(chan struct{}),
		done:        make(chan struct{}),
	}
	go sampler.run()
	return sampler, nil
}

func deriveMemoryLogPaths(tracePath string) (string, string, error) {
	if strings.TrimSpace(tracePath) == "" {
		return "", "", fmt.Errorf("shared Gramine memory sampling requires the existing -trace path")
	}

	cleanPath := filepath.Clean(tracePath)
	traceDir := filepath.Dir(cleanPath)
	outputDir := traceDir
	if filepath.Base(traceDir) == "traces" {
		outputDir = filepath.Join(filepath.Dir(traceDir), "cpu")
	}

	name := strings.TrimSuffix(filepath.Base(cleanPath), filepath.Ext(cleanPath))
	if name == "" || name == "." {
		name = "middlebox"
	}
	return filepath.Join(outputDir, name+"_memory.csv"),
		filepath.Join(outputDir, name+"_memory_summary.csv"), nil
}

func (s *gramineMemorySampler) run() {
	defer close(s.done)

	s.recordGoMemory()
	ticker := time.NewTicker(s.interval)
	defer ticker.Stop()

	for {
		select {
		case <-ticker.C:
			s.recordGoMemory()
		case <-s.stop:
			s.recordGoMemory()
			s.gramineVMPeakBytes, s.vmPeakErr = readGramineVMPeak()
			s.haveGramineVMPeak = s.vmPeakErr == nil
			return
		}
	}
}

func (s *gramineMemorySampler) recordGoMemory() {
	retained, live, err := readGoMemoryUsage()
	if err != nil {
		if s.sampleErr == nil {
			s.sampleErr = err
		}
		return
	}

	s.samples = append(s.samples, memoryUsageSample{
		timestampNS:     time.Now().UnixNano(),
		goRetainedBytes: retained,
		goLiveHeapBytes: live,
	})
	if retained > s.goRetainedPeak {
		s.goRetainedPeak = retained
	}
	if live > s.goLiveHeapPeak {
		s.goLiveHeapPeak = live
	}
}

func readGoMemoryUsage() (uint64, uint64, error) {
	samples := []metrics.Sample{
		{Name: goTotalMemoryMetric},
		{Name: goReleasedHeapMetric},
		{Name: goLiveHeapMemoryMetric},
	}
	metrics.Read(samples)

	total, err := uint64Metric(samples[0])
	if err != nil {
		return 0, 0, err
	}
	released, err := uint64Metric(samples[1])
	if err != nil {
		return 0, 0, err
	}
	live, err := uint64Metric(samples[2])
	if err != nil {
		return 0, 0, err
	}
	if released > total {
		return 0, 0, fmt.Errorf("Go released heap (%d) exceeds total runtime memory (%d)", released, total)
	}
	return total - released, live, nil
}

func uint64Metric(sample metrics.Sample) (uint64, error) {
	if sample.Value.Kind() != metrics.KindUint64 {
		return 0, fmt.Errorf("Go runtime metric %q is unavailable or is not uint64", sample.Name)
	}
	return sample.Value.Uint64(), nil
}

func readGramineVMPeak() (uint64, error) {
	status, err := os.ReadFile("/proc/self/status")
	if err != nil {
		return 0, fmt.Errorf("read Gramine /proc/self/status: %w", err)
	}
	return parseVMPeak(status)
}

func parseVMPeak(status []byte) (uint64, error) {
	scanner := bufio.NewScanner(strings.NewReader(string(status)))
	for scanner.Scan() {
		fields := strings.Fields(scanner.Text())
		if len(fields) == 0 || fields[0] != "VmPeak:" {
			continue
		}
		if len(fields) != 3 || fields[2] != "kB" {
			return 0, fmt.Errorf("unexpected VmPeak line %q", scanner.Text())
		}
		kilobytes, err := strconv.ParseUint(fields[1], 10, 64)
		if err != nil {
			return 0, fmt.Errorf("parse VmPeak value %q: %w", fields[1], err)
		}
		if kilobytes > math.MaxUint64/1024 {
			return 0, fmt.Errorf("VmPeak value overflows bytes: %d kB", kilobytes)
		}
		return kilobytes * 1024, nil
	}
	if err := scanner.Err(); err != nil {
		return 0, fmt.Errorf("scan VmPeak: %w", err)
	}
	return 0, fmt.Errorf("VmPeak is absent from /proc/self/status")
}

func (s *gramineMemorySampler) Stop() error {
	if s == nil {
		return nil
	}
	s.stopOnce.Do(func() { close(s.stop) })
	<-s.done

	writeErr := s.writeFiles()
	vmPeakText := "unavailable"
	if s.haveGramineVMPeak {
		vmPeakText = strconv.FormatUint(s.gramineVMPeakBytes, 10)
	}
	fmt.Fprintf(os.Stderr,
		"[OPERATOR_MEMORY] go_retained_peak_bytes=%d go_live_heap_peak_bytes=%d gramine_vm_peak_bytes=%s\n",
		s.goRetainedPeak, s.goLiveHeapPeak, vmPeakText)

	return errors.Join(s.sampleErr, s.vmPeakErr, writeErr)
}

func (s *gramineMemorySampler) writeFiles() error {
	if err := os.MkdirAll(filepath.Dir(s.seriesPath), 0o755); err != nil {
		return fmt.Errorf("create memory log directory: %w", err)
	}
	if err := writeMemorySeries(s.seriesPath, s.samples); err != nil {
		return err
	}
	if err := writeMemorySummary(s.summaryPath, s); err != nil {
		return err
	}
	return nil
}

func writeMemorySeries(path string, samples []memoryUsageSample) error {
	handle, err := os.Create(path)
	if err != nil {
		return fmt.Errorf("create memory series %s: %w", path, err)
	}
	writer := csv.NewWriter(handle)
	writeErr := writer.Write([]string{"ts_ns", "go_retained_bytes", "go_live_heap_bytes"})
	for _, sample := range samples {
		if writeErr != nil {
			break
		}
		writeErr = writer.Write([]string{
			strconv.FormatInt(sample.timestampNS, 10),
			strconv.FormatUint(sample.goRetainedBytes, 10),
			strconv.FormatUint(sample.goLiveHeapBytes, 10),
		})
	}
	writer.Flush()
	if writeErr == nil {
		writeErr = writer.Error()
	}
	if closeErr := handle.Close(); writeErr == nil {
		writeErr = closeErr
	}
	if writeErr != nil {
		return fmt.Errorf("write memory series %s: %w", path, writeErr)
	}
	return nil
}

func writeMemorySummary(path string, sampler *gramineMemorySampler) error {
	handle, err := os.Create(path)
	if err != nil {
		return fmt.Errorf("create memory summary %s: %w", path, err)
	}
	writer := csv.NewWriter(handle)
	writeErr := writer.Write([]string{
		"samples",
		"go_retained_peak_bytes",
		"go_live_heap_peak_bytes",
		"gramine_vm_peak_bytes",
	})
	vmPeak := ""
	if sampler.haveGramineVMPeak {
		vmPeak = strconv.FormatUint(sampler.gramineVMPeakBytes, 10)
	}
	if writeErr == nil {
		writeErr = writer.Write([]string{
			strconv.Itoa(len(sampler.samples)),
			strconv.FormatUint(sampler.goRetainedPeak, 10),
			strconv.FormatUint(sampler.goLiveHeapPeak, 10),
			vmPeak,
		})
	}
	writer.Flush()
	if writeErr == nil {
		writeErr = writer.Error()
	}
	if closeErr := handle.Close(); writeErr == nil {
		writeErr = closeErr
	}
	if writeErr != nil {
		return fmt.Errorf("write memory summary %s: %w", path, writeErr)
	}
	return nil
}
