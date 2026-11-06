#!/usr/bin/env python3
"""Drag-and-drop monitor layout editor for Umbriel, Hyprland and niri.

Layouts are applied live through each compositor's runtime API, then stored in an
unwatched state file that apply-layout.sh replays at session start. No watched
config file is ever written, so applying a layout never triggers a config reload.
"""

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gtk, Gdk, GLib
import cairo

STATE_DIR = os.path.join(
    os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"),
    "umbriel-monitor",
)
LAYOUT_FILE = os.path.join(STATE_DIR, "monitor-layout.json")

SCALE = 0.1
COLORS = [
    (0.23, 0.51, 0.96),
    (0.06, 0.73, 0.51),
    (0.96, 0.62, 0.04),
    (0.55, 0.36, 0.96),
    (0.93, 0.28, 0.60),
]
TRANSFORMS = ["normal", "90", "180", "270", "flipped", "flipped-90", "flipped-180", "flipped-270"]
SCALE_PRESETS = [0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0]


def hz_str(mhz):
    hz = mhz / 1000
    return f"{hz:.0f}" if hz == int(hz) else f"{hz:.3f}".rstrip("0")


def mode_id(width, height, mhz):
    return f"{width}x{height}@{hz_str(mhz)}"


def parse_mode_id(text):
    m = re.match(r"^(\d+)x(\d+)@([\d.]+)$", str(text).strip())
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), round(float(m.group(3)) * 1000)


def transform_name(value):
    """Accepts wl_output transform integers and the canonical names.

    Checked against the names first: "90" is a rotation, not the index 90.
    """
    text = str(value).strip().strip('"') if value is not None else "normal"
    if text in TRANSFORMS:
        return text
    if text.isdigit():
        return TRANSFORMS[int(text) % 8]
    return "normal"


class Runner:
    """Reads always go through; writes are only printed when dry_run is set."""

    def __init__(self, dry_run=False):
        self.dry_run = dry_run

    def run(self, cmd):
        if self.dry_run:
            print("DRY " + " ".join(shlex.quote(c) for c in cmd))
            return 0, "", ""
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            return r.returncode, r.stdout, r.stderr
        except Exception as e:
            return 127, "", str(e)

    def json(self, cmd):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        except Exception as e:
            raise RuntimeError(str(e).strip() or f"{cmd[0]} failed")
        if r.returncode != 0:
            raise RuntimeError((r.stderr or r.stdout).strip().splitlines()[0] if (r.stderr or r.stdout).strip() else f"{cmd[0]} exit {r.returncode}")
        try:
            return json.loads(r.stdout)
        except ValueError:
            raise RuntimeError(f"{cmd[0]} returned no valid JSON")


class Backend:
    name = ""
    binary = ""

    def __init__(self, runner):
        self.runner = runner

    def installed(self):
        return shutil.which(self.binary) is not None

    def outputs(self):
        raise NotImplementedError

    def enable(self, name):
        raise NotImplementedError

    def disable(self, name):
        raise NotImplementedError

    def configure(self, mon):
        raise NotImplementedError

    @staticmethod
    def best_mode(modes):
        cur = next((m for m in modes if m["current"]), None)
        return cur or max(modes, key=lambda m: (m["width"] * m["height"], m["mhz"]), default=None)


class UmbrielBackend(Backend):
    """Umbriel: read natively, apply in place with wlr-randr (wlroots-based)."""

    name = "umbriel"
    binary = "umbriel"

    def outputs(self):
        data = self.runner.json(["umbriel", "outputs", "--json"])
        monitors = []
        for m in data:
            modes = [
                {
                    "width": x["width"],
                    "height": x["height"],
                    "mhz": int(x["refresh_mhz"]),
                    "preferred": bool(x.get("preferred")),
                    "current": bool(x.get("current")),
                }
                for x in m.get("modes", [])
            ]
            cur = self.best_mode(modes) or {"width": 1920, "height": 1080}
            monitors.append(
                {
                    "name": m["name"],
                    "x": int(m["position"]["x"]),
                    "y": int(m["position"]["y"]),
                    "width": cur["width"],
                    "height": cur["height"],
                    "scale": float(m.get("scale", 1.0)),
                    "transform": transform_name(m.get("transform")),
                    "enabled": bool(m.get("enabled", True)),
                    "modes": modes,
                }
            )
        return monitors

    def enable(self, name):
        return self.runner.run(["umbriel", "msg", "output-enable", name])

    def disable(self, name):
        return self.runner.run(["umbriel", "msg", "output-disable", name])

    def configure(self, mon):
        return [
            [
                "wlr-randr",
                "--output", mon["name"],
                "--mode", f"{mon['mode']}Hz",
                "--pos", f"{mon['x']},{mon['y']}",
                "--scale", f"{mon['scale']:g}",
                "--transform", mon["transform"],
            ]
        ]


