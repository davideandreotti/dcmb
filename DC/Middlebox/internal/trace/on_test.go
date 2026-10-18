//go:build trace

package trace

import (
	"encoding/binary"
	"os"
	"path/filepath"
	"sync"
	"testing"
)

func TestBufferOnlyTracing(t *testing.T) {
	for _, capacity := range []int{8, 0} {
		Stop()
		t.Cleanup(Stop)
		if err := Start("buffers", capacity, false); err != nil {
			t.Fatal(err)
		}
		expected := capacity
		if expected == 0 {
			expected = 100000
		}
		if Enabled() || writer == nil || writer.Size() != 1<<20 || cap(events) != expected || out != nil || done != nil {
			t.Fatal("buffer-only startup did not retain just the recording buffers")
		}
		originalWriter, originalEvents := writer, events
		if err := Start("buffers", capacity, false); err != nil {
			t.Fatal(err)
		}
		if writer != originalWriter || events != originalEvents {
			t.Fatal("repeated startup replaced the retained allocations")
		}
		var wg sync.WaitGroup
		for i := 0; i < 8; i++ {
			wg.Add(1)
			go func() {
				defer wg.Done()
				for j := 0; j < 100; j++ {
					Mark(123, "ignored", 42)
				}
			}()
		}
		wg.Wait()
		if len(events) != 0 || writer.Buffered() != 0 {
			t.Fatal("buffer-only mode recorded events")
		}
		if n := testing.AllocsPerRun(100, func() { Mark(123, "ignored", 42) }); n != 0 {
			t.Fatalf("disabled Mark allocated: %v", n)
		}
		if _, err := os.Stat("buffers"); !os.IsNotExist(err) {
			t.Fatalf("unexpected trace output at reserved path: %v", err)
		}
		Stop()
		Stop()
		if writer != nil || events != nil || done != nil || out != nil {
			t.Fatal("Stop did not release retained buffers")
		}
	}
}

func TestEmptyPathDoesNotRetainBuffers(t *testing.T) {
	Stop()
	t.Cleanup(Stop)
	originalWriter, originalEvents := writer, events
	if err := Start("", 8, true); err != nil {
		t.Fatal(err)
	}
	if Enabled() || writer != originalWriter || events != originalEvents {
		t.Fatal("empty trace path allocated buffers or enabled recording")
	}
}

func TestRecordingKeepsBinaryFormat(t *testing.T) {
	for _, name := range []string{"trace.bin", "buffers"} {
		t.Run(name, func(t *testing.T) {
			Stop()
			t.Cleanup(Stop)
			path := filepath.Join(t.TempDir(), name) // A real path named buffers is not reserved.
			if err := Start(path, 8, false); err != nil {
				t.Fatal(err)
			}
			if !Enabled() {
				t.Fatal("normal recording disabled")
			}
			Mark(123, "request", 42)
			Stop()
			data, err := os.ReadFile(path)
			if err != nil {
				t.Fatal(err)
			}
			if len(data) != 12+22+len("request") || string(data[:4]) != magic {
				t.Fatalf("unexpected trace format: %x", data)
			}
			if binary.LittleEndian.Uint16(data[12:14]) != uint16(len("request")) ||
				binary.LittleEndian.Uint64(data[14:22]) == 0 ||
				binary.LittleEndian.Uint32(data[22:26]) != 123 ||
				binary.LittleEndian.Uint64(data[26:34]) != 42 || string(data[34:]) != "request" {
				t.Fatal("trace event payload changed")
			}
		})
	}
}
