cmake_minimum_required(VERSION 3.16)
project(dcmb_qvl LANGUAGES CXX)
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)
if(NOT DCAP_SOURCE)
  message(FATAL_ERROR "Set DCAP_SOURCE to the unmodified Intel DCAP source checkout (tested: 1.23)")
endif()
set(QVL "${DCAP_SOURCE}/QuoteVerification/QVL/Src")
set(OPENSSL_USE_STATIC_LIBS TRUE)
find_package(OpenSSL 3.0 REQUIRED COMPONENTS Crypto)
set(VERSION "1.0.0")
configure_file("${QVL}/AttestationLibrary/templates/Version.h.in" "generated/Version/Version.h" @ONLY)
configure_file("${QVL}/AttestationLibrary/include/SgxEcdsaAttestation/QuoteVerification.h"
  "include/SgxEcdsaAttestation/QuoteVerification.h" COPYONLY)
# Unmodified Intel QVL, linked privately to the system OpenSSL archive. No install
# step, downloaded dependencies, or changes to the Intel checkout are needed.
file(GLOB INTEL_SOURCES
  "${QVL}/AttestationCommons/src/*.cpp" "${QVL}/AttestationCommons/src/Utils/*.cpp"
  "${QVL}/AttestationParsers/src/*.cpp" "${QVL}/AttestationParsers/src/Json/*.cpp"
  "${QVL}/AttestationParsers/src/X509/*.cpp" "${QVL}/AttestationParsers/src/OpensslHelpers/*.cpp"
  "${QVL}/AttestationLibrary/src/*.cpp" "${QVL}/AttestationLibrary/src/OpensslHelpers/*.cpp"
  "${QVL}/AttestationLibrary/src/PckParser/*.cpp" "${QVL}/AttestationLibrary/src/CertVerification/*.cpp"
  "${QVL}/AttestationLibrary/src/QuoteVerification/*.cpp" "${QVL}/AttestationLibrary/src/Verifiers/*.cpp"
  "${QVL}/AttestationLibrary/src/Verifiers/Checks/*.cpp" "${QVL}/AttestationLibrary/src/Utils/*.cpp")
add_library(dcmb_qvl SHARED ${INTEL_SOURCES})
target_include_directories(dcmb_qvl PRIVATE
  "${CMAKE_CURRENT_BINARY_DIR}/generated"
  "${QVL}/AttestationCommons/include" "${QVL}/AttestationCommons/include/OpensslHelpers"
  "${QVL}/AttestationCommons/include/Utils" "${QVL}/AttestationParsers/include"
  "${QVL}/AttestationParsers/src" "${QVL}/AttestationLibrary/include"
  "${QVL}/AttestationLibrary/src" "${QVL}/ThirdParty/rapidjson/include"
  "${OPENSSL_INCLUDE_DIR}")
target_compile_options(dcmb_qvl PRIVATE -O2 -fvisibility=hidden)
target_link_libraries(dcmb_qvl PRIVATE OpenSSL::Crypto dl pthread)
target_link_options(dcmb_qvl PRIVATE -Wl,--exclude-libs,ALL -Wl,-Bsymbolic -Wl,--no-undefined)