class HyprlandBackend(Backend):
    """Hyprland: read hyprctl monitors -j, apply with hyprctl keyword monitor."""

    name = "hyprland"
    binary = "hyprctl"

    def outputs(self):
        data = self.runner.json(["hyprctl", "monitors", "-j"])
        monitors = []
        for m in data:
            modes = []
            for raw in m.get("availableModes", []):
                parsed = parse_mode_id(re.sub(r"Hz\s*$", "", str(raw).strip()))
                if parsed:
                    w, h, mhz = parsed
                    modes.append(
                        {"width": w, "height": h, "mhz": mhz, "preferred": False, "current": False}
                    )
            if not modes:
                modes = [
                    {
                        "width": int(m["width"]),
                        "height": int(m["height"]),
                        "mhz": round(float(m.get("refreshRate", 60.0)) * 1000),
                        "preferred": True,
                        "current": True,
                    }
                ]
            monitors.append(
                {
                    "name": m["name"],
                    "x": int(m["x"]),
                    "y": int(m["y"]),
                    "width": int(m["width"]),
                    "height": int(m["height"]),
                    "scale": float(m.get("scale", 1.0)),
                    "transform": transform_name(m.get("transform", 0)),
                    "enabled": not bool(m.get("disabled", False)),
                    "modes": modes,
                }
            )
        return monitors

    def keyword(self, spec):
        return self.runner.run(["hyprctl", "keyword", "monitor", spec])

    def enable(self, name):
        return self.keyword(f"{name},preferred,auto,1")

    def disable(self, name):
        return self.keyword(f"{name},disable")

    def configure(self, mon):
        tf = TRANSFORMS.index(mon["transform"])
        spec = (
            f"{mon['name']},{mon['mode']},{mon['x']}x{mon['y']},"
            f"{mon['scale']:g},transform {tf}"
        )
        return [["hyprctl", "keyword", "monitor", spec]]


class NiriBackend(Backend):
    """niri: read niri msg --json outputs, apply transient niri msg output actions."""

    name = "niri"
    binary = "niri"

    def outputs(self):
        data = self.runner.json(["niri", "msg", "--json", "outputs"])
        monitors = []
        for m in data:
            cur_idx = m.get("current_mode")
            modes = [
                {
                    "width": x["width"],
                    "height": x["height"],
                    "mhz": int(x["refresh_rate"]),
                    "preferred": bool(x.get("is_preferred")),
                    "current": i == cur_idx,
                }
                for i, x in enumerate(m.get("modes", []))
            ]
            cur = self.best_mode(modes) or {"width": 1920, "height": 1080}
            logical = m.get("logical") or {}
            monitors.append(
                {
                    "name": m["name"],
                    "x": int(logical.get("x", 0)),
                    "y": int(logical.get("y", 0)),
                    "width": cur["width"],
                    "height": cur["height"],
                    "scale": float(logical.get("scale", 1.0)),
                    "transform": transform_name(logical.get("transform")),
                    "enabled": cur_idx is not None,
                    "modes": modes,
                }
            )
        return monitors

    def enable(self, name):
        return self.runner.run(["niri", "msg", "output", name, "on"])

    def disable(self, name):
        return self.runner.run(["niri", "msg", "output", name, "off"])

    def configure(self, mon):
        base = ["niri", "msg", "output", mon["name"]]
        return [
            base + ["mode", mon["mode"]],
            base + ["scale", f"{mon['scale']:g}"],
            base + ["transform", mon["transform"]],
            base + ["position", "set", str(mon["x"]), str(mon["y"])],
        ]


