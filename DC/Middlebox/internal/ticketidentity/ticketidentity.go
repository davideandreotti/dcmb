package ticketidentity

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/binary"
	"errors"
	"strings"
)

const (
	Version      byte = 0x01
	IdentitySize      = len(magic) + 1 + sha256.Size

	domain = "dcmb-ticket-identity-v1"
	magic  = "DCMB"
)

func Derive(key []byte, operatorID string, serviceID string) ([]byte, error) {
	operatorID = strings.TrimSpace(operatorID)
	serviceID = strings.ToLower(strings.TrimSpace(serviceID))
	if len(key) == 0 {
		return nil, errors.New("ticket identity key is empty")
	}
	if operatorID == "" {
		return nil, errors.New("operator id is empty")
	}
	if serviceID == "" {
		return nil, errors.New("service id is empty")
	}

	mac := hmac.New(sha256.New, key)
	mac.Write([]byte(domain))
	writeString(mac, operatorID)
	writeString(mac, serviceID)

	identity := make([]byte, 0, IdentitySize)
	identity = append(identity, magic...)
	identity = append(identity, Version)
	identity = mac.Sum(identity)
	return identity, nil
}

func IsIdentity(identity []byte) bool {
	return len(identity) == IdentitySize &&
		hmac.Equal(identity[:len(magic)], []byte(magic)) &&
		identity[len(magic)] == Version
}

func Equal(a []byte, b []byte) bool {
	return hmac.Equal(a, b)
}

type byteWriter interface {
	Write([]byte) (int, error)
}

func writeString(w byteWriter, s string) {
	var length [4]byte
	binary.BigEndian.PutUint32(length[:], uint32(len(s)))
	w.Write(length[:])
	w.Write([]byte(s))
}
