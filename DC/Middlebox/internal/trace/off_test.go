//go:build !trace

package trace

import (
	"strings"
	"testing"
)

func TestNoTraceBuildRejectsBuffers(t *testing.T) {
	if err := Start("buffers", 8, true); err == nil || !strings.Contains(err.Error(), "requires build tag trace") {
		t.Fatalf("expected missing-build-tag error, got %v", err)
	}
	for _, path := range []string{"", "trace.bin", "./buffers"} {
		if err := Start(path, 8, true); err != nil {
			t.Fatalf("legacy no-op changed for %q: %v", path, err)
		}
		Mark(123, "ignored", 42)
		Stop()
		if Enabled() {
			t.Fatal("no-trace build enabled recording")
		}
	}
}
