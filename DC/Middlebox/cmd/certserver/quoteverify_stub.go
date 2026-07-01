//go:build !dcapverify

package main

import "fmt"

func verifyQuote(_ []byte, _ []byte) (quoteVerificationInfo, error) {
	return quoteVerificationInfo{}, fmt.Errorf("DCAP quote verification not built in; rebuild certserver with -tags dcapverify")
}
