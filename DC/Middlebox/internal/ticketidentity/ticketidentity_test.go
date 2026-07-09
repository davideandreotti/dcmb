package ticketidentity

import (
	"bytes"
	"testing"
)

func TestDeriveStableAndBound(t *testing.T) {
	key := []byte("experiment-key")

	a, err := Derive(key, "worker-1", "server")
	if err != nil {
		t.Fatal(err)
	}
	b, err := Derive(key, "worker-1", "SERVER")
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(a, b) {
		t.Fatal("identity should normalize SNI case")
	}
	if !IsIdentity(a) {
		t.Fatal("derived identity was not recognized")
	}

	otherWorker, err := Derive(key, "worker-2", "server")
	if err != nil {
		t.Fatal(err)
	}
	if bytes.Equal(a, otherWorker) {
		t.Fatal("different workers must not share an identity")
	}

	otherService, err := Derive(key, "worker-1", "other")
	if err != nil {
		t.Fatal(err)
	}
	if bytes.Equal(a, otherService) {
		t.Fatal("different services must not share an identity")
	}
}

func TestDeriveRequiresInputs(t *testing.T) {
	if _, err := Derive(nil, "worker", "server"); err == nil {
		t.Fatal("expected empty key error")
	}
	if _, err := Derive([]byte("key"), "", "server"); err == nil {
		t.Fatal("expected empty operator error")
	}
	if _, err := Derive([]byte("key"), "worker", ""); err == nil {
		t.Fatal("expected empty service error")
	}
}