BACKENDS = [UmbrielBackend, HyprlandBackend, NiriBackend]


def detect(runner, forced=None):
    """First backend that answers, with its outputs already read.

    Reading is the real test: a compositor binary can be installed without the
    session running it.
    """
    candidates = BACKENDS
    if forced:
        candidates = [b for b in BACKENDS if b.name == forced]
        if not candidates:
            raise SystemExit(
                f"unknown backend '{forced}' (choose: "
                + ", ".join(b.name for b in BACKENDS)
                + ")"
            )
    failures = []
    for cls in candidates:
        backend = cls(runner)
        if not backend.installed():
            failures.append(f"{cls.name}: '{backend.binary}' not found")
            continue
        try:
            return backend, normalize(backend.outputs())
        except Exception as e:
            failures.append(f"{cls.name}: {e}")
    if forced:
        raise SystemExit(failures[0] if failures else f"backend '{forced}' unavailable")
    raise SystemExit("no supported compositor answered — " + "; ".join(failures))


def normalize(monitors):
    for i, mon in enumerate(monitors):
        mon["modes"].sort(key=lambda m: (-m["width"] * m["height"], -m["mhz"]))
        cur = Backend.best_mode(mon["modes"])
        mon["mode"] = mode_id(cur["width"], cur["height"], cur["mhz"]) if cur else "1920x1080@60"
        mon["color"] = COLORS[i % len(COLORS)]
    return monitors


def apply_monitors(backend, monitors):
    errors = []
    for mon in monitors:
        name = mon["name"]
        if not mon["enabled"]:
            code, _, err = backend.disable(name)
            if code != 0:
                errors.append(f"{name}: {err.strip().splitlines()[0] if err.strip() else 'disable failed'}")
            continue

        code, _, err = backend.enable(name)
        if code != 0:
            errors.append(f"{name}: {err.strip().splitlines()[0] if err.strip() else 'enable failed'}")
            continue

        for cmd in backend.configure(mon):
            code, _, err = backend.runner.run(cmd)
            if code != 0:
                errors.append(f"{name}: {err.strip().splitlines()[0] if err.strip() else 'failed'}")
    return errors


def layout_from_monitors(backend_name, monitors):
    return {
        "backend": backend_name,
        "outputs": {
            m["name"]: {
                "mode": m["mode"],
                "x": m["x"],
                "y": m["y"],
                "scale": m["scale"],
                "transform": m["transform"],
                "enabled": m["enabled"],
            }
            for m in monitors
        },
    }


def read_saved_layout():
    try:
        with open(LAYOUT_FILE) as f:
            return json.load(f).get("outputs", {})
    except Exception:
        return {}



