package main

import (
	"encoding/json"
	"errors"
	"net/http"
	"reflect"
	"testing"
)

type captureCreateTransport func(*http.Request) (*http.Response, error)

func (f captureCreateTransport) RoundTrip(r *http.Request) (*http.Response, error) {
	return f(r)
}

func TestWorkerTraceArguments(t *testing.T) {
	for _, mode := range []string{"false", "buffers", "true"} {
		t.Run(mode, func(t *testing.T) {
			var request dockerContainerCreateRequest
			captured := false
			stop := errors.New("stop after capturing create request")
			d := &dockerBackend{
				api: &dockerAPIClient{httpClient: &http.Client{Transport: captureCreateTransport(func(r *http.Request) (*http.Response, error) {
					if r.Method != http.MethodPost || r.URL.Path != "/containers/create" {
						t.Fatalf("unexpected Docker request: %s %s", r.Method, r.URL.Path)
					}
					if err := json.NewDecoder(r.Body).Decode(&request); err != nil {
						t.Fatal(err)
					}
					captured = true
					return nil, stop // No container or network connection is created.
				})}},
				namePrefix: "test", runID: "unit", operatorReuseDC: "true",
				workerTraceEnabled: mode == "true", workerTraceBuffers: mode == "buffers",
				workerTraceHostDir: "/host-traces", workerTraceContainerDir: "/trace",
				workerTraceBufferEvents: 100000, workerTraceDropOnFull: true,
			}
			if _, err := d.createAndWaitReady(); !errors.Is(err, stop) || !captured {
				t.Fatalf("create request was not captured: %v", err)
			}
			var actual []string
			for i, arg := range request.Cmd {
				if arg == "-trace" {
					actual = request.Cmd[i:]
				}
			}
			var expected, binds []string
			if mode != "false" {
				path := "buffers"
				if mode == "true" {
					path = "/trace/test-unit-1.bin"
					binds = []string{"/host-traces:/trace"}
				}
				expected = []string{"-trace", path, "-trace-buffer-events", "100000", "-trace-drop-on-full=true"}
			}
			if !reflect.DeepEqual(actual, expected) || !reflect.DeepEqual(request.HostConfig.Binds, binds) {
				t.Fatalf("mode=%s args=%v binds=%v; want args=%v binds=%v", mode, actual, request.HostConfig.Binds, expected, binds)
			}
		})
	}
}
