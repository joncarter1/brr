#!/bin/bash
# idle-shutdown -- Shuts down instance after sustained idle period.
# Installed by setup.sh or UserData, managed by systemd (idle-shutdown.service).
#
# Monitors CPU, GPU (if available), SSH sessions, and network throughput.
# Node is "idle" only when ALL metrics are below thresholds.
#
# Usage: journalctl -u idle-shutdown -f

set -euo pipefail

# --- Configuration (passed as env vars by systemd) ---
IDLE_TIMEOUT_MIN="${IDLE_SHUTDOWN_TIMEOUT_MIN}"
IDLE_TIMEOUT_SEC=$((IDLE_TIMEOUT_MIN * 60))
CPU_THRESHOLD="${IDLE_SHUTDOWN_CPU_THRESHOLD}"
NET_THRESHOLD_KBPS="${IDLE_SHUTDOWN_NET_THRESHOLD_KBPS:-100}"
GRACE_PERIOD=$(( ${IDLE_SHUTDOWN_GRACE_MIN} * 60 ))
CHECK_INTERVAL=60
GPU_THRESHOLD=5

# --- Helpers ---
log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') $*"
}

get_cpu_usage() {
    # Sample /proc/stat twice (1s apart), compute CPU % from delta.
    local line1 line2
    line1=$(head -1 /proc/stat)
    local -a f1
    read -ra f1 <<< "$line1"
    local total1=0 idle1=0
    for i in $(seq 1 10); do
        total1=$((total1 + ${f1[$i]:-0}))
    done
    idle1=$(( ${f1[4]:-0} + ${f1[5]:-0} ))  # idle + iowait

    sleep 1

    line2=$(head -1 /proc/stat)
    local -a f2
    read -ra f2 <<< "$line2"
    local total2=0 idle2=0
    for i in $(seq 1 10); do
        total2=$((total2 + ${f2[$i]:-0}))
    done
    idle2=$(( ${f2[4]:-0} + ${f2[5]:-0} ))

    local total_delta=$((total2 - total1))
    local idle_delta=$((idle2 - idle1))

    if [ "$total_delta" -eq 0 ]; then
        echo 0
        return
    fi

    echo $(( (total_delta - idle_delta) * 100 / total_delta ))
}

HAS_GPU=""
get_gpu_usage() {
    # Returns max GPU utilization % across all GPUs, or 0 if no GPU.
    if [ -z "$HAS_GPU" ]; then
        if command -v nvidia-smi >/dev/null 2>&1; then
            HAS_GPU="yes"
        else
            HAS_GPU="no"
        fi
    fi

    if [ "$HAS_GPU" = "no" ]; then
        echo 0
        return
    fi

    local gpu_util max_util=0
    gpu_util=$(nvidia-smi --query-gpu=utilization.gpu --format=csv,noheader,nounits 2>/dev/null || echo "0")
    while IFS= read -r val; do
        val=$(echo "$val" | tr -d ' ')
        if [ -n "$val" ] && [ "$val" -gt "$max_util" ] 2>/dev/null; then
            max_util=$val
        fi
    done <<< "$gpu_util"
    echo "$max_util"
}

get_ssh_sessions() {
    local count
    count=$(ss -tnp 2>/dev/null | grep -c ':22 ' 2>/dev/null) || count=0
    echo "$count"
}

get_network_bytes() {
    # Sum rx+tx bytes across all non-loopback interfaces from /proc/net/dev.
    awk 'NR>2 && $1 !~ /^lo:/ { total += $2 + $10 } END { print total+0 }' /proc/net/dev
}

get_network_kbps() {
    # Returns rx+tx throughput in KB/s averaged over a 1-second window.
    local b1 b2
    b1=$(get_network_bytes)
    sleep 1
    b2=$(get_network_bytes)
    echo $(( (b2 - b1) / 1024 ))
}

# --- Main ---
log "idle-shutdown daemon starting"
log "config: timeout=${IDLE_TIMEOUT_SEC}s cpu_threshold=${CPU_THRESHOLD}% gpu_threshold=${GPU_THRESHOLD}% net_threshold=${NET_THRESHOLD_KBPS}KB/s grace=${GRACE_PERIOD}s interval=${CHECK_INTERVAL}s"

log "grace period: sleeping ${GRACE_PERIOD}s before monitoring"
sleep "$GRACE_PERIOD"
log "grace period complete, beginning idle monitoring"

IDLE_SINCE=""

while true; do
    cpu=$(get_cpu_usage)
    gpu=$(get_gpu_usage)
    ssh_count=$(get_ssh_sessions)
    net_kbps=$(get_network_kbps)

    is_active=false
    [ "$cpu" -gt "$CPU_THRESHOLD" ] && is_active=true
    [ "$gpu" -gt "$GPU_THRESHOLD" ] && is_active=true
    [ "$ssh_count" -gt 0 ] && is_active=true
    [ "$net_kbps" -gt "$NET_THRESHOLD_KBPS" ] && is_active=true

    now=$(date +%s)

    if [ "$is_active" = true ]; then
        IDLE_SINCE=""
        log "active: cpu=${cpu}% gpu=${gpu}% ssh=${ssh_count} net=${net_kbps}KB/s"
    else
        if [ -z "$IDLE_SINCE" ]; then
            IDLE_SINCE=$now
        fi
        idle_elapsed=$((now - IDLE_SINCE))
        idle_min=$((idle_elapsed / 60))
        log "idle: cpu=${cpu}% gpu=${gpu}% ssh=${ssh_count} net=${net_kbps}KB/s -- ${idle_min}m / ${IDLE_TIMEOUT_MIN}m"

        if [ "$idle_elapsed" -ge "$IDLE_TIMEOUT_SEC" ]; then
            log "IDLE TIMEOUT REACHED -- shutting down"
            shutdown -h now
            exit 0
        fi
    fi

    sleep "$CHECK_INTERVAL"
done
