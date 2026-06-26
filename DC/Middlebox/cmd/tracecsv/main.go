package main

import (
	"bufio"
	"encoding/binary"
	"flag"
	"fmt"
	"io"
	"log"
	"os"

	"dc/middlebox/internal/trace"
)

func main() {
	inPath := flag.String("in", "", "input trace binary file")
	outPath := flag.String("out", "", "output CSV file; stdout if empty")
	flag.Parse()

	if *inPath == "" {
		log.Fatal("-in is required")
	}

	in, err := os.Open(*inPath)
	if err != nil {
		log.Fatal(err)
	}
	defer in.Close()

	var out io.Writer = os.Stdout
	var outFile *os.File
	if *outPath != "" {
		outFile, err = os.Create(*outPath)
		if err != nil {
			log.Fatal(err)
		}
		defer outFile.Close()
		out = outFile
	}

	reader := bufio.NewReaderSize(in, 1<<20)
	writer := bufio.NewWriterSize(out, 1<<20)
	defer writer.Flush()

	header := make([]byte, 12)
	if _, err := io.ReadFull(reader, header); err != nil {
		log.Fatalf("read header: %v", err)
	}
	if string(header[:4]) != "DCTR" {
		log.Fatalf("invalid trace magic %q", string(header[:4]))
	}

	fmt.Fprintln(writer, "timestamp_ns,event_code,event_name,id,arg")
	for {
		var fixed [22]byte
		if _, err := io.ReadFull(reader, fixed[:]); err != nil {
			if err == io.EOF || err == io.ErrUnexpectedEOF {
				break
			}
			log.Fatalf("read record: %v", err)
		}

		idLen := int(binary.LittleEndian.Uint16(fixed[0:2]))
		ts := int64(binary.LittleEndian.Uint64(fixed[2:10]))
		code := binary.LittleEndian.Uint32(fixed[10:14])
		arg := binary.LittleEndian.Uint64(fixed[14:22])

		id := make([]byte, idLen)
		if idLen > 0 {
			if _, err := io.ReadFull(reader, id); err != nil {
				log.Fatalf("read id: %v", err)
			}
		}

		fmt.Fprintf(writer, "%d,%d,%s,%q,%d\n", ts, code, trace.EventName(code), string(id), arg)
	}
}
