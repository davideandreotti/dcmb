#!/usr/bin/env bash

set -euo pipefail

SERVICE_NAME="cpu-performance-mode.service"
SERVICE_PATH="/etc/systemd/system/$SERVICE_NAME"
STATE_DIR="/var/lib/cpu-performance-mode"
STATE_FILE="$STATE_DIR/original-settings"
CPUFREQ_DIR="/sys/devices/system/cpu/cpufreq"

# Used only if no saved pre-performance settings exist.
OFF_GOVERNOR="${CPU_GOVERNOR_OFF:-powersave}"
OFF_EPP="${CPU_EPP_OFF:-balance_performance}"
PERFORMANCE_IDLE_DISABLE_STATES="${CPU_IDLE_DISABLE_STATES:-2 3}"

usage() {
    cat <<'USAGE'
Usage: sudo ./scripts/cpu-performance-mode.sh on
       sudo ./scripts/cpu-performance-mode.sh off
       ./scripts/cpu-performance-mode.sh status

Commands:
  on      Save current CPU scaling settings, install/enable the performance service,
          and apply performance governor/EPP immediately. Also disable idle
          states 2 and 3 by default, matching the benchmark tuning we used.
  off     Disable the performance service and restore the saved settings. If no saved
          settings exist, fall back to powersave + balance_performance. Also
          re-enable all CPU idle states.
  status  Print service state, current per-policy governor/EPP values, and
          disabled idle states.

Fallbacks can be overridden when no saved state exists:
  CPU_GOVERNOR_OFF=powersave CPU_EPP_OFF=balance_power sudo ./scripts/cpu-performance-mode.sh off

Performance idle states can be overridden:
  CPU_IDLE_DISABLE_STATES="2 3 4" sudo ./scripts/cpu-performance-mode.sh on
USAGE
}

require_root() {
    if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
        echo "This command must be run with sudo/root." >&2
        exit 1
    fi
}

policies() {
    find "$CPUFREQ_DIR" -maxdepth 1 -type d -name 'policy*' 2>/dev/null | sort -V
}

supports_value() {
    local available_file="$1"
    local value="$2"

    [[ ! -f "$available_file" ]] || grep -qw -- "$value" "$available_file"
}

write_setting() {
    local value_file="$1"
    local available_file="$2"
    local value="$3"

    [[ -f "$value_file" ]] || return 0

    if supports_value "$available_file" "$value"; then
        echo "$value" >"$value_file"
    else
        echo "Skipping unsupported value '$value' for $value_file" >&2
    fi
}

enable_all_idle_states() {
    if command -v cpupower >/dev/null 2>&1; then
        cpupower idle-set -E >/dev/null
        return
    fi

    local disable_file
    while IFS= read -r disable_file; do
        echo 0 >"$disable_file"
    done < <(find /sys/devices/system/cpu/cpu*/cpuidle/state* -type f -name disable 2>/dev/null)
}

disable_performance_idle_states() {
    local state

    if command -v cpupower >/dev/null 2>&1; then
        for state in $PERFORMANCE_IDLE_DISABLE_STATES; do
            cpupower idle-set -d "$state" >/dev/null
        done
        return
    fi

    local disable_file
    for state in $PERFORMANCE_IDLE_DISABLE_STATES; do
        while IFS= read -r disable_file; do
            echo 1 >"$disable_file"
        done < <(find /sys/devices/system/cpu/cpu*/cpuidle/state"$state" -type f -name disable 2>/dev/null)
    done
}

disabled_idle_states() {
    local disable_file state_dir cpu_dir cpu state name

    while IFS= read -r disable_file; do
        [[ "$(cat "$disable_file" 2>/dev/null)" == "1" ]] || continue
        state_dir="$(dirname "$disable_file")"
        cpu_dir="$(dirname "$(dirname "$state_dir")")"
        cpu="$(basename "$cpu_dir")"
        state="$(basename "$state_dir")"
        name="$(cat "$state_dir/name" 2>/dev/null || echo "$state")"
        printf '%s %s %s\n' "$cpu" "$state" "$name"
    done < <(find /sys/devices/system/cpu/cpu*/cpuidle/state* -type f -name disable 2>/dev/null)
}

save_current_settings_once() {
    [[ ! -f "$STATE_FILE" ]] || return 0

    mkdir -p "$STATE_DIR"
    chmod 700 "$STATE_DIR"

    local tmp
    tmp="$(mktemp)"

    local policy name
    while IFS= read -r policy; do
        name="$(basename "$policy")"
        if [[ -f "$policy/scaling_governor" ]]; then
            printf '%s governor %s\n' "$name" "$(cat "$policy/scaling_governor")" >>"$tmp"
        fi
        if [[ -f "$policy/energy_performance_preference" ]]; then
            printf '%s epp %s\n' "$name" "$(cat "$policy/energy_performance_preference")" >>"$tmp"
        fi
    done < <(policies)

    if [[ -s "$tmp" ]]; then
        mv "$tmp" "$STATE_FILE"
        chmod 600 "$STATE_FILE"
    else
        rm -f "$tmp"
    fi
}

