// Exercise the actual wrapper on a locally captured SGX v3 quote. Including the
// implementation permits expiry/clock tests without production-only test hooks.
#include "../quoteverify_dcap.cpp"
#include <fstream>
#include <iostream>
#include <iterator>
#include <thread>

int main(int argc, char** argv) {
    try {
        require(argc == 2, "usage: quoteverify_test <quote.bin>");
        std::ifstream input(argv[1], std::ios::binary);
        std::vector<uint8_t> quote((std::istreambuf_iterator<char>(input)), {});
        require(quote.size() > 1020, "missing or undersized SGX quote fixture");
        std::vector<uint8_t> expected(quote.begin() + 368, quote.begin() + 432);
        unsigned checks = 0;
        auto check = [&](const std::vector<uint8_t>& q, const std::vector<uint8_t>& report, bool accept) {
            dcmb_quote_result result;
            int status = dcmb_verify_quote(q.data(), q.size(), report.data(), report.size(), &result);
            if ((status == 0) != accept)
                throw std::runtime_error(std::string("unexpected verification outcome: ") + result.error);
            ++checks;
            return result;
        };
        auto flip = [&](size_t offset) { auto q = quote; q.at(offset) ^= 1; return q; };
        check(flip(112), expected, false);
        require(!context, "invalid warmup populated the cache");
        ++checks;

        // Contending first requests must all observe a fully initialized cache.
        std::vector<std::thread> threads;
        std::vector<int> status(8);
        std::vector<dcmb_quote_result> results(8);
        for (size_t i = 0; i < status.size(); ++i)
            threads.emplace_back([&, i] {
                status[i] = dcmb_verify_quote(quote.data(), quote.size(), expected.data(), expected.size(), &results[i]);
            });
        for (auto& thread : threads) thread.join();
        for (size_t i = 0; i < status.size(); ++i) {
            require(status[i] == 0 && accepted(results[i].qv_result) && results[i].collateral_expiration == 0,
                    "concurrent initialization failed");
            require(results[i].qv_result == results[0].qv_result, "concurrent result disagreement");
            ++checks;
        }

        check(quote, expected, true);
        check(quote, {}, true); // REPORTDATA policy remains optional.
        for (size_t offset : {112, 436, 500, 564, 948, 368, 0, 4})
            check(flip(offset), expected, false);
        std::string bytes(quote.begin(), quote.end());
        auto cert = bytes.find("-----BEGIN CERTIFICATE-----");
        require(cert != std::string::npos, "fixture lacks PCK chain");
        check(flip(cert + 50), expected, false);
        check({}, expected, false);
        check(std::vector<uint8_t>(quote.begin(), quote.begin() + 100), expected, false);
        check(std::vector<uint8_t>(quote.begin(), quote.end() - 100), expected, false);
        check(std::vector<uint8_t>(65537), expected, false);
        check(quote, std::vector<uint8_t>(32), false);
        check(quote, std::vector<uint8_t>(64), false);
        auto altered = flip(368);
        check(altered, std::vector<uint8_t>(altered.begin() + 368, altered.begin() + 432), false);
        check(altered, {}, false); // Signature check still applies with policy disabled.

        auto expires = context->expires;
        context->expires = std::time(nullptr) - 1;
        require(check(quote, expected, false).collateral_expiration == 1, "missing expiry diagnostic");
        context->expires = expires;
        auto validated_at = context->validated_at;
        context->validated_at = std::time(nullptr) + 60;
        check(quote, expected, false);
        context->validated_at = validated_at;

        // Mixed concurrent success/failure must keep error buffers request-local.
        threads.clear();
        auto invalid = flip(436);
        for (size_t i = 0; i < status.size(); ++i)
            threads.emplace_back([&, i] {
                const auto& q = i % 2 ? invalid : quote;
                status[i] = dcmb_verify_quote(q.data(), q.size(), expected.data(), expected.size(), &results[i]);
            });
        for (auto& thread : threads) thread.join();
        for (size_t i = 0; i < status.size(); ++i) {
            require((status[i] == 0) == (i % 2 == 0), "concurrent outcome mismatch");
            require((results[i].error[0] == 0) == (i % 2 == 0), "concurrent error contamination");
            ++checks;
        }
        check(quote, expected, true);
        std::cout << checks << " checks passed\n";
    } catch (const std::exception& e) {
        std::cerr << e.what() << '\n';
        return 1;
    }
}
