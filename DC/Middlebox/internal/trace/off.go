//go:build !trace

package trace

import "errors"

func Start(path string, bufferEvents int, dropOnFull bool) error {
	if path == "buffers" {
		return errors.New("-trace buffers requires build tag trace")
	}
	return nil
}
func Mark(code uint32, id string, arg uint64) {}
func Stop()                                   {}
func Enabled() bool                           { return false }