install_service() {
    local idle_states
    idle_states="$PERFORMANCE_IDLE_DISABLE_STATES"

    cat >"$SERVICE_PATH" <<'SERVICE'
[Unit]
Description=Set CPU governor and EPP to performance
After=multi-user.target

[Service]
Type=oneshot
SERVICE
    cat >>"$SERVICE_PATH" <<SERVICE
Environment="CPU_IDLE_DISABLE_STATES=$idle_states"
SERVICE
    cat >>"$SERVICE_PATH" <<'SERVICE'
ExecStart=/bin/sh -c 'for p in /sys/devices/system/cpu/cpufreq/policy*; do [ -f "$p/scaling_governor" ] && echo performance > "$p/scaling_governor"; [ -f "$p/energy_performance_preference" ] && echo performance > "$p/energy_performance_preference"; done; if command -v cpupower >/dev/null 2>&1; then for s in $CPU_IDLE_DISABLE_STATES; do cpupower idle-set -d "$s" >/dev/null; done; else for s in $CPU_IDLE_DISABLE_STATES; do for f in /sys/devices/system/cpu/cpu*/cpuidle/state"$s"/disable; do [ -f "$f" ] && echo 1 > "$f"; done; done; fi'
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
SERVICE

    systemctl daemon-reload
    systemctl enable "$SERVICE_NAME" >/dev/null
    systemctl restart "$SERVICE_NAME"
}

restore_saved_settings() {
    local policy_name kind value policy

    while read -r policy_name kind value; do
        policy="$CPUFREQ_DIR/$policy_name"
        [[ -d "$policy" ]] || continue

        case "$kind" in
            governor)
                write_setting "$policy/scaling_governor" "$policy/scaling_available_governors" "$value"
                ;;
            epp)
                write_setting "$policy/energy_performance_preference" "$policy/energy_performance_available_preferences" "$value"
                ;;
        esac
    done <"$STATE_FILE"

    rm -f "$STATE_FILE"
}

apply_fallback_off_settings() {
    local policy

    while IFS= read -r policy; do
        write_setting "$policy/scaling_governor" "$policy/scaling_available_governors" "$OFF_GOVERNOR"
        write_setting "$policy/energy_performance_preference" "$policy/energy_performance_available_preferences" "$OFF_EPP"
    done < <(policies)
}

turn_on() {
    require_root
    save_current_settings_once
    install_service
    disable_performance_idle_states
    echo "Performance mode enabled; disabled CPU idle states: $PERFORMANCE_IDLE_DISABLE_STATES."
}

turn_off() {
    require_root

    if systemctl list-unit-files "$SERVICE_NAME" >/dev/null 2>&1; then
        systemctl disable --now "$SERVICE_NAME" >/dev/null 2>&1 || true
    fi

    if [[ -f "$STATE_FILE" ]]; then
        restore_saved_settings
        enable_all_idle_states
        echo "Performance mode disabled; restored saved CPU scaling settings and re-enabled CPU idle states."
    else
        apply_fallback_off_settings
        enable_all_idle_states
        echo "Performance mode disabled; no saved settings found, applied fallback: governor=$OFF_GOVERNOR epp=$OFF_EPP, and re-enabled CPU idle states."
    fi
}

status() {
    local enabled active
    enabled="$(systemctl is-enabled "$SERVICE_NAME" 2>/dev/null || true)"
    active="$(systemctl is-active "$SERVICE_NAME" 2>/dev/null || true)"
    [[ -n "$enabled" ]] || enabled="absent"
    [[ -n "$active" ]] || active="inactive"

    echo "Service:"
    echo "  enabled: $enabled"
    echo "  active:  $active"
    echo
    echo "Saved settings:"
    if [[ -f "$STATE_FILE" ]]; then
        sed 's/^/  /' "$STATE_FILE"
    else
        echo "  none"
    fi
    echo
    echo "Current CPU scaling:"

    local policy name
    while IFS= read -r policy; do
        name="$(basename "$policy")"
        if [[ -f "$policy/scaling_governor" ]]; then
            printf '  %s governor: %s\n' "$name" "$(cat "$policy/scaling_governor")"
        fi
        if [[ -f "$policy/energy_performance_preference" ]]; then
            printf '  %s epp:      %s\n' "$name" "$(cat "$policy/energy_performance_preference")"
        fi
    done < <(policies)

    echo
    echo "CPU idle states:"
    echo "  performance disables state indexes: $PERFORMANCE_IDLE_DISABLE_STATES"
    local disabled
    disabled="$(disabled_idle_states)"
    if [[ -n "$disabled" ]]; then
        echo "  disabled states:"
        sed 's/^/    /' <<<"$disabled"
    else
        echo "  disabled states: none"
    fi
}

case "${1:-}" in
    on)
        turn_on
        ;;
    off)
        turn_off
        ;;
    status)
        status
        ;;
    -h|--help|help)
        usage
        ;;
    *)
        usage >&2
        exit 2
        ;;
esac
