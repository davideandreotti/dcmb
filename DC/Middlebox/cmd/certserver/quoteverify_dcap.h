#pragma once
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
    uint32_t dcap_return, collateral_expiration, qv_result, qvl_status;
    char error[256];
} dcmb_quote_result;

// One immutable collateral cache per process. Failed initialization is retried
// by the next request; a changed certification chain or expired cache rejects.
int dcmb_verify_quote(const uint8_t* quote, uint32_t quote_size,
                      const uint8_t* expected_report_data, uint32_t expected_size,
                      dcmb_quote_result* result);

#ifdef __cplusplus
}
#endif
