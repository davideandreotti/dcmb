package main

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestInit(t *testing.T) {
	req := httptest.NewRequest(http.MethodPost, "/function/init", strings.NewReader(`{"operation":"init"}`))
	req.Header.Set("Authorization", "Bearer token")
	req.Header.Set("X-Trace-ID", "test-request")
	recorder := httptest.NewRecorder()

	appHandler{}.ServeHTTP(recorder, req)

	if recorder.Code != http.StatusOK {
		t.Fatalf("status = %d, want %d", recorder.Code, http.StatusOK)
	}
	if contentType := recorder.Header().Get("Content-Type"); contentType != "application/json" {
		t.Fatalf("Content-Type = %q", contentType)
	}
}

func TestInitRequiresBearerToken(t *testing.T) {
	req := httptest.NewRequest(http.MethodPost, "/function/init", nil)
	recorder := httptest.NewRecorder()

	appHandler{}.ServeHTTP(recorder, req)

	if recorder.Code != http.StatusUnauthorized {
		t.Fatalf("status = %d, want %d", recorder.Code, http.StatusUnauthorized)
	}
}
