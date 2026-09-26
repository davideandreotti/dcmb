//go:build dcapverify

package main

/*
#cgo CXXFLAGS: -std=c++17 -I${SRCDIR}/../../build/quoteverify/include
#cgo LDFLAGS: -L${SRCDIR}/../../build/quoteverify -Wl,-rpath,${SRCDIR}/../../build/quoteverify -ldcmb_qvl -lsgx_dcap_quoteverify -lcrypto -lstdc++
#include "quoteverify_dcap.h"
*/
import "C"

import (
	"fmt"
	"unsafe"
)

func verifyQuote(quote, expectedReportData []byte) (quoteVerificationInfo, error) {
	if len(quote) == 0 || len(quote) > 65536 {
		return quoteVerificationInfo{}, fmt.Errorf("invalid attestation quote size")
	}
	if len(expectedReportData) != 0 && len(expectedReportData) != 64 {
		return quoteVerificationInfo{}, fmt.Errorf("invalid expected REPORTDATA size")
	}
	var expected *C.uint8_t
	if len(expectedReportData) != 0 {
		expected = (*C.uint8_t)(unsafe.Pointer(&expectedReportData[0]))
	}
	var result C.dcmb_quote_result
	status := C.dcmb_verify_quote((*C.uint8_t)(unsafe.Pointer(&quote[0])), C.uint32_t(len(quote)),
		expected, C.uint32_t(len(expectedReportData)), &result)
	info := quoteVerificationInfo{
		DCAPReturn:              uint32(result.dcap_return),
		CollateralExpiration:    uint32(result.collateral_expiration),
		QuoteVerificationResult: uint32(result.qv_result),
		AcceptedNonTerminal:     status == 0 && result.qv_result != 0,
	}
	if status != 0 {
		return info, fmt.Errorf("%s (dcap=0x%x, qvl=%d, result=0x%x)",
			C.GoString(&result.error[0]), info.DCAPReturn, uint32(result.qvl_status), info.QuoteVerificationResult)
	}
	return info, nil
}
