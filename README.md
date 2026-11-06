# umbriel-monitor-gui

A visual monitor manager for [Umbriel](https://github.com/noctalia-dev/umbriel) + [Noctalia](https://github.com/noctalia-dev/noctalia).

---

## What it does

- Displays all connected outputs in a drag-and-drop canvas
- Magnetic snap + force-contact: screens always stay adjacent
- Per-screen controls: resolution×refresh, scale, rotation, enable/disable
- **Apply** reconfigures outputs live via `wlr-randr` + native Umbriel IPC — no config file is rewritten, so Noctalia never segfaults
- Saves the layout to `~/.config/umbriel/state/monitor-layout.json` (unwatched), re-applied at the next session start by `apply-layout.sh`

> [!NOTE]
> Mirror mode (toggled with `Mod+P`) is managed separately by `toggle-monitor.sh`.  
> If mirror mode is active when you click **Apply**, the GUI will ask you to switch back to extended mode first.

---

## Dependencies

| Package | Why |
|---------|-----|
| `umbriel` | Compositor — provides `umbriel outputs --json` and IPC (`output-enable`, `output-disable`) |
| `noctalia` | Shell — the layout persistence strategy avoids the upstream segfault it triggers on output teardown |
| `python3` | Runtime |
| `python3-gobject` (PyGObject) | GTK3 bindings (`gi.repository.Gtk`, `Gdk`, `GLib`) |
| `python3-cairo` (pycairo) | Canvas rendering |
| `wlr-randr` | Live output reconfiguration without config reload |
| `jq` | JSON parsing in `apply-layout.sh` |

### Fedora / RPM

```bash
sudo dnf install python3-gobject python3-cairo wlr-randr jq
```

### Debian / Ubuntu

```bash
sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-3.0 python3-cairo wlr-randr jq
```

### Arch

```bash
sudo pacman -S python-gobject python-cairo wlr-randr jq
```

---

## Installation

```bash
# 1. Copy the scripts
mkdir -p ~/.config/umbriel/scripts ~/.config/umbriel/state
cp umbriel_monitor_gui.py  ~/.config/umbriel/scripts/
cp apply-layout.sh         ~/.config/umbriel/scripts/
chmod +x ~/.config/umbriel/scripts/umbriel_monitor_gui.py
chmod +x ~/.config/umbriel/scripts/apply-layout.sh

# 2. Auto-apply layout at session start — add to your Umbriel autostart
#    In ~/.config/umbriel/config.toml:
#    spawn ~/.config/umbriel/scripts/apply-layout.sh
```

---

## Usage

```bash
python3 ~/.config/umbriel/scripts/umbriel_monitor_gui.py
```

Or bind it to a key in `~/.config/umbriel/config.toml`:

```toml
[[keybind]]
key = "Super+shift+m"
action = "spawn python3 ~/.config/umbriel/scripts/umbriel_monitor_gui.py"
```

**Workflow:**

1. Drag screens to arrange them
2. Select mode, scale and rotation in the bottom bar
3. Click **Apply** — changes are live immediately
4. The layout is saved and will be restored automatically at the next login

---

## Files

| File | Role |
|------|------|
| `umbriel_monitor_gui.py` | GUI — drag & drop monitor positioning |
| `apply-layout.sh` | Session startup script — re-applies `monitor-layout.json` via `wlr-randr` |
| `~/.config/umbriel/state/monitor-layout.json` | Persisted layout (written by the GUI, read by `apply-layout.sh`) |
| `~/.config/umbriel/state/monitor-mode` | Current mode flag (`extended` / `mirror` / `single`) |
