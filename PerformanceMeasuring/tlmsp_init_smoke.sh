#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
TLMSP_INSTALL="${TLMSP_INSTALL:-$PROJECT_ROOT/.tlmsp}"
TLMSP_CONFIG="${TLMSP_CONFIG:-$PROJECT_ROOT/ETSI/Configurations/local_init.ucl}"
TLMSP_APACHE_CONFIG_SRC="${TLMSP_APACHE_CONFIG_SRC:-$PROJECT_ROOT/ETSI/httpd_tlmsp_local.conf}"
TLMSP_APACHE_CONFIG_DST="${TLMSP_APACHE_CONFIG_DST:-$TLMSP_INSTALL/etc/apache24/httpd_tlmsp.conf}"
TLMSP_BACKEND_PORT="${TLMSP_BACKEND_PORT:-7000}"

usage() {
    cat <<USAGE
Usage: $0 <command>

Commands:
  install-config  Copy the local TLMSP Apache vhost into .tlmsp.
  backend         Run the plain HTTP /function/init backend on 127.0.0.1:${TLMSP_BACKEND_PORT}.
  httpd           Run TLMSP Apache in foreground/debug mode.
  listener        Run the Go TLMSP policy listener on :8080.
  middlebox       Run tlmsp-mb with local_init.ucl.
  curl            Send the TLMSP /function/init smoke request.
  check-backend   Send a direct plain HTTP /function/init request to the backend.
  commands        Print the recommended terminal layout.
USAGE
}

source_tlmsp_env() {
    # shellcheck disable=SC1091
    . "$TLMSP_INSTALL/share/tlmsp-tools/tlmsp-env.sh"
}

install_config() {
    cp "$TLMSP_APACHE_CONFIG_SRC" "$TLMSP_APACHE_CONFIG_DST"
    printf 'Installed %s\n' "$TLMSP_APACHE_CONFIG_DST"
}

run_backend() {
    cd "$SCRIPT_DIR"
    TLMSP_INIT_BACKEND_PORT="$TLMSP_BACKEND_PORT" exec python3 -u init_http_server.py
}

run_httpd() {
    source_tlmsp_env
    rm -f "$TLMSP_INSTALL/var/logs/httpd.pid"
    exec "$TLMSP_INSTALL/bin/httpd" -X -e debug
}

run_listener() {
    cd "$PROJECT_ROOT/ETSI/NewMiddlebox"
    exec ./listener
}

run_middlebox() {
    source_tlmsp_env
    cd "$PROJECT_ROOT/ETSI/NewMiddlebox"
    exec tlmsp-mb -c "$TLMSP_CONFIG" -a -P
}

run_curl() {
    source_tlmsp_env
    exec curl --tlmsp "$TLMSP_CONFIG" \
        -k -v \
        -H 'X-Testing: 1' \
        -H 'Authorization: Bearer token' \
        -H 'Content-Type: application/json' \
        -d '{}' \
        https://127.0.0.1:4444/function/init
}

check_backend() {
    exec curl -v \
        -H 'Authorization: Bearer token' \
        -H 'Content-Type: application/json' \
        -d '{}' \
        "http://127.0.0.1:${TLMSP_BACKEND_PORT}/function/init"
}

print_commands() {
    cat <<COMMANDS
Terminal 1:
  $0 backend

Terminal 2:
  $0 httpd

Terminal 3:
  cd "$PROJECT_ROOT/ETSI/NewMiddlebox"
  ./listener

Terminal 4:
  $0 middlebox

Terminal 5:
  $0 check-backend
  $0 curl
COMMANDS
}

command="${1:-}"
case "$command" in
    install-config) install_config ;;
    backend) run_backend ;;
    httpd) run_httpd ;;
    listener) run_listener ;;
    middlebox) run_middlebox ;;
    curl) run_curl ;;
    check-backend) check_backend ;;
    commands) print_commands ;;
    -h|--help|help|"") usage ;;
    *)
        printf 'Unknown command: %s\n\n' "$command" >&2
        usage >&2
        exit 2
        ;;
esac
