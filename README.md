# lowerdeck

Touch UI for the second screen of dual-screen handhelds.

A 3DS Virtual Console-inspired companion panel for RetroArch on devices
with a secondary touchscreen (e.g. Anbernic RG DS, AYANEO Pocket DS,
Retroid Pocket 5/Mini/Flip2). 

## Features

<img width="1240" height="1080" alt="image" src="https://github.com/user-attachments/assets/8b8d4eeb-ed81-49de-b700-e8f4aa247468" />

- **VC-style panel** — Resume Game, Create Restore Point, Load Restore
  Point with slot picker and live savestate thumbnails. Touches are
  forwarded to RetroArch via the Network Control Interface.

<img width="1240" height="1080" alt="image" src="https://github.com/user-attachments/assets/779492f6-6da5-4a55-ae79-f85deaba2611" />
  
- **RetroAchievements browser** — scrollable list with badges, points,
  unlock dates, and rarity. Game icon, score, and progress
  appear in the header.
 

## Architecture

```
+-------------------+        UDP 55355         +---------------+
|   RetroArch       |  <----------------->     |   main.py     |
|  (game + cheevos) |  network commands        | (SDL UI)      |
+-------------------+                          +---------------+
         |                                             ^
         |   HTTP /dorequest.php                       |  HTTP
         |   (cheevos_custom_host=127.0.0.1:8080)      |  /_ui/state
         v                                             |
+-------------------+        HTTPS             +---------------+
|   ra_proxy.py     |  --------------------->  | retroachieve- |
|  (local proxy)    |  forward + intercept     |   ments.org   |
+-------------------+                          +---------------+
```

`main.py` runs the SDL2 UI on the secondary display. `ra_proxy.py` is a
small HTTP server launched separately by the host's run-emu script
*before* RetroArch starts; it self-exits 3 seconds after RetroArch
disappears.

## Requirements

- Python 3.10+
- `libSDL2`, `libSDL2_image`, `libSDL2_ttf` (loaded at runtime via ctypes)
- A TTF font (default path is configurable)
- RetroArch with `network_cmd_enable = "true"` and
  `cheevos_custom_host = "http://127.0.0.1:8080"`
- Linux with `/proc` and (optionally) `/sys/class/backlight` for
  brightness control

No `pip install` is required; the project ships pure stdlib + a hand-
rolled SDL2 ctypes binding.

## Repository layout

```
main.py               SDL UI entry point
ra_proxy.py           local RetroAchievements proxy (run alongside)
ra_state.py           HTTP state poller used by main.py
ra_client.py          RetroArch UDP network-cmd client
layout.py             pure-data geometry
brightness.py         /sys/class/backlight wrapper
achievements_view.py  scrollable cheevos list with marquee + inertia
sdl2.py               ctypes bindings for libSDL2 / SDL2_image / SDL2_ttf
config.json           defaults (theme, fonts, paths, proxy host/port)
assets/               icons, click.wav
```

## Configuration

`config.json` is loaded first; an optional `config.local.json` next
to it overrides keys.

| Key | Purpose |
|---|---|
| `font_path` | TTF font for all text |
| `backlight_root` | sysfs backlight directory (`/sys/class/backlight` on Linux) |
| `bottom_backlight_node` | per-device backlight node name |
| `audio_players` | list of CLI players tried for the click sound |
| `wayland_app_id` | window app_id (must match the compositor rule) |
| `ra_proxy.listen_host` / `listen_port` | local proxy bind |
| `ra_proxy.upstream` | RetroAchievements endpoint |
| `ra_proxy.badges_dir` | shared badge cache (use RA's own dir) |
| `ra_proxy.retroarch_process_match` | cmdline substring used to detect RA |

## Running

```bash
# 1. Start the proxy before RetroArch boots:
python3 ra_proxy.py &

# 2. Launch RetroArch with cheevos pointed at the proxy.

# 3. Launch the UI:
python3 main.py \
    --rom              "/path/to/rom.zip" \
    --core             mgba \
    --platform         gb \
    --ra-pid           "$(pgrep -f /usr/bin/retroarch | head -n1)" \
    --cheevos-enabled  true
```

`--cheevos-enabled` is a tri-state (`true` / `false` / omit). Pass the
host's per-game cheevos toggle so the UI can show a clear "disabled"
message when the user opted out for that ROM.

`ra_proxy.py` watches `/proc` for RetroArch and self-exits 3 seconds
after it disappears, so a single launch from your run-emu script
covers both startup and shutdown.

## Integrating with a host OS

Examples for ROCKNIX-style stacks (`runemu.sh` + sway `for_window`
hook) live under `os/`. Adapting to another distro is mostly a matter
of:

1. Spawning `ra_proxy.py` before RetroArch in your run-game script.
2. Adding `cheevos_custom_host = "http://127.0.0.1:8080"` to RA's
   appendconfig (or main `retroarch.cfg`).
3. Spawning `main.py` after RetroArch's window appears, with the four
   CLI args above.
4. Routing the window to the secondary display (e.g. via the
   compositor's `app_id` rule).

## License

GPL-2.0-or-later. See `LICENSE`.
