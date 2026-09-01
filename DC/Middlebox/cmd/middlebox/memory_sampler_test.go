package main

import (
	"os"
	"path/filepath"
	"testing"
)

func TestDeriveMemoryLogPathsFromExperimentTrace(t *testing.T) {
	series, summary, err := deriveMemoryLogPaths(
		filepath.FromSlash("/trace/campaign/run/traces/middlebox.bin"),
	)
	if err != nil {
		t.Fatal(err)
	}

	wantSeries := filepath.FromSlash("/trace/campaign/run/cpu/middlebox_memory.csv")
	wantSummary := filepath.FromSlash("/trace/campaign/run/cpu/middlebox_memory_summary.csv")
	if series != wantSeries {
		t.Fatalf("series path = %q, want %q", series, wantSeries)
	}
	if summary != wantSummary {
		t.Fatalf("summary path = %q, want %q", summary, wantSummary)
	}
}

func TestDeriveMemoryLogPathsBesideStandaloneTrace(t *testing.T) {
	series, summary, err := deriveMemoryLogPaths(filepath.FromSlash("/tmp/manual.bin"))
	if err != nil {
		t.Fatal(err)
	}

	if want := filepath.FromSlash("/tmp/manual_memory.csv"); series != want {
		t.Fatalf("series path = %q, want %q", series, want)
	}
	if want := filepath.FromSlash("/tmp/manual_memory_summary.csv"); summary != want {
		t.Fatalf("summary path = %q, want %q", summary, want)
	}
}

func TestDeriveMemoryLogPathsRequiresTracePath(t *testing.T) {
	if _, _, err := deriveMemoryLogPaths(""); err == nil {
		t.Fatal("expected an error for an empty trace path")
	}
}

func TestParseVMPeak(t *testing.T) {
	status := []byte("Name:\tmiddlebox\nVmPeak:\t  123456 kB\nThreads:\t4\n")
	got, err := parseVMPeak(status)
	if err != nil {
		t.Fatal(err)
	}
	if want := uint64(123456 * 1024); got != want {
		t.Fatalf("VmPeak = %d bytes, want %d", got, want)
	}
}

func TestParseVMPeakRejectsMissingValue(t *testing.T) {
	if _, err := parseVMPeak([]byte("Name:\tmiddlebox\n")); err == nil {
		t.Fatal("expected an error when VmPeak is absent")
	}
}

func TestReadGoMemoryUsage(t *testing.T) {
	retained, _, err := readGoMemoryUsage()
	if err != nil {
		t.Fatal(err)
	}
	if retained == 0 {
		t.Fatal("Go retained memory is zero")
	}
}

func TestGramineMemorySamplerWritesDerivedFiles(t *testing.T) {
	t.Setenv(sharedGramineEnv, "1")
	tracePath := filepath.Join(t.TempDir(), "run", "traces", "middlebox.bin")
	if err := os.MkdirAll(filepath.Dir(tracePath), 0o755); err != nil {
		t.Fatal(err)
	}

	sampler, err := startGramineMemorySampler(tracePath)
	if err != nil {
		t.Fatal(err)
	}
	if sampler == nil {
		t.Fatal("sampler was not enabled")
	}
	if err := sampler.Stop(); err != nil {
		t.Fatal(err)
	}

	seriesPath, summaryPath, err := deriveMemoryLogPaths(tracePath)
	if err != nil {
		t.Fatal(err)
	}
	for _, path := range []string{seriesPath, summaryPath} {
		info, err := os.Stat(path)
		if err != nil {
			t.Fatalf("stat %s: %v", path, err)
		}
		if info.Size() == 0 {
			t.Fatalf("memory output %s is empty", path)
		}
	}
}
