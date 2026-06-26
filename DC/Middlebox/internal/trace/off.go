//go:build !trace

package trace

func Start(path string, bufferEvents int, dropOnFull bool) error { return nil }
func Mark(code uint32, id string, arg uint64)                    {}
func Stop()                                                      {}
func Enabled() bool                                              { return false }
