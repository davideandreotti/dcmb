package tracebind

import "testing"

func TestConnectionKeyStringsIsDirectionIndependent(t *testing.T) {
	forward := ConnectionKeyStrings("127.0.0.1:1234", "127.0.0.1:9443")
	reverse := ConnectionKeyStrings("127.0.0.1:9443", "127.0.0.1:1234")
	if forward == 0 || forward != reverse {
		t.Fatalf("connection key mismatch: forward=%d reverse=%d", forward, reverse)
	}
}
