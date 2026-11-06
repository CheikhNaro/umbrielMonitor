#!/usr/bin/env python3
# umbriel_monitor_gui.py — visual monitor manager for Umbriel
#
# Features:
#   - Live load via `umbriel outputs --json`
#   - Drag & drop with magnetic snap and force-contact
#   - Mode (resolution@Hz) selector per screen
#   - Scale selector (0.5 → 3.0)
#   - Transform selector (normal, 90, 180, 270, flipped...)
#   - Live apply via wlr-randr + native output-enable/disable
#   - Persists the layout to state/monitor-layout.json (unwatched), re-applied
#     at session start by apply-layout.sh. Umbriel watches its config files and
#     any save that changes outputs re-creates them, which segfaults Noctalia;
#     this tool therefore never writes a watched config file.

import json
import os
import re
import subprocess
import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gtk, Gdk, GLib
import cairo

SCALE = 0.1
OUTPUT_FILE  = os.path.expanduser("~/.config/umbriel/modules/monitors.toml")
STATE_FILE   = os.path.expanduser("~/.config/umbriel/state/monitor-mode")
LAYOUT_FILE  = os.path.expanduser("~/.config/umbriel/state/monitor-layout.json")

COLORS = [
    (0.23, 0.51, 0.96),
    (0.06, 0.73, 0.51),
    (0.96, 0.62, 0.04),
    (0.55, 0.36, 0.96),
    (0.93, 0.28, 0.60),
]

TRANSFORMS = [
    "normal", "90", "180", "270",
    "flipped", "flipped-90", "flipped-180", "flipped-270",
]

SCALE_PRESETS = [0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0]


def hz_str(m):
    hz = m["refresh_mhz"] / 1000
    return f"{hz:.0f}" if hz == int(hz) else f"{hz:.3f}".rstrip("0")


def fmt_mode(m):
    label = f"{m['width']}x{m['height']}@{hz_str(m)}Hz"
    tags = []
    if m.get("preferred"):
        tags.append("★")
    if m.get("current"):
        tags.append("current")
    if tags:
        label += f" [{', '.join(tags)}]"
    return label


def parse_existing_workspaces():
    ws_map = {}
    if not os.path.exists(OUTPUT_FILE):
        return ws_map
    try:
        current_output = None
        with open(OUTPUT_FILE) as f:
            for line in f:
                line = line.strip()
                m = re.match(r"^\[output\.(.+)\]$", line)
                if m:
                    current_output = m.group(1)
                    continue
                if current_output and line.startswith("workspaces"):
                    m2 = re.search(r"\[([^\]]+)\]", line)
                    if m2:
                        names = [x.strip().strip('"').strip("'")
                                 for x in m2.group(1).split(",") if x.strip()]
                        ws_map[current_output] = names
    except Exception:
        pass
    return ws_map


