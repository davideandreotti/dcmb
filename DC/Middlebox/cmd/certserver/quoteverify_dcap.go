//go:build dcapverify

package main

/*
#cgo LDFLAGS: -lsgx_dcap_quoteverify

#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <stddef.h>

#include <sgx_dcap_quoteverify.h>
#include <sgx_quote_3.h>
#include <sgx_report.h>

static int dcmb_verify_quote(
	const uint8_t *quote,
	uint32_t quote_len,
	const uint8_t *expected_report_data,
	uint32_t expected_report_data_len,
	uint32_t *dcap_ret_out,
	uint32_t *collateral_expiration_out,
	uint32_t *qv_result_out
) {
	if (dcap_ret_out == NULL || collateral_expiration_out == NULL || qv_result_out == NULL) {
		return 100;
	}

	*dcap_ret_out = 0;
	*collateral_expiration_out = 0;
	*qv_result_out = TEE_QV_RESULT_UNSPECIFIED;

	if (quote == NULL || quote_len == 0) {
		return 101;
	}

	if (expected_report_data_len > 0) {
		if (expected_report_data == NULL || expected_report_data_len != SGX_REPORT_DATA_SIZE) {
			return 102;
		}
		if (quote_len < sizeof(sgx_quote_header_t)) {
			return 103;
		}
		sgx_quote_header_t header;
		memset(&header, 0, sizeof(header));
		memcpy(&header, quote, sizeof(header));
		if (header.version != 3) {
			return 104;
		}

		const size_t report_data_offset =
			offsetof(sgx_quote3_t, report_body) + offsetof(sgx_report_body_t, report_data);
		if (quote_len < report_data_offset + SGX_REPORT_DATA_SIZE) {
			return 105;
		}
		if (memcmp(quote + report_data_offset, expected_report_data, SGX_REPORT_DATA_SIZE) != 0) {
			return 106;
		}
	}

	tee_supp_data_descriptor_t supp_data;
	memset(&supp_data, 0, sizeof(supp_data));

	uint32_t latest_version = 0;
	quote3_error_t dcap_ret = tee_get_supplemental_data_version_and_size(
		quote,
		quote_len,
		&latest_version,
		&supp_data.data_size
	);
	if (dcap_ret == TEE_SUCCESS && supp_data.data_size > 0) {
		supp_data.p_data = (uint8_t *)calloc(1, supp_data.data_size);
		if (supp_data.p_data == NULL) {
			supp_data.data_size = 0;
		}
	} else {
		supp_data.data_size = 0;
		supp_data.p_data = NULL;
	}

	uint32_t collateral_expiration_status = 1;
	sgx_ql_qv_result_t quote_verification_result = TEE_QV_RESULT_UNSPECIFIED;
	dcap_ret = tee_verify_quote(
		quote,
		quote_len,
		NULL,
		time(NULL),
		&collateral_expiration_status,
		&quote_verification_result,
		NULL,
		&supp_data
	);

	if (supp_data.p_data != NULL) {
		free(supp_data.p_data);
	}

	*dcap_ret_out = (uint32_t)dcap_ret;
	*collateral_expiration_out = collateral_expiration_status;
	*qv_result_out = (uint32_t)quote_verification_result;

	if (dcap_ret != TEE_SUCCESS) {
		return 200;
	}

	switch (quote_verification_result) {
	case TEE_QV_RESULT_OK:
		if (collateral_expiration_status == 0) {
			return 0;
		}
		return 201;
	case TEE_QV_RESULT_CONFIG_NEEDED:
	case TEE_QV_RESULT_OUT_OF_DATE:
	case TEE_QV_RESULT_OUT_OF_DATE_CONFIG_NEEDED:
		return 1;
	default:
		return 202;
	}
}
*/
import "C"

import (
	"fmt"
	"unsafe"
)

func verifyQuote(quote []byte, expectedReportData []byte) (quoteVerificationInfo, error) {
	if len(quote) == 0 {
		return quoteVerificationInfo{}, fmt.Errorf("empty attestation quote")
	}

	var quotePtr *C.uint8_t
	quotePtr = (*C.uint8_t)(unsafe.Pointer(&quote[0]))

	var expectedPtr *C.uint8_t
	if len(expectedReportData) > 0 {
		expectedPtr = (*C.uint8_t)(unsafe.Pointer(&expectedReportData[0]))
	}

	var dcapRet C.uint32_t
	var collateralExpiration C.uint32_t
	var qvResult C.uint32_t

	status := C.dcmb_verify_quote(
		quotePtr,
		C.uint32_t(len(quote)),
		expectedPtr,
		C.uint32_t(len(expectedReportData)),
		&dcapRet,
		&collateralExpiration,
		&qvResult,
	)

	info := quoteVerificationInfo{
		DCAPReturn:              uint32(dcapRet),
		CollateralExpiration:    uint32(collateralExpiration),
		QuoteVerificationResult: uint32(qvResult),
		AcceptedNonTerminal:     status == 1,
	}

	switch status {
	case 0, 1:
		return info, nil
	case 100:
		return info, fmt.Errorf("internal verifier parameter error")
	case 101:
		return info, fmt.Errorf("empty attestation quote")
	case 102:
		return info, fmt.Errorf("invalid expected REPORTDATA")
	case 103:
		return info, fmt.Errorf("quote too small for SGX quote header")
	case 104:
		return info, fmt.Errorf("REPORTDATA check supports SGX quote version 3 only")
	case 105:
		return info, fmt.Errorf("quote too small for SGX REPORTDATA")
	case 106:
		return info, fmt.Errorf("REPORTDATA policy check failed")
	case 200:
		return info, fmt.Errorf("DCAP quote verification call failed: 0x%x", info.DCAPReturn)
	case 201:
		return info, fmt.Errorf("quote verified but collateral is expired")
	case 202:
		return info, fmt.Errorf("quote verification rejected result: 0x%x", info.QuoteVerificationResult)
	default:
		return info, fmt.Errorf("unknown quote verification status: %d", int(status))
	}
}
