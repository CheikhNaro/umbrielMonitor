# umbriel-monitor-gui

A visual monitor manager for [Umbriel](https://github.com/noctalia-dev/umbriel) + [Noctalia](https://github.com/noctalia-dev/noctalia), with partial support for [Hyprland](https://hyprland.org).


https://github.com/user-attachments/assets/9716865e-9f09-4c37-9509-d4a1aad56605


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

## Installation — Umbriel + Noctalia

```bash
mkdir -p ~/.config/umbriel/scripts ~/.config/umbriel/state
cp umbriel_monitor_gui.py  ~/.config/umbriel/scripts/
cp apply-layout.sh         ~/.config/umbriel/scripts/
chmod +x ~/.config/umbriel/scripts/umbriel_monitor_gui.py
chmod +x ~/.config/umbriel/scripts/apply-layout.sh
```

Add to your Umbriel autostart in `~/.config/umbriel/config.toml`:

```toml
spawn ~/.config/umbriel/scripts/apply-layout.sh
```

---

## Installation — Hyprland

> [!NOTE]
> The GUI calls `umbriel outputs --json` and `umbriel msg output-enable/disable` — these must be replaced. The layout file and `wlr-randr` work as-is on any wlroots compositor.

**Adapt `umbriel_monitor_gui.py`** — replace the Umbriel-specific calls:

| Original (Umbriel) | Hyprland equivalent |
|--------------------|---------------------|
| `umbriel outputs --json` | `hyprctl monitors -j` |
| `umbriel msg output-enable <name>` | `wlr-randr --output <name> --on` |
| `umbriel msg output-disable <name>` | `wlr-randr --output <name> --off` |

> [!IMPORTANT]
> `hyprctl monitors -j` has a different JSON schema. You will also need to adapt `_load_monitors()` to map Hyprland field names (`width`, `height`, `refreshRate`, `x`, `y`, `scale`, `transform`, `disabled`).

**Adapt `apply-layout.sh`** — replace:

```bash
# Instead of: umbriel outputs --json > /dev/null 2>&1 || exit 0
hyprctl monitors -j > /dev/null 2>&1 || exit 0

# Instead of: umbriel msg output-enable "$name"
wlr-randr --output "$name" --on

# Instead of: umbriel msg output-disable "$name"
wlr-randr --output "$name" --off
```

Add to your Hyprland autostart:

```
exec-once = ~/.config/umbriel/scripts/apply-layout.sh
```

---

## Usage

```bash
python3 ~/.config/umbriel/scripts/umbriel_monitor_gui.py
```

**Umbriel** — bind it to a key in `~/.config/umbriel/config.toml`:

```toml
[[keybind]]
key = "Super+shift+m"
action = "spawn python3 ~/.config/umbriel/scripts/umbriel_monitor_gui.py"
```

**Hyprland** — bind it in your Hyprland config:

```
bind = SUPER SHIFT, M, exec, python3 ~/.config/umbriel/scripts/umbriel_monitor_gui.py
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

---

## ⚠ Known limitation — do not change scale

**Do not modify the scale of any output in this GUI.**

Changing the scale triggers an Umbriel output teardown/recreate cycle that causes Noctalia to crash (negative viewport size — upstream Umbriel bug, fix pending).

If Noctalia stops after an accidental scale change, restart it manually:

```bash
noctalia & disown
```

**This GUI is intended for monitor positioning only** (arrangement, mode, rotation, enable/disable). Leave all scale values as-is.