class MonitorGUI(Gtk.Window):
    def __init__(self, backend, monitors):
        GLib.set_prgname("umbriel-monitor")
        super().__init__(title="Monitor Positioning")
        self.set_wmclass("umbriel-monitor", "umbriel-monitor")
        self.set_default_size(900, 580)
        self.connect("destroy", Gtk.main_quit)

        self.backend = backend
        self.monitors = monitors
        self.dragged_mon = None
        self.offset_x = None
        self.offset_y = None
        self.grab_dx = 0.0
        self.grab_dy = 0.0

        self._build_ui()
        self._build_controls()
        self.status.set_text(f"{len(self.monitors)} monitor(s) via {backend.name}.")

    def _build_ui(self):
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.add(vbox)

        header = Gtk.HeaderBar()
        header.set_show_close_button(True)
        header.props.title = f"Monitor Positioning — {self.backend.name}"
        self.set_titlebar(header)

        reload_btn = Gtk.Button()
        reload_btn.set_tooltip_text(f"Reload from {self.backend.name}")
        reload_btn.add(Gtk.Image.new_from_icon_name("view-refresh-symbolic", Gtk.IconSize.BUTTON))
        reload_btn.connect("clicked", self.on_reload)
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

        vbox.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 0)

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

    def _build_controls(self):
        for child in list(self.bottom_stack.get_children()):
            name = self.bottom_stack.child_get_property(child, "name")
            if name and name.startswith("mon-"):
                self.bottom_stack.remove(child)
        for mon in self.monitors:
            ctrl = self._make_controls_for(mon)
            ctrl.show_all()
            self.bottom_stack.add_named(ctrl, f"mon-{mon['name']}")
        self.bottom_stack.set_visible_child_name("placeholder")

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

        box.pack_start(Gtk.Label(label="Mode:"), False, False, 0)
        mode_store = Gtk.ListStore(str, str)
        mode_combo = Gtk.ComboBox.new_with_model(mode_store)
        renderer = Gtk.CellRendererText()
        renderer.set_property("ellipsize", 3)  # PANGO_ELLIPSIZE_END
        mode_combo.pack_start(renderer, True)
        mode_combo.add_attribute(renderer, "text", 0)

        current_idx = 0
        for i, m in enumerate(mon["modes"]):
            mid = mode_id(m["width"], m["height"], m["mhz"])
            tags = [t for t, on in (("★", m["preferred"]), ("current", mid == mon["mode"])) if on]
            mode_store.append([mid + (f" [{', '.join(tags)}]" if tags else ""), mid])
            if mid == mon["mode"]:
                current_idx = i
        mode_combo.set_active(current_idx)

        def on_mode_changed(combo, _mon=mon, _store=mode_store):
            it = combo.get_active_iter()
            if not it:
                return
            parsed = parse_mode_id(_store[it][1])
            if not parsed:
                return
            _mon["mode"] = _store[it][1]
            _mon["width"], _mon["height"] = parsed[0], parsed[1]
            self.area.queue_draw()

        mode_combo.connect("changed", on_mode_changed)
        box.pack_start(mode_combo, True, True, 0)

        box.pack_start(Gtk.Label(label="Scale:"), False, False, 0)
        scale_store = Gtk.ListStore(str, float)
        scale_combo = Gtk.ComboBox.new_with_model(scale_store)
        scale_renderer = Gtk.CellRendererText()
        scale_combo.pack_start(scale_renderer, True)
        scale_combo.add_attribute(scale_renderer, "text", 0)
        sel = 2
        for i, s in enumerate(SCALE_PRESETS):
            scale_store.append([f"×{s:g}", s])
            if abs(s - mon["scale"]) < 0.01:
                sel = i
        scale_combo.set_active(sel)

        def on_scale_changed(combo, _mon=mon, _store=scale_store):
            it = combo.get_active_iter()
            if it:
                _mon["scale"] = _store[it][1]

        scale_combo.connect("changed", on_scale_changed)
        box.pack_start(scale_combo, False, False, 0)

        box.pack_start(Gtk.Label(label="Rotation:"), False, False, 0)
        tf_store = Gtk.ListStore(str)
        tf_combo = Gtk.ComboBox.new_with_model(tf_store)
        tf_renderer = Gtk.CellRendererText()
        tf_combo.pack_start(tf_renderer, True)
        tf_combo.add_attribute(tf_renderer, "text", 0)
        sel_tf = 0
        for i, tf in enumerate(TRANSFORMS):
            tf_store.append([tf])
            if tf == mon["transform"]:
                sel_tf = i
        tf_combo.set_active(sel_tf)

        def on_tf_changed(combo, _mon=mon):
            _mon["transform"] = TRANSFORMS[combo.get_active()]

        tf_combo.connect("changed", on_tf_changed)
        box.pack_start(tf_combo, False, False, 0)

        box.pack_start(Gtk.Label(label="Enabled:"), False, False, 0)
        enabled_sw = Gtk.Switch()
        enabled_sw.set_valign(Gtk.Align.CENTER)
        enabled_sw.set_halign(Gtk.Align.CENTER)
        enabled_sw.set_active(mon["enabled"])

        def on_enabled(sw, _, _mon=mon):
            _mon["enabled"] = sw.get_active()
            self.area.queue_draw()

        enabled_sw.connect("notify::active", on_enabled)
        box.pack_start(enabled_sw, False, False, 0)
        return box

    def on_reload(self, _widget):
        try:
            self.monitors = normalize(self.backend.outputs())
        except Exception as e:
            self._dialog(Gtk.MessageType.ERROR, "Error", f"Could not read outputs:\n{e}")
            return
        self._build_controls()
        self.offset_x = self.offset_y = None
        self.area.queue_draw()
        self.status.set_text(f"{len(self.monitors)} monitor(s) via {self.backend.name}.")

    # ── geometry ─────────────────────────────────────────────────────────
    def _visible(self):
        return [m for m in self.monitors if m["enabled"]]

    def _offset(self):
        if self.dragged_mon is not None and self.offset_x is not None:
            return self.offset_x, self.offset_y
        alloc = self.area.get_allocation()
        visible = self._visible()
        if not visible:
            self.offset_x = alloc.width / 2
            self.offset_y = alloc.height / 2
            return self.offset_x, self.offset_y
        min_x = min(m["x"] for m in visible)
        min_y = min(m["y"] for m in visible)
        max_x = max(m["x"] + m["width"] for m in visible)
        max_y = max(m["y"] + m["height"] for m in visible)
        self.offset_x = (alloc.width - (max_x - min_x) * SCALE) / 2 - min_x * SCALE
        self.offset_y = (alloc.height - (max_y - min_y) * SCALE) / 2 - min_y * SCALE
        return self.offset_x, self.offset_y

    def on_draw(self, area, cr):
        cr.set_source_rgb(0.10, 0.10, 0.12)
        cr.paint()
        ox, oy = self._offset()

        for mon in self.monitors:
            rx = ox + mon["x"] * SCALE
            ry = oy + mon["y"] * SCALE
            rw = mon["width"] * SCALE
            rh = mon["height"] * SCALE
            on = mon["enabled"]

            r, g, b = mon["color"]
            cr.set_source_rgba(r, g, b, 0.85 if on else 0.30)
            cr.rectangle(rx, ry, rw, rh)
            cr.fill_preserve()

            if mon is self.dragged_mon:
                cr.set_source_rgb(1, 1, 0.4)
                cr.set_line_width(3)
            else:
                cr.set_source_rgba(1, 1, 1, 0.6 if on else 0.2)
                cr.set_line_width(1.5)
            cr.stroke()

            if not on:
                cr.set_source_rgba(0, 0, 0, 0.35)
                cr.rectangle(rx, ry, rw, rh)
                cr.fill()

            cr.set_source_rgba(1, 1, 1, 1.0 if on else 0.4)
            cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
            cr.set_font_size(13)
            lines = [mon["name"], mon["mode"].split("@")[0], f"({mon['x']}, {mon['y']})"]
            if not on:
                lines.append("[disabled]")
            y = ry + (rh - len(lines) * 17) / 2 + 13
            for line in lines:
                cr.move_to(rx + (rw - cr.text_extents(line).width) / 2, y)
                cr.show_text(line)
                y += 17

    # ── dragging ─────────────────────────────────────────────────────────
    def on_button_press(self, area, event):
        if event.button != 1:
            return
        ox, oy = self._offset()
        for mon in reversed(self.monitors):
            if not mon["enabled"]:
                continue
            rx = ox + mon["x"] * SCALE
            ry = oy + mon["y"] * SCALE
            if rx <= event.x <= rx + mon["width"] * SCALE and ry <= event.y <= ry + mon["height"] * SCALE:
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
        self._snap(self.dragged_mon)
        self.area.queue_draw()

    def on_button_release(self, area, event):
        if event.button != 1 or not self.dragged_mon:
            return
        self._snap(self.dragged_mon, snap_dist=100)
        self._force_contact(self.dragged_mon)
        self.dragged_mon = None
        self.area.queue_draw()

    def _snap(self, mon, snap_dist=60):
        w, h = mon["width"], mon["height"]
        for other in self.monitors:
            if other["name"] == mon["name"] or not other["enabled"]:
                continue
            ow, oh = other["width"], other["height"]
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
        """A released screen can never stay detached: it is glued to the nearest
        edge of the closest neighbour."""
        others = [o for o in self.monitors if o["name"] != mon["name"] and o["enabled"]]
        if not others:
            return
        w, h = mon["width"], mon["height"]

        def touching(o):
            x_ov = mon["x"] < o["x"] + o["width"] and mon["x"] + w > o["x"]
            y_ov = mon["y"] < o["y"] + o["height"] and mon["y"] + h > o["y"]
            x_t = abs(mon["x"] - (o["x"] + o["width"])) <= 2 or abs((mon["x"] + w) - o["x"]) <= 2
            y_t = abs(mon["y"] - (o["y"] + o["height"])) <= 2 or abs((mon["y"] + h) - o["y"]) <= 2
            return (x_ov and y_t) or (y_ov and x_t)

        if any(touching(o) for o in others):
            return

        best = None
        for o in others:
            ow, oh = o["width"], o["height"]
            for nx in (o["x"] + ow, o["x"] - w):
                y, cost = mon["y"], abs(nx - mon["x"])
                if not (y < o["y"] + oh and y + h > o["y"]):
                    top, bot = o["y"], o["y"] + oh - h
                    y = top if abs(y - top) <= abs(y - bot) else bot
                    cost += abs(y - mon["y"])
                if best is None or cost < best[0]:
                    best = (cost, nx, y)
            for ny in (o["y"] + oh, o["y"] - h):
                x, cost = mon["x"], abs(ny - mon["y"])
                if not (x < o["x"] + ow and x + w > o["x"]):
                    left, right = o["x"], o["x"] + ow - w
                    x = left if abs(x - left) <= abs(x - right) else right
                    cost += abs(x - mon["x"])
                if best is None or cost < best[0]:
                    best = (cost, x, ny)

        if best:
            mon["x"] = int(round(best[1]))
            mon["y"] = int(round(best[2]))

    # ── apply ────────────────────────────────────────────────────────────
    def on_apply(self, _widget):
        errors = apply_monitors(self.backend, self.monitors)
        if not self.backend.runner.dry_run:
            os.makedirs(STATE_DIR, exist_ok=True)
            with open(LAYOUT_FILE, "w") as f:
                json.dump(layout_from_monitors(self.backend.name, self.monitors), f, indent=2)
        if errors:
            self.status.set_text("⚠ " + "; ".join(errors))
        elif self.backend.runner.dry_run:
            self.status.set_text("Dry run: nothing applied.")
        else:
            self.status.set_text("✓ Applied live; layout saved for the next session.")

    def _dialog(self, kind, title, msg):
        d = Gtk.MessageDialog(
            transient_for=self, flags=0, message_type=kind,
            buttons=Gtk.ButtonsType.OK, text=title,
        )
        d.format_secondary_text(msg)
        d.run()
        d.destroy()


def main():
    ap = argparse.ArgumentParser(
        description="GTK monitor layout editor for Umbriel, Hyprland and niri"
    )
    ap.add_argument("--backend", choices=[b.name for b in BACKENDS], help="force one compositor backend")
    ap.add_argument("--dry-run", action="store_true", help="print compositor commands instead of running them")
    ap.add_argument("--list", action="store_true", help="print detected outputs as JSON and exit")
    ap.add_argument(
        "--preview-apply",
        action="store_true",
        help="re-apply the detected layout with --dry-run and exit (no window)",
    )
    args = ap.parse_args()

    runner = Runner(dry_run=args.dry_run)
    backend, monitors = detect(runner, args.backend)

    if args.list:
        print(json.dumps(layout_from_monitors(backend.name, monitors), indent=2))
        return 0

    if args.preview_apply:
        errors = apply_monitors(backend, monitors)
        for err in errors:
            print(f"error: {err}", file=sys.stderr)
        return 1 if errors else 0

    win = MonitorGUI(backend, monitors)
    win.show_all()
    Gtk.main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
