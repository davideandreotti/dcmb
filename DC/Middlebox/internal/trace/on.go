//go:build trace

package trace

import (
	"bufio"
	"encoding/binary"
	"io"
	"os"
	"sync"
	"sync/atomic"
	"time"
)

const magic = "DCTR"

type event struct {
	ts   int64
	code uint32
	id   string
	arg  uint64
}

var (
	mu      sync.Mutex
	out     *os.File
	writer  *bufio.Writer
	events  chan event
	done    chan struct{}
	enabled atomic.Bool
	drop    bool
)

func Start(path string, bufferEvents int, dropOnFull bool) error {
	if path == "" {
		return nil
	}
	if bufferEvents <= 0 {
		bufferEvents = 100000
	}

	mu.Lock()
	defer mu.Unlock()
	if writer != nil {
		return nil
	}

	var sink io.Writer = io.Discard
	if path != "buffers" {
		f, err := os.Create(path)
		if err != nil {
			return err
		}
		out = f
		sink = f
	}

	writer = bufio.NewWriterSize(sink, 1<<20)
	events = make(chan event, bufferEvents)
	if path == "buffers" {
		// Retain the recording allocations, but leave Mark disabled and do not
		// open a file or start a writer. No completion channel is needed.
		done = nil
		return nil
	}
	done = make(chan struct{})
	drop = dropOnFull

	header := make([]byte, 12)
	copy(header[:4], []byte(magic))
	binary.LittleEndian.PutUint64(header[4:12], uint64(time.Now().UnixNano()))
	if _, err := writer.Write(header); err != nil {
		_ = out.Close()
		out = nil
		writer = nil
		events = nil
		done = nil
		return err
	}

	enabled.Store(true)
	go runWriter()
	return nil
}

func Mark(code uint32, id string, arg uint64) {
	if !enabled.Load() {
		return
	}
	defer func() {
		_ = recover()
	}()

	e := event{ts: time.Now().UnixNano(), code: code, id: id, arg: arg}
	if drop {
		select {
		case events <- e:
		default:
		}
		return
	}
	events <- e
}

func Stop() {
	if !enabled.Load() {
		mu.Lock()
		if writer != nil && out == nil { // Buffer-only startup has no writer to wait for.
			writer, events, done = nil, nil, nil
		}
		mu.Unlock()
		return
	}

	mu.Lock()
	if !enabled.Load() {
		mu.Unlock()
		return
	}
	enabled.Store(false)
	close(events)
	chDone := done
	mu.Unlock()

	<-chDone

	mu.Lock()
	if writer != nil {
		_ = writer.Flush()
	}
	if out != nil {
		_ = out.Close()
	}
	writer = nil
	out = nil
	mu.Unlock()
}

func Enabled() bool {
	return enabled.Load()
}

func runWriter() {
	defer close(done)
	var fixed [22]byte
	for e := range events {
		idBytes := []byte(e.id)
		if len(idBytes) > 65535 {
			idBytes = idBytes[:65535]
		}

		binary.LittleEndian.PutUint16(fixed[0:2], uint16(len(idBytes)))
		binary.LittleEndian.PutUint64(fixed[2:10], uint64(e.ts))
		binary.LittleEndian.PutUint32(fixed[10:14], e.code)
		binary.LittleEndian.PutUint64(fixed[14:22], e.arg)

		_, _ = writer.Write(fixed[:])
		if len(idBytes) > 0 {
			_, _ = writer.Write(idBytes)
		}
	}
	_ = writer.Flush()
}
