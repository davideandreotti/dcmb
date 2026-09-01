package tracebind

import (
	"hash/fnv"
	"net"
)

// ConnectionKey returns the same key from either end of a TCP connection.
func ConnectionKey(local, remote net.Addr) uint64 {
	if local == nil || remote == nil {
		return 0
	}
	return ConnectionKeyStrings(local.String(), remote.String())
}

func ConnectionKeyStrings(first, second string) uint64 {
	if second < first {
		first, second = second, first
	}
	h := fnv.New64a()
	_, _ = h.Write([]byte(first))
	_, _ = h.Write([]byte{0})
	_, _ = h.Write([]byte(second))
	return h.Sum64()
}
