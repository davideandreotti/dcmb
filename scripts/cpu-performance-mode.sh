#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
STATE_FILE="$SCRIPT_DIR/cpu-performance-mode.local-state"
CPU_SYS_DIR="/sys/devices/system/cpu"
CPUFREQ_DIR="$CPU_SYS_DIR/cpufreq"
TUNED_PROFILE_FILE="/etc/tuned/active_profile"
TUNED_MODE_FILE="/etc/tuned/profile_mode"
PERFORMANCE_IDLE_DISABLE_STATES="${CPU_IDLE_DISABLE_STATES:-2 3}"

usage() {
    cat <<'USAGE'
Usage: sudo ./scripts/cpu-performance-mode.sh on
       sudo ./scripts/cpu-performance-mode.sh off
       ./scripts/cpu-performance-mode.sh status

Commands:
  on      Save the current TuneD profile/mode, CPU governor/EPP, and idle-state
          flags alongside this script. Apply latency-performance and performance
          governor/EPP, and disable idle states 2 and 3 by default.
  off     Restore the saved profile/mode and exact CPU settings. Without saved
          settings, refuse to guess. No service is installed at boot.
  status  Print the TuneD profile/mode, saved-state location, current governor/EPP,
          and disabled idle states. Use sudo to read the root-owned saved state.

Run on before experiments and off afterwards; restoration is manual.
TuneD keeps the selected profile across reboot until you change it back.

Performance idle states can be overridden:
  sudo env CPU_IDLE_DISABLE_STATES="2 3 4" ./scripts/cpu-performance-mode.sh on
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

idle_state_files() {
    local path
    for path in "$CPU_SYS_DIR"/cpu*/cpuidle/state*/disable; do
        if [[ -f "$path" ]]; then
            printf '%s\n' "$path"
        fi
    done
}

disable_performance_idle_states() {
    local state disable_file
    for state in $PERFORMANCE_IDLE_DISABLE_STATES; do
        for disable_file in "$CPU_SYS_DIR"/cpu*/cpuidle/state"$state"/disable; do
            if [[ -f "$disable_file" ]]; then
                echo 1 >"$disable_file"
            fi
        done
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
    done < <(idle_state_files)
}

save_current_settings_once() {
    [[ ! -f "$STATE_FILE" ]] || return 0

    local tmp
    tmp="$(mktemp "$STATE_FILE.tmp.XXXXXX")"
    printf 'tuned profile %s\n' "$(cat "$TUNED_PROFILE_FILE")" >>"$tmp"
    printf 'tuned mode %s\n' "$(cat "$TUNED_MODE_FILE")" >>"$tmp"

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

    local disable_file state_dir cpu_dir
    while IFS= read -r disable_file; do
        state_dir="$(dirname "$disable_file")"
        cpu_dir="$(dirname "$(dirname "$state_dir")")"
        printf '%s/%s idle %s\n' "$(basename "$cpu_dir")" \
            "$(basename "$state_dir")" "$(cat "$disable_file")" >>"$tmp"
    done < <(idle_state_files)
    mv "$tmp" "$STATE_FILE"
}

restore_saved_settings() {
    local target kind value value_file

    while read -r target kind value; do
        case "$kind" in
            governor)
                [[ "$target" =~ ^policy[0-9]+$ ]] || return 1
                value_file="$CPUFREQ_DIR/$target/scaling_governor"
                ;;
            epp)
                [[ "$target" =~ ^policy[0-9]+$ ]] || return 1
                value_file="$CPUFREQ_DIR/$target/energy_performance_preference"
                ;;
            idle)
                [[ "$target" =~ ^cpu[0-9]+/state[0-9]+$ && "$value" =~ ^[01]$ ]] || return 1
                value_file="$CPU_SYS_DIR/${target%/*}/cpuidle/${target#*/}/disable"
                ;;
            profile|mode) continue ;;
            *) return 1 ;;
        esac
        if [[ ! -f "$value_file" ]]; then
            echo "Cannot restore missing setting: $value_file; keeping saved state." >&2
            return 1
        fi
        echo "$value" >"$value_file"
        if [[ "$(cat "$value_file")" != "$value" ]]; then
            echo "Restoration failed: $value_file; keeping saved state." >&2
            return 1
        fi
    done <"$STATE_FILE"
}

apply_performance_settings() {
    local policy

    while IFS= read -r policy; do
        write_setting "$policy/scaling_governor" "$policy/scaling_available_governors" performance
        write_setting "$policy/energy_performance_preference" "$policy/energy_performance_available_preferences" performance
    done < <(policies)
}

turn_on() {
    require_root
    command -v tuned-adm >/dev/null
    [[ -s "$TUNED_PROFILE_FILE" && -s "$TUNED_MODE_FILE" ]]
    local state
    for state in $PERFORMANCE_IDLE_DISABLE_STATES; do
        [[ "$state" =~ ^[0-9]+$ ]] || { echo "Invalid idle-state index: $state" >&2; return 1; }
    done
    save_current_settings_once
    tuned-adm profile latency-performance
    tuned-adm verify
    apply_performance_settings
    disable_performance_idle_states
    echo "Latency-performance enabled; disabled CPU idle states: $PERFORMANCE_IDLE_DISABLE_STATES."
    echo "Original settings saved in $STATE_FILE. Run this script with off after experiments."
}

turn_off() {
    require_root
    if [[ ! -f "$STATE_FILE" ]]; then
        echo "No saved settings found at $STATE_FILE; nothing changed, no fallback applied." >&2
        return 1
    fi
    local original_profile original_mode
    original_profile="$(sed -n 's/^tuned profile //p' "$STATE_FILE")"
    original_mode="$(sed -n 's/^tuned mode //p' "$STATE_FILE")"
    [[ -n "$original_profile" && "$original_mode" =~ ^(auto|manual)$ ]] || return 1
    if [[ "$original_mode" == auto ]]; then
        if [[ "$(tuned-adm recommend)" != "$original_profile" ]]; then
            echo "Automatic recommendation changed; cannot restore the original profile and auto mode. Keeping saved state." >&2
            return 1
        fi
        tuned-adm auto_profile
    else
        local profiles
        read -r -a profiles <<<"$original_profile"
        tuned-adm profile "${profiles[@]}"
    fi
    [[ "$(cat "$TUNED_PROFILE_FILE")" == "$original_profile" && \
       "$(cat "$TUNED_MODE_FILE")" == "$original_mode" ]]
    tuned-adm verify
    restore_saved_settings
    rm -- "$STATE_FILE"
    echo "Restored TuneD profile $original_profile ($original_mode) and saved CPU governor/EPP/idle settings."
}

status() {
    echo "TuneD:"
    echo "  profile: $(cat "$TUNED_PROFILE_FILE" 2>/dev/null || echo unavailable)"
    echo "  mode:    $(cat "$TUNED_MODE_FILE" 2>/dev/null || echo unavailable)"
    echo
    echo "Saved settings:"
    echo "  file: $STATE_FILE"
    if [[ -r "$STATE_FILE" ]]; then
        sed 's/^/  /' "$STATE_FILE"
    elif [[ -f "$STATE_FILE" ]]; then
        echo "  saved state exists; use sudo to read it"
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