class UmbrielMonitorGUI(Gtk.Window):
    def __init__(self):
        GLib.set_prgname("umbriel-monitor-gui")
        super().__init__(title="Monitor Positioning — Umbriel")
        self.set_default_size(900, 580)
        self.connect("destroy", Gtk.main_quit)

        self.monitors = []
        self.dragged_mon = None
        self.offset_x = None
        self.offset_y = None
        self.grab_dx = 0.0
        self.grab_dy = 0.0

        self._build_ui()
        self._load_monitors()

    def _build_ui(self):
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.add(vbox)

        header = Gtk.HeaderBar()
        header.set_show_close_button(True)
        header.props.title = "Umbriel — Monitor Positioning"
        self.set_titlebar(header)

        reload_btn = Gtk.Button()
        reload_btn.set_tooltip_text("Reload from umbriel")
        reload_btn.add(Gtk.Image.new_from_icon_name("view-refresh-symbolic", Gtk.IconSize.BUTTON))
        reload_btn.connect("clicked", lambda _: self._load_monitors())
        header.pack_end(reload_btn)

        apply_btn = Gtk.Button(label="Apply")
        apply_btn.get_style_context().add_class("suggested-action")
        apply_btn.connect("clicked", self.on_apply)
        header.pack_end(apply_btn)

        self.area = Gtk.DrawingArea()
        self.area.set_hexpand(True)
        self.area.set_vexpand(True)
        self.area.add_events(
            Gdk.EventMask.BUTTON_PRESS_MASK
            | Gdk.EventMask.BUTTON_RELEASE_MASK
            | Gdk.EventMask.POINTER_MOTION_MASK
        )
        self.area.connect("draw", self.on_draw)
        self.area.connect("button-press-event", self.on_button_press)
        self.area.connect("motion-notify-event", self.on_motion_notify)
        self.area.connect("button-release-event", self.on_button_release)
        vbox.pack_start(self.area, True, True, 0)

        sep = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        vbox.pack_start(sep, False, False, 0)

        self.bottom_stack = Gtk.Stack()
        self.bottom_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        vbox.pack_start(self.bottom_stack, False, False, 0)

        placeholder = Gtk.Label(label="⬆ Click a screen to show its options")
        placeholder.set_margin_top(12)
        placeholder.set_margin_bottom(12)
        self.bottom_stack.add_named(placeholder, "placeholder")

        self.status = Gtk.Label(label="")
        self.status.set_halign(Gtk.Align.START)
        self.status.set_margin_start(12)
        self.status.set_margin_top(4)
        self.status.set_margin_bottom(4)
        vbox.pack_start(self.status, False, False, 0)

    def _make_controls_for(self, mon):
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        box.set_margin_start(12)
        box.set_margin_end(12)
        box.set_margin_top(6)
        box.set_margin_bottom(6)

        lbl = Gtk.Label()
        lbl.set_markup(f"<b>{mon['name']}</b>")
        lbl.set_width_chars(10)
        lbl.set_xalign(0.0)
        box.pack_start(lbl, False, False, 0)

        # Mode
        box.pack_start(Gtk.Label(label="Mode:"), False, False, 0)
        mode_store = Gtk.ListStore(str, str)
        mode_combo = Gtk.ComboBox.new_with_model(mode_store)
        r = Gtk.CellRendererText()
        r.set_property("ellipsize", 3)  # PANGO_ELLIPSIZE_END
        mode_combo.pack_start(r, True)
        mode_combo.add_attribute(r, "text", 0)

        current_idx = 0
        for i, m in enumerate(mon.get("modes", [])):
            raw = f"{m['width']}x{m['height']}@{hz_str(m)}"
            mode_store.append([fmt_mode(m), raw])
            if raw == mon.get("best_mode", ""):
                current_idx = i
        mode_combo.set_active(current_idx)

        def on_mode_changed(combo, _mon=mon, _store=mode_store):
            it = combo.get_active_iter()
            if it:
                raw = _store[it][1]
                _mon["best_mode"] = raw
                parts = raw.split("@")[0].split("x")
                if len(parts) == 2:
                    _mon["display_w"] = int(parts[0])
                    _mon["display_h"] = int(parts[1])
                self.area.queue_draw()

        mode_combo.connect("changed", on_mode_changed)
        box.pack_start(mode_combo, True, True, 0)

        # Scale
        box.pack_start(Gtk.Label(label="Scale:"), False, False, 0)
        scale_store = Gtk.ListStore(str, float)
        scale_combo = Gtk.ComboBox.new_with_model(scale_store)
        r2 = Gtk.CellRendererText()
        scale_combo.pack_start(r2, True)
        scale_combo.add_attribute(r2, "text", 0)

        current_scale = float(mon.get("scale", 1.0))
        sel_scale_idx = 2
        for i, s in enumerate(SCALE_PRESETS):
            lbl_s = f"×{s:.2f}".rstrip("0").rstrip(".")
            scale_store.append([lbl_s, s])
            if abs(s - current_scale) < 0.01:
                sel_scale_idx = i
        scale_combo.set_active(sel_scale_idx)

        def on_scale_changed(combo, _mon=mon, _store=scale_store):
            it = combo.get_active_iter()
            if it:
                _mon["scale"] = _store[it][1]

        scale_combo.connect("changed", on_scale_changed)
        box.pack_start(scale_combo, False, False, 0)

        # Transform
        box.pack_start(Gtk.Label(label="Rotation:"), False, False, 0)
        tf_store = Gtk.ListStore(str)
        tf_combo = Gtk.ComboBox.new_with_model(tf_store)
        r3 = Gtk.CellRendererText()
        tf_combo.pack_start(r3, True)
        tf_combo.add_attribute(r3, "text", 0)

        current_tf = mon.get("transform", "normal")
        sel_tf_idx = 0
        for i, tf in enumerate(TRANSFORMS):
            tf_store.append([tf])
            if tf == current_tf:
                sel_tf_idx = i
        tf_combo.set_active(sel_tf_idx)

        def on_tf_changed(combo, _mon=mon):
            _mon["transform"] = TRANSFORMS[combo.get_active()]

        tf_combo.connect("changed", on_tf_changed)
        box.pack_start(tf_combo, False, False, 0)

        # Enabled switch (centered, not stretched)
        box.pack_start(Gtk.Label(label="Enabled:"), False, False, 0)
        enabled_sw = Gtk.Switch()
        enabled_sw.set_valign(Gtk.Align.CENTER)
        enabled_sw.set_halign(Gtk.Align.CENTER)
        enabled_sw.set_active(mon.get("enabled", True))

        def on_enabled(sw, _, _mon=mon):
            _mon["enabled"] = sw.get_active()
            self.area.queue_draw()

        enabled_sw.connect("notify::active", on_enabled)
        box.pack_start(enabled_sw, False, False, 0)

        return box

    def _load_monitors(self):
        try:
            raw = subprocess.check_output(["umbriel", "outputs", "--json"], text=True)
            data = json.loads(raw)
        except Exception as e:
            self._show_error(f"Could not read Umbriel outputs:\n{e}")
            return

        existing_ws = parse_existing_workspaces()

        self.monitors = []
        for i, m in enumerate(data):
            best = self._best_mode(m)
            bw = int(best.split("@")[0].split("x")[0])
            bh = int(best.split("@")[0].split("x")[1])
            mon = {
                "name":      m["name"],
                "display_w": bw,
                "display_h": bh,
                "x":         m["position"]["x"],
                "y":         m["position"]["y"],
                "scale":     float(m.get("scale", 1.0)),
                "transform": m.get("transform", "normal"),
                "enabled":   m.get("enabled", True),
                "color":     COLORS[i % len(COLORS)],
                "best_mode": best,
                "modes":     m.get("modes", []),
                "workspaces": existing_ws.get(m["name"], []),
            }
            self.monitors.append(mon)

        for child in list(self.bottom_stack.get_children()):
            name = self.bottom_stack.child_get_property(child, "name")
            if name and name.startswith("mon-"):
                self.bottom_stack.remove(child)

        for mon in self.monitors:
            ctrl = self._make_controls_for(mon)
            ctrl.show_all()
            self.bottom_stack.add_named(ctrl, f"mon-{mon['name']}")

        self.bottom_stack.set_visible_child_name("placeholder")
        self.offset_x = None
        self.offset_y = None
        self.area.queue_draw()
        self.status.set_text(f"{len(self.monitors)} monitor(s) detected.")

    @staticmethod
    def _best_mode(m):
        modes = m.get("modes", [])
        best = max(modes, key=lambda x: (x["width"] * x["height"], x["refresh_mhz"]), default=None)
        if best:
            return f"{best['width']}x{best['height']}@{hz_str(best)}"
        return "1920x1080@60"

    def _mon_w(self, mon):
        return mon["display_w"]

    def _mon_h(self, mon):
        return mon["display_h"]

    def _update_offset(self):
        if self.dragged_mon is not None and self.offset_x is not None:
            return
        alloc = self.area.get_allocation()
        visible = [m for m in self.monitors if m.get("enabled", True)]
        if not visible:
            self.offset_x = alloc.width / 2
            self.offset_y = alloc.height / 2
            return
        min_x = min(m["x"] for m in visible)
        min_y = min(m["y"] for m in visible)
        max_x = max(m["x"] + self._mon_w(m) for m in visible)
        max_y = max(m["y"] + self._mon_h(m) for m in visible)
        total_w = (max_x - min_x) * SCALE
        total_h = (max_y - min_y) * SCALE
        self.offset_x = (alloc.width  - total_w) / 2 - min_x * SCALE
        self.offset_y = (alloc.height - total_h) / 2 - min_y * SCALE

    def _offset(self):
        self._update_offset()
        return self.offset_x, self.offset_y

    def on_draw(self, area, cr):
        cr.set_source_rgb(0.10, 0.10, 0.12)
        cr.paint()
        ox, oy = self._offset()

        for mon in self.monitors:
            enabled = mon.get("enabled", True)
            rx = ox + mon["x"] * SCALE
            ry = oy + mon["y"] * SCALE
            rw = self._mon_w(mon) * SCALE
            rh = self._mon_h(mon) * SCALE

            r, g, b = mon["color"]
            alpha = 0.85 if enabled else 0.30
            cr.set_source_rgba(r, g, b, alpha)
            cr.rectangle(rx, ry, rw, rh)
            cr.fill_preserve()

            if mon is self.dragged_mon:
                cr.set_source_rgb(1, 1, 0.4)
                cr.set_line_width(3)
            else:
                cr.set_source_rgba(1, 1, 1, 0.6 if enabled else 0.2)
                cr.set_line_width(1.5)
            cr.stroke()

            if not enabled:
                cr.set_source_rgba(0, 0, 0, 0.35)
                cr.rectangle(rx, ry, rw, rh)
                cr.fill()

            cr.set_source_rgba(1, 1, 1, 1.0 if enabled else 0.4)
            cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
            cr.set_font_size(13)

            mode_short = mon["best_mode"].split("@")[0]
            lines = [mon["name"], mode_short, f"({mon['x']}, {mon['y']})"]
            if not enabled:
                lines.append("[disabled]")

            total_h_txt = len(lines) * 17
            start_y = ry + (rh - total_h_txt) / 2 + 13
            for line in lines:
                extents = cr.text_extents(line)
                tx = rx + (rw - extents.width) / 2
                cr.move_to(tx, start_y)
                cr.show_text(line)
                start_y += 17

    def on_button_press(self, area, event):
        if event.button != 1:
            return
        ox, oy = self._offset()
        for mon in reversed(self.monitors):
            if not mon.get("enabled", True):
                continue
            rx = ox + mon["x"] * SCALE
            ry = oy + mon["y"] * SCALE
            rw = self._mon_w(mon) * SCALE
            rh = self._mon_h(mon) * SCALE
            if rx <= event.x <= rx + rw and ry <= event.y <= ry + rh:
                self.dragged_mon = mon
                self.grab_dx = event.x - rx
                self.grab_dy = event.y - ry
                self.bottom_stack.set_visible_child_name(f"mon-{mon['name']}")
                self.area.queue_draw()
                break

    def on_motion_notify(self, area, event):
        if not self.dragged_mon:
            return
        ox, oy = self._offset()
        self.dragged_mon["x"] = int(round((event.x - ox - self.grab_dx) / SCALE))
        self.dragged_mon["y"] = int(round((event.y - oy - self.grab_dy) / SCALE))
        self._snap_monitor(self.dragged_mon)
        self.area.queue_draw()

    def on_button_release(self, area, event):
        if event.button != 1 or not self.dragged_mon:
            return
        self._snap_monitor(self.dragged_mon, snap_dist=100)
        self._force_contact(self.dragged_mon)
        self.dragged_mon = None
        self.area.queue_draw()

    def _snap_monitor(self, mon, snap_dist=60):
        w, h = self._mon_w(mon), self._mon_h(mon)
        for other in self.monitors:
            if other["name"] == mon["name"] or not other.get("enabled", True):
                continue
            ow, oh = self._mon_w(other), self._mon_h(other)
            if abs(mon["x"] - (other["x"] + ow)) < snap_dist:
                mon["x"] = other["x"] + ow
            elif abs((mon["x"] + w) - other["x"]) < snap_dist:
                mon["x"] = other["x"] - w
            elif abs(mon["x"] - other["x"]) < snap_dist:
                mon["x"] = other["x"]
            if abs(mon["y"] - (other["y"] + oh)) < snap_dist:
                mon["y"] = other["y"] + oh
            elif abs((mon["y"] + h) - other["y"]) < snap_dist:
                mon["y"] = other["y"] - h
            elif abs(mon["y"] - other["y"]) < snap_dist:
                mon["y"] = other["y"]

    def _force_contact(self, mon):
        others = [o for o in self.monitors
                  if o["name"] != mon["name"] and o.get("enabled", True)]
        if not others:
            return
        w, h = self._mon_w(mon), self._mon_h(mon)

        def touching(o):
            ow, oh = self._mon_w(o), self._mon_h(o)
            x_ov = mon["x"] < o["x"] + ow and mon["x"] + w > o["x"]
            y_ov = mon["y"] < o["y"] + oh and mon["y"] + h > o["y"]
            x_t = (abs(mon["x"] - (o["x"] + ow)) <= 2
                   or abs((mon["x"] + w) - o["x"]) <= 2)
            y_t = (abs(mon["y"] - (o["y"] + oh)) <= 2
                   or abs((mon["y"] + h) - o["y"]) <= 2)
            return (x_ov and y_t) or (y_ov and x_t)

        if any(touching(o) for o in others):
            return

        best = None
        for o in others:
            ow, oh = self._mon_w(o), self._mon_h(o)
            for nx in (o["x"] + ow, o["x"] - w):
                y = mon["y"]
                cost = abs(nx - mon["x"])
                if not (y < o["y"] + oh and y + h > o["y"]):
                    y_top = o["y"]
                    y_bot = o["y"] + oh - h
                    y = y_top if abs(y - y_top) <= abs(y - y_bot) else y_bot
                    cost += abs(y - mon["y"])
                if best is None or cost < best[0]:
                    best = (cost, nx, y)
            for ny in (o["y"] + oh, o["y"] - h):
                x = mon["x"]
                cost = abs(ny - mon["y"])
                if not (x < o["x"] + ow and x + w > o["x"]):
                    x_left = o["x"]
                    x_right = o["x"] + ow - w
                    x = x_left if abs(x - x_left) <= abs(x - x_right) else x_right
                    cost += abs(x - mon["x"])
                if best is None or cost < best[0]:
                    best = (cost, x, ny)

        if best:
            mon["x"] = int(round(best[1]))
            mon["y"] = int(round(best[2]))

    def on_apply(self, _widget):
        current_mode = self._read_state_mode()
        if current_mode == "mirror":
            self._show_info(
                "Mirror mode active",
                "Switch back to Extended mode first (Mod+P)\n"
                "to rearrange monitors, then reopen this GUI.",
            )
            return

        # 1. In-place live application: wlr-randr resizes/positions outputs
        #    without destroying the wl_output globals, so shells survive.
        live_errors = self._apply_live()

        # 2. Persist to an UNWATCHED state file, applied at the next session
        #    start by apply-layout.sh. Umbriel watches its config files and
        #    any save that touches outputs re-creates them, which segfaults
        #    Noctalia (negative viewport sizes during teardown — upstream
        #    bug). Writing no watched file is the only crash-free persistence.
        self._save_layout()

        if live_errors:
            self.status.set_text("⚠ " + "; ".join(live_errors))
        else:
            self.status.set_text("✓ Applied live; layout saved for next session.")

    def _save_layout(self):
        layout = {}
        for mon in self.monitors:
            layout[mon["name"]] = {
                "mode":      mon["best_mode"],
                "x":         mon["x"],
                "y":         mon["y"],
                "scale":     mon["scale"],
                "transform": mon.get("transform", "normal"),
                "enabled":   bool(mon.get("enabled", True)),
            }
        os.makedirs(os.path.dirname(LAYOUT_FILE), exist_ok=True)
        with open(LAYOUT_FILE, "w") as f:
            json.dump({"outputs": layout}, f, indent=2)

    def _apply_live(self):
        """Reconfigure outputs in place with wlr-randr (and native
        enable/disable). Never `umbriel msg config-reload` here: reloading the
        config destroys and recreates the outputs, and Noctalia segfaults on
        the transient negative viewport sizes of that teardown."""
        errors = []
        for mon in self.monitors:
            name = mon["name"]
            want = bool(mon.get("enabled", True))
            live = self._live_enabled(name)
            if want and not live:
                r = subprocess.run(["umbriel", "msg", "output-enable", name],
                                   capture_output=True, text=True, timeout=5)
                if r.returncode != 0:
                    errors.append(f"{name}: enable failed")
                    continue
            elif not want:
                if live:
                    subprocess.run(["umbriel", "msg", "output-disable", name],
                                   capture_output=True, text=True, timeout=5)
                continue

            cmd = ["wlr-randr", "--output", name,
                   "--mode", mon["best_mode"] + "Hz",
                   "--pos", f"{mon['x']},{mon['y']}",
                   "--scale", f"{mon['scale']:g}",
                   "--transform", mon.get("transform", "normal")]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
            if r.returncode != 0:
                errors.append(f"{name}: {r.stderr.strip().splitlines()[0] if r.stderr.strip() else 'wlr-randr failed'}")
        return errors

    @staticmethod
    def _live_enabled(name):
        try:
            raw = subprocess.check_output(["umbriel", "outputs", "--json"], text=True)
            for m in json.loads(raw):
                if m["name"] == name:
                    return bool(m.get("enabled", True))
        except Exception:
            pass
        return True

    def _read_state_mode(self):
        try:
            with open(STATE_FILE) as f:
                return f.read().strip()
        except Exception:
            return "extended"

    def _show_error(self, msg):
        d = Gtk.MessageDialog(
            transient_for=self, flags=0,
            message_type=Gtk.MessageType.ERROR,
            buttons=Gtk.ButtonsType.OK, text="Error",
        )
        d.format_secondary_text(msg)
        d.run()
        d.destroy()

    def _show_info(self, title, msg):
        d = Gtk.MessageDialog(
            transient_for=self, flags=0,
            message_type=Gtk.MessageType.INFO,
            buttons=Gtk.ButtonsType.OK, text=title,
        )
        d.format_secondary_text(msg)
        d.run()
        d.destroy()


if __name__ == "__main__":
    win = UmbrielMonitorGUI()
    win.show_all()
    Gtk.main()
