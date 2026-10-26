# assisted_screws — guided bed levelling for Klipper

A Klipper extra that walks you through levelling a printer with two Z motors and
bed screws:

1. **Mechanical gantry alignment**: the X axis is lowered onto two identical
   stops at reduced motor current, so both sides end at the same height.
2. **Assisted bed screws**: the probe sits over each screw and keeps measuring
   while you turn it, telling you which way and how much. After it reads within
   tolerance several times in a row, it moves on to the next screw.
3. **Final `Z_TILT_ADJUST`** to remove what is left (tenths of a millimetre).

While it runs, the printer reports itself as `calibrating`. It also counts
print hours and suggests a recalibration at boot after a configurable number
of hours.

## Requirements

- Klipper with a Z probe (`[probe]`, `[bltouch]`…) used as the Z endstop.
- `[screws_tilt_adjust]` configured. Its screw positions are where the
  **nozzle** goes so that the **probe** sits over each screw, and its thread
  sets the turn advice.
- Two Z motors named `stepper_z` (left) and `stepper_z1` (right), each with a
  `[tmc2209]` driver, for the stall alignment.
- `[force_move]` with `enable_force_move: True`.
- `[save_variables]` to remember print hours and the last calibration.
- `[z_tilt]` (optional), which is run at the end if present.
- Mainsail or Fluidd, for the on-screen prompts (`action:prompt`).
- Two identical stops (printed blocks or metal cylinders) that fit under the X
  axis ends next to the Z lead screws.

## Install

```bash
cd ~
git clone https://github.com/<you>/klipper-assisted-screws.git
bash ./klipper-assisted-screws/install.sh
```

The installer links `assisted_screws.py` into `klippy/extras` and adds an
`[update_manager assisted_screws]` entry to `moonraker.conf`, so updates show
up in Mainsail/Fluidd.

Copy `assisted_screws.cfg` into your config (or include it), set at least
`stop_height` and `stall_x`, then restart the Klipper **service**. A plain
`RESTART` does not reload Python code:

```bash
sudo systemctl restart klipper
```

Uninstall with `bash ./klipper-assisted-screws/install.sh -u`.

### Block printing while calibrating

Add this at the top of your `START_PRINT` (or `PRINT_START`) macro:

```
{% if printer.assisted_screws.state == 'calibrating' %}
  { action_raise_error("Bed calibration in progress") }
{% endif %}
```

## Use

Run `ASSISTED_BED_CALIBRATE` from the console (or add it as a macro button) and
follow the prompts:

| Step | What the printer does | What you do |
|---|---|---|
| Start | Homes, raises Z to `stops_z`, parks the nozzle at `stall_x` off the bed | Put the two stops under the X axis ends, then press **Stops inserted** |
| Stops in | Lowers the gantry (both Z motors together) at `stall_current`; the side that reaches its stop first skips steps until the other one arrives. Restores the current and raises `raise_after_stall` | **Remove both stops**, then press **Stops removed** |
| Levelling | Homes again. Probes the reference screw (screw 1, do not touch it). Then for each screw it probes every `interval` seconds and prints e.g. `Front right: +0.1200 mm -> adjust CCW 00:10` | Turn the screw as advised until it shows `OK (3/3)` |
| Check | Probes every screw against the reference; repeats the round up to `max_rounds` times | Keep adjusting if asked |
| Finish | Runs `Z_TILT_ADJUST`, saves the calibration, back to `ready` | Close the prompt |

The turn advice uses the same convention as Klipper's `SCREWS_TILT_CALCULATE`
(`CW 00:10` = clockwise, 10 minutes of a full turn). Check on the first screw
that the reading moves the right way.

### Individual commands

- `ASSISTED_SCREWS_LEVEL [TOLERANCE=] [CONFIRM=] [INTERVAL=] [TIMEOUT=]` runs
  one assisted screw round (axes must be homed).
- `ASSISTED_GANTRY_STALL [DISTANCE=] [CURRENT=] [VELOCITY=]` lowers both Z
  motors onto the stops and releases the motors.
- `ASSISTED_GANTRY_CHECK [HEIGHT=]` raises the gantry for a manual measurement.
  `ASSISTED_GANTRY_CHECK LEFT=<mm> RIGHT=<mm> [APPLY=1]` reports the difference
  and, with `APPLY=1`, lowers the high side by it.

### Status

`printer.assisted_screws` exposes `state` (`ready`/`calibrating`),
`print_hours`, `calibrated_at` (Unix time), `hours_since` and
`recalibrate_hours`.

## Safety

- Before every assisted round, each screw position is checked at a safe nozzle
  height (`check_z`). If the probe cannot see the bed there, the command stops
  instead of letting `PROBE` drive the nozzle down to the Z limit.
- Keep the nozzle clean: a blob can touch the bed before the probe triggers.
- Stay at the printer during the stall step, and start with a low
  `stall_current`.

## License

GPLv3, like Klipper.
