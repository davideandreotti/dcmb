package tlshello

import (
	"encoding/binary"
	"errors"
	"testing"
)

func TestParseClientHelloSNIAndPSK(t *testing.T) {
	identity := []byte("DCMB-test-ticket")
	records := buildClientHelloRecord("server", identity)

	info, err := Parse(records)
	if err != nil {
		t.Fatal(err)
	}
	if info.ServerName != "server" {
		t.Fatalf("ServerName = %q, want server", info.ServerName)
	}
	if len(info.PSKIdentities) != 1 {
		t.Fatalf("PSKIdentities len = %d, want 1", len(info.PSKIdentities))
	}
	if string(info.PSKIdentities[0]) != string(identity) {
		t.Fatalf("PSK identity = %q, want %q", info.PSKIdentities[0], identity)
	}
}

func TestParseIncomplete(t *testing.T) {
	_, err := Parse([]byte{22, 3})
	if !errors.Is(err, ErrIncomplete) {
		t.Fatalf("err = %v, want ErrIncomplete", err)
	}
}

func buildClientHelloRecord(serverName string, identity []byte) []byte {
	var body []byte
	body = append(body, 0x03, 0x03)
	body = append(body, make([]byte, 32)...)
	body = append(body, 0)
	body = appendUint16(body, 2)
	body = append(body, 0x13, 0x01)
	body = append(body, 1, 0)

	var extensions []byte
	extensions = appendExtension(extensions, extensionServerName, buildSNIExtension(serverName))
	extensions = appendExtension(extensions, extensionPreSharedKey, buildPSKExtension(identity))
	body = appendUint16(body, len(extensions))
	body = append(body, extensions...)

	var handshake []byte
	handshake = append(handshake, handshakeClientHello)
	handshake = appendUint24(handshake, len(body))
	handshake = append(handshake, body...)

	var record []byte
	record = append(record, recordTypeHandshake, 0x03, 0x01)
	record = appendUint16(record, len(handshake))
	record = append(record, handshake...)
	return record
}

func buildSNIExtension(serverName string) []byte {
	var name []byte
	name = append(name, 0)
	name = appendUint16(name, len(serverName))
	name = append(name, []byte(serverName)...)

	var data []byte
	data = appendUint16(data, len(name))
	data = append(data, name...)
	return data
}

func buildPSKExtension(identity []byte) []byte {
	var identities []byte
	identities = appendUint16(identities, len(identity))
	identities = append(identities, identity...)
	identities = append(identities, 0, 0, 0, 0)

	var data []byte
	data = appendUint16(data, len(identities))
	data = append(data, identities...)
	data = appendUint16(data, 33)
	data = append(data, 32)
	data = append(data, make([]byte, 32)...)
	return data
}

func appendExtension(dst []byte, typ uint16, data []byte) []byte {
	dst = appendUint16(dst, int(typ))
	dst = appendUint16(dst, len(data))
	return append(dst, data...)
}

func appendUint16(dst []byte, value int) []byte {
	var buf [2]byte
	binary.BigEndian.PutUint16(buf[:], uint16(value))
	return append(dst, buf[:]...)
}

func appendUint24(dst []byte, value int) []byte {
	return append(dst, byte(value>>16), byte(value>>8), byte(value))
}
