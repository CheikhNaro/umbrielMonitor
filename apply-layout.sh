#!/usr/bin/env bash
# apply-layout.sh — re-apply the saved monitor layout live at session start.
#
# Reads ~/.config/umbriel/state/monitor-layout.json (written by
# umbriel_monitor_gui.py) and applies each output's mode, position, scale,
# transform and enabled state in place with wlr-randr + native Umbriel
# actions. Nothing here writes a watched config file, so no config reload is
# triggered — which is exactly what keeps Noctalia alive (Umbriel reloads that
# recreate outputs currently segfault it: upstream negative-viewport bug).
set -uo pipefail

LAYOUT="${XDG_CONFIG_HOME:-$HOME/.config}/umbriel/state/monitor-layout.json"
MODE_FILE="${XDG_CONFIG_HOME:-$HOME/.config}/umbriel/state/monitor-mode"

[[ -f "$LAYOUT" ]] || exit 0
command -v umbriel >/dev/null 2>&1 || exit 0
umbriel outputs --json >/dev/null 2>&1 || exit 0

# Mirror/single are dynamic states managed by toggle-monitor.sh — the saved
# desktop layout must not fight them at login.
[[ "$(cat "$MODE_FILE" 2>/dev/null)" == "extended" || ! -f "$MODE_FILE" ]] || exit 0

# The compositor may still be mid-startup when autostart runs; wait until it
# answers (up to ~10 s).
for _ in $(seq 1 20); do
    umbriel outputs --json >/dev/null 2>&1 && break
    sleep 0.5
done

while IFS=$'\t' read -r name mode x y scale transform enabled; do
    [[ -n "$name" ]] || continue
    if [[ "$enabled" == "true" ]]; then
        umbriel msg output-enable "$name" 2>/dev/null
        mode_arg="${mode}"
        [[ "$mode_arg" == *"Hz" ]] || mode_arg="${mode_arg}Hz"
        wlr-randr --output "$name" \
            --mode "$mode_arg" \
            --pos "${x},${y}" \
            --scale "$scale" \
            --transform "$transform" 2>/dev/null
    else
        umbriel msg output-disable "$name" 2>/dev/null
    fi
done < <(jq -r '.outputs | to_entries[] |
    [.key, .value.mode, .value.x, .value.y, (.value.scale|tostring),
     .value.transform, (.value.enabled|tostring)] | @tsv' "$LAYOUT" 2>/dev/null)
