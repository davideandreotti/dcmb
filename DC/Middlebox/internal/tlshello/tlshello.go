package tlshello

import (
	"encoding/binary"
	"errors"
)

var ErrIncomplete = errors.New("incomplete TLS ClientHello")

type Info struct {
	ServerName    string
	PSKIdentities [][]byte
}

const (
	recordTypeHandshake  = 22
	handshakeClientHello = 1

	extensionServerName   uint16 = 0
	extensionPreSharedKey uint16 = 41
)

func Parse(records []byte) (*Info, error) {
	var handshake []byte
	for offset := 0; offset < len(records); {
		if len(records)-offset < 5 {
			return nil, ErrIncomplete
		}
		if records[offset] != recordTypeHandshake {
			return nil, errors.New("first TLS record is not a handshake record")
		}
		recordLen := int(binary.BigEndian.Uint16(records[offset+3 : offset+5]))
		offset += 5
		if len(records)-offset < recordLen {
			return nil, ErrIncomplete
		}
		handshake = append(handshake, records[offset:offset+recordLen]...)
		offset += recordLen

		if len(handshake) < 4 {
			continue
		}
		if handshake[0] != handshakeClientHello {
			return nil, errors.New("first handshake message is not ClientHello")
		}
		helloLen := int(handshake[1])<<16 | int(handshake[2])<<8 | int(handshake[3])
		if len(handshake) < 4+helloLen {
			continue
		}
		return parseClientHelloBody(handshake[4 : 4+helloLen])
	}
	return nil, ErrIncomplete
}

func parseClientHelloBody(body []byte) (*Info, error) {
	info := &Info{}
	offset := 0

	if !skip(body, &offset, 2+32) {
		return nil, ErrIncomplete
	}
	if !skipUint8Vector(body, &offset) {
		return nil, ErrIncomplete
	}
	if !skipUint16Vector(body, &offset) {
		return nil, ErrIncomplete
	}
	if !skipUint8Vector(body, &offset) {
		return nil, ErrIncomplete
	}
	if offset == len(body) {
		return info, nil
	}
	if len(body)-offset < 2 {
		return nil, ErrIncomplete
	}

	extensionsLen := int(binary.BigEndian.Uint16(body[offset : offset+2]))
	offset += 2
	if len(body)-offset < extensionsLen {
		return nil, ErrIncomplete
	}
	extensions := body[offset : offset+extensionsLen]

	for len(extensions) > 0 {
		if len(extensions) < 4 {
			return nil, ErrIncomplete
		}
		extType := binary.BigEndian.Uint16(extensions[:2])
		extLen := int(binary.BigEndian.Uint16(extensions[2:4]))
		extensions = extensions[4:]
		if len(extensions) < extLen {
			return nil, ErrIncomplete
		}
		extData := extensions[:extLen]
		extensions = extensions[extLen:]

		switch extType {
		case extensionServerName:
			if serverName := parseServerName(extData); serverName != "" {
				info.ServerName = serverName
			}
		case extensionPreSharedKey:
			info.PSKIdentities = parsePSKIdentities(extData)
		}
	}

	return info, nil
}

func parseServerName(data []byte) string {
	if len(data) < 2 {
		return ""
	}
	listLen := int(binary.BigEndian.Uint16(data[:2]))
	data = data[2:]
	if len(data) < listLen {
		return ""
	}
	data = data[:listLen]
	for len(data) > 0 {
		if len(data) < 3 {
			return ""
		}
		nameType := data[0]
		nameLen := int(binary.BigEndian.Uint16(data[1:3]))
		data = data[3:]
		if len(data) < nameLen {
			return ""
		}
		if nameType == 0 && nameLen > 0 {
			return string(data[:nameLen])
		}
		data = data[nameLen:]
	}
	return ""
}

func parsePSKIdentities(data []byte) [][]byte {
	if len(data) < 2 {
		return nil
	}
	identitiesLen := int(binary.BigEndian.Uint16(data[:2]))
	data = data[2:]
	if len(data) < identitiesLen {
		return nil
	}
	identities := data[:identitiesLen]
	result := make([][]byte, 0)
	for len(identities) > 0 {
		if len(identities) < 2 {
			return nil
		}
		identityLen := int(binary.BigEndian.Uint16(identities[:2]))
		identities = identities[2:]
		if len(identities) < identityLen+4 {
			return nil
		}
		identity := make([]byte, identityLen)
		copy(identity, identities[:identityLen])
		result = append(result, identity)
		identities = identities[identityLen+4:]
	}
	return result
}

func skip(data []byte, offset *int, n int) bool {
	if n < 0 || len(data)-*offset < n {
		return false
	}
	*offset += n
	return true
}

func skipUint8Vector(data []byte, offset *int) bool {
	if len(data)-*offset < 1 {
		return false
	}
	n := int(data[*offset])
	*offset += 1
	return skip(data, offset, n)
}

func skipUint16Vector(data []byte, offset *int) bool {
	if len(data)-*offset < 2 {
		return false
	}
	n := int(binary.BigEndian.Uint16(data[*offset : *offset+2]))
	*offset += 2
	return skip(data, offset, n)
}
