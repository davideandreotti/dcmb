//go:build dcapverify

#include "quoteverify_dcap.h"
#include <SgxEcdsaAttestation/QuoteVerification.h>
#include <sgx_dcap_quoteverify.h>
#include <sgx_quote_3.h>
#include <openssl/pem.h>
#include <cstdio>
#include <cstring>
#include <ctime>
#include <memory>
#include <mutex>
#include <stdexcept>
#include <string>
#include <vector>

namespace {
struct Context {
    uint8_t* collateral = nullptr;
    std::vector<uint8_t> chain;
    std::string leaf, pck_crl;
    time_t expires = 0, validated_at = 0;
    ~Context() { if (collateral) tee_qv_free_collateral(collateral); }
};
std::mutex init_mutex;
std::unique_ptr<Context> context;

void require(bool ok, const char* why) {
    if (!ok) throw std::runtime_error(why);
}

bool accepted(uint32_t result) {
    return result == SGX_QL_QV_RESULT_OK || result == SGX_QL_QV_RESULT_CONFIG_NEEDED ||
           result == SGX_QL_QV_RESULT_OUT_OF_DATE || result == SGX_QL_QV_RESULT_OUT_OF_DATE_CONFIG_NEEDED;
}

uint32_t translate(Status status) {
    switch (status) {
    case STATUS_OK: return SGX_QL_QV_RESULT_OK;
    case STATUS_TCB_CONFIGURATION_NEEDED: return SGX_QL_QV_RESULT_CONFIG_NEEDED;
    case STATUS_TCB_OUT_OF_DATE: return SGX_QL_QV_RESULT_OUT_OF_DATE;
    case STATUS_TCB_OUT_OF_DATE_CONFIGURATION_NEEDED: return SGX_QL_QV_RESULT_OUT_OF_DATE_CONFIG_NEEDED;
    default: return SGX_QL_QV_RESULT_UNSPECIFIED;
    }
}

std::vector<uint8_t> certification_chain(const uint8_t* quote, uint32_t size, dcmb_quote_result& out) {
    uint32_t chain_size = 0;
    out.qvl_status = sgxAttestationGetQECertificationDataSize(quote, size, &chain_size);
    require(out.qvl_status == STATUS_OK && chain_size > 0 && chain_size <= size, "invalid certification size");
    std::vector<uint8_t> chain(chain_size);
    uint16_t type = 0;
    out.qvl_status = sgxAttestationGetQECertificationData(quote, size, chain_size, chain.data(), &type);
    require(out.qvl_status == STATUS_OK && type == 5, "expected embedded PCK certificate chain (type 5)");
    return chain;
}

std::string pem(X509* cert) {
    std::unique_ptr<BIO, decltype(&BIO_free)> bio(BIO_new(BIO_s_mem()), BIO_free);
    require(bio && PEM_write_bio_X509(bio.get(), cert), "certificate serialization failed");
    char* data = nullptr;
    long size = BIO_get_mem_data(bio.get(), &data);
    require(size > 0, "empty certificate");
    return std::string(data, size);
}

// Preserve Intel's accepted PEM/hex-DER representation. The provider may return
// binary DER, which must be hex-encoded for the granular QVL string interface.
std::string crl_string(const char* data, uint32_t size) {
    require(data && size, "empty CRL");
    if (size >= 10 && std::memcmp(data, "-----BEGIN", 10) == 0)
        return std::string(data, strnlen(data, size));
    size_t len = size;
    if (data[len - 1] == 0) --len;
    bool hex = len > 0 && len % 2 == 0;
    for (size_t i = 0; i < len && hex; ++i)
        hex = (data[i] >= '0' && data[i] <= '9') || (data[i] >= 'a' && data[i] <= 'f') ||
              (data[i] >= 'A' && data[i] <= 'F');
    if (hex) return std::string(data, len);
    static const char digits[] = "0123456789abcdef";
    std::string result;
    result.reserve(size * 2);
    for (uint32_t i = 0; i < size; ++i) {
        auto byte = static_cast<uint8_t>(data[i]);
        result += digits[byte >> 4];
        result += digits[byte & 15];
    }
    return result;
}

void verify_granular(const Context& ctx, const uint8_t* quote, uint32_t size, dcmb_quote_result& out) {
    const auto* col = reinterpret_cast<const sgx_ql_qve_collateral_t*>(ctx.collateral);
    auto status = sgxAttestationVerifyQuote(quote, size, ctx.leaf.c_str(), ctx.pck_crl.c_str(),
                                           col->tcb_info, col->qe_identity);
    out.qvl_status = status;
    out.qv_result = translate(status);
    require(accepted(out.qv_result), "quote verification rejected");
}

std::unique_ptr<Context> initialize(const uint8_t* quote, uint32_t size,
                                   std::vector<uint8_t> chain, time_t now, dcmb_quote_result& out) {
    auto ctx = std::make_unique<Context>();
    uint32_t collateral_size = 0;
    out.dcap_return = tee_qv_get_collateral(quote, size, &ctx->collateral, &collateral_size);
    require(out.dcap_return == SGX_QL_SUCCESS, "collateral retrieval failed");
    uint32_t version = 0, supp_size = 0;
    out.dcap_return = tee_get_supplemental_data_version_and_size(quote, size, &version, &supp_size);
    require(out.dcap_return == SGX_QL_SUCCESS && supp_size >= sizeof(sgx_ql_qv_supplemental_t),
            "supplemental metadata unavailable");
    std::vector<uint8_t> supp(supp_size);
    tee_supp_data_descriptor_t desc = {};
    desc.data_size = supp_size;
    desc.p_data = supp.data();
    sgx_ql_qv_result_t result = SGX_QL_QV_RESULT_UNSPECIFIED;
    out.collateral_expiration = 1;
    out.dcap_return = tee_verify_quote(quote, size, ctx->collateral, now,
                                       &out.collateral_expiration, &result, nullptr, &desc);
    out.qv_result = result;
    require(out.dcap_return == SGX_QL_SUCCESS && out.collateral_expiration == 0 && accepted(result),
            "initial full verification rejected (including expired collateral)");
    // This full verification anchors the chain in Intel's pinned root key.
    // Never trust the root solely because it appears in the incoming quote.
    const auto* metadata = reinterpret_cast<const sgx_ql_qv_supplemental_t*>(supp.data());
    require(metadata->major_version == 3, "unsupported supplemental metadata version");
    ctx->expires = metadata->earliest_expiration_date;
    ctx->validated_at = now;
    require(ctx->expires > now, "collateral already expired");
    ctx->chain = std::move(chain);
    std::unique_ptr<BIO, decltype(&BIO_free)> bio(
        BIO_new_mem_buf(ctx->chain.data(), static_cast<int>(ctx->chain.size())), BIO_free);
    require(bio != nullptr, "certificate buffer allocation failed");
    std::string root;
    int count = 0;
    while (X509* raw = PEM_read_bio_X509(bio.get(), nullptr, nullptr, nullptr)) {
        std::unique_ptr<X509, decltype(&X509_free)> cert(raw, X509_free);
        if (count == 0) ctx->leaf = pem(cert.get());
        root = pem(cert.get());
        ++count;
    }
    require(count == 3, "expected three certificates");
    const auto* col = reinterpret_cast<const sgx_ql_qve_collateral_t*>(ctx->collateral);
    const auto root_crl = crl_string(col->root_ca_crl, col->root_ca_crl_size);
    ctx->pck_crl = crl_string(col->pck_crl, col->pck_crl_size);
    const char* crls[] = {root_crl.c_str(), ctx->pck_crl.c_str()};
    std::string pem_chain(ctx->chain.begin(), ctx->chain.end());
    out.qvl_status = sgxAttestationVerifyPCKCertificate(pem_chain.c_str(), crls, root.c_str(), &now);
    require(out.qvl_status == STATUS_OK, "PCK collateral validation failed");
    out.qvl_status = sgxAttestationVerifyTCBInfo(col->tcb_info, col->tcb_info_issuer_chain,
                                               root_crl.c_str(), root.c_str(), &now);
    require(out.qvl_status == STATUS_OK, "TCB collateral validation failed");
    out.qvl_status = sgxAttestationVerifyEnclaveIdentity(col->qe_identity, col->qe_identity_issuer_chain,
                                                       root_crl.c_str(), root.c_str(), &now);
    require(out.qvl_status == STATUS_OK, "QE identity collateral validation failed");
    verify_granular(*ctx, quote, size, out);
    require(out.qv_result == static_cast<uint32_t>(result), "initial verification result disagreement");
    return ctx;
}
}

extern "C" int dcmb_verify_quote(const uint8_t* quote, uint32_t size,
                                 const uint8_t* expected, uint32_t expected_size, dcmb_quote_result* out) {
    if (!out) return 1;
    *out = {};
    out->qv_result = SGX_QL_QV_RESULT_UNSPECIFIED;
    try {
        require(quote && size >= sizeof(sgx_quote3_t) && size <= 65536, "invalid quote size");
        uint16_t version = 0;
        uint32_t tee = 0;
        std::memcpy(&version, quote, 2);
        std::memcpy(&tee, quote + 4, 4);
        require(version == 3 && tee == 0, "only SGX quote v3 supported");
        require(expected_size == 0 || (expected && expected_size == 64), "invalid expected REPORTDATA");
        constexpr size_t offset = offsetof(sgx_quote3_t, report_body) + offsetof(sgx_report_body_t, report_data);
        if (expected_size)
            require(std::memcmp(quote + offset, expected, 64) == 0, "REPORTDATA mismatch");
        auto chain = certification_chain(quote, size, *out);
        const Context* ctx;
        {
            // Publish only after successful full initialization. Later calls use
            // the immutable context concurrently; failures leave it uninitialized.
            std::lock_guard<std::mutex> guard(init_mutex);
            if (!context) {
                context = initialize(quote, size, std::move(chain), std::time(nullptr), *out);
                return 0;
            }
            ctx = context.get();
        }
        time_t now = std::time(nullptr);
        out->collateral_expiration = now >= ctx->expires;
        require(now >= ctx->validated_at && now < ctx->expires,
                "cached collateral expired or clock predates validation; restart certserver");
        require(chain == ctx->chain, "certification chain changed; restart certserver");
        verify_granular(*ctx, quote, size, *out);
        return 0;
    } catch (const std::exception& e) {
        std::snprintf(out->error, sizeof(out->error), "%s", e.what());
    } catch (...) {
        std::snprintf(out->error, sizeof(out->error), "unknown verifier exception");
    }
    return 1;
}
