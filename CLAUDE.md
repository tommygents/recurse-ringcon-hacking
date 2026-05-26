# CLAUDE.md — recurse-ringcon-hacking

Guidance for Claude Code (or any contributor) working in this repo.

## What this is

A from-scratch driver + games that turn Nintendo **Ring Fit** hardware into a
PC game controller. Two Joy-Cons, read over **Bluetooth HID** (not BLE):

- **Right Joy-Con** seats in the **Ring-Con** and exposes a strain gauge
  (squeeze/pull) plus an accelerometer.
- **Left Joy-Con** straps to the **thigh** and is read purely from its IMU to
  detect **running in place** and **squats** (à la Ring Fit Adventure).

## Architecture (the layering matters)

- **`monitor.py` — the driver + interpretation core (no game logic).**
  - HID transport over the Joy-Con MCU subcommand protocol, ported from
    [`ringrunnermg/Ringcon-Driver`](https://github.com/ringrunnermg/Ringcon-Driver):
    `init_ringcon()` powers on the MCU and starts strain polling (right pad);
    `init_imu_only()` is the lightweight standard-input + IMU path (left pad).
  - `find_joycon(side="R"|"L")` / `find_joycons()` — **always target by side**;
    with both pads connected, the unfiltered lookup is nondeterministic.
  - `parse_report()` returns `strain`, `accel`, `gyro` (frame-1 IMU offsets).
  - `LegTracker` — a **pure** class (no I/O): feed it `(accel, gyro)`, read
    `.state` (`rest`/`run`/`sprint`/`squat`), `.squat_reps`, `.tilt`, `.energy`.
    Squats = gravity-vector tilt from a calibrated rest pose (orientation-
    agnostic; counted only when the tilt *persists*). Run = rolling mean-|gyro|.
- **Games** (each owns its own input *interpretation*, built on `monitor.py`):
  `flappy.py` (squeeze→flap), `ski.py` (strain=speed, tilt=steer),
  `doom_ring.py` (Ring-Con + leg mod of the `doom/` submodule),
  `ringfit.py` (combined Ring-Con + leg live readout / diagnostics).
- **`doom/`** — git **submodule** ([`stanislavPetrovV/DOOM-style-game`](https://github.com/stanislavPetrovV/DOOM-style-game)).

## Conventions / gotchas (read before editing)

- **Never edit files inside `doom/`.** It's a pinned submodule; edits won't
  travel with this repo and dirty the submodule. `doom_ring.py` customizes the
  game entirely by **runtime patching** (`settings`, `Player`, `Weapon`, and
  per-instance overrides) — extend that pattern, don't touch upstream.
- **Keep input interpretation pure and unit-tested.** `LegTracker._*` and
  flappy's `_on_strain` take samples in and return decisions out, with no HID
  calls — that's why `test_leg.py` / `test_flappy.py` run with no hardware.
  Put new gesture logic in that shape; thresholds are tuned against recorded
  fixtures (`leg_trace.csv`), not guessed.
- **HID binding is cython `hidapi`, not apmorton `hid`.** Both claim the `hid`
  import name and conflict — depend on exactly one. `pip install hidapi` ships
  prebuilt wheels (bundled native lib) for Windows + macOS, so no compiler /
  brew / manual DLL. If you ever see a "can't load hidapi" import error,
  someone installed the wrong package; `pip uninstall hid && pip install hidapi`.
- **Stop reader threads before quitting.** Each game runs Joy-Con readers on
  daemon threads holding native HID handles. Tearing down the interpreter while
  one is mid-`read()` segfaults the process — call `.stop()` + `.join()` before
  `pg.quit()`/`sys.exit()` (see `doom_ring.RingGame._shutdown`).
- **Calibrate while worn/still.** Leg detection calibrates a rest pose on
  startup — hold the strapped leg still for the first few seconds. The rest
  gravity axis depends on mounting, so `LegTracker` keys off tilt-from-rest
  rather than a hardcoded axis.

## Setup & run

```bash
git clone --recurse-submodules <url> && cd recurse-ringcon-hacking
python -m venv .venv && .venv/Scripts/activate   # (or: source .venv/bin/activate)
pip install -r requirements.txt                  # hidapi (cython) + pygame-ce

python monitor.py     # stream raw strain + IMU
python ringfit.py     # combined Ring-Con + leg readout
python flappy.py / ski.py / doom_ring.py          # games
python -m unittest test_flappy test_leg           # no hardware needed
```

Pair the Joy-Con(s) over Bluetooth first (they enumerate as HID gamepads). The
**right** Joy-Con is the one the Ring-Con attaches to; the **left** is the leg
strap. Joy-Cons sleep when idle — press a button to wake before launching.

## doom_ring.py controls

Run in place → move (tilt forward/back picks direction); tilt left/right →
turn/aim; **squat → freeze aim** for a stable shot; squeeze → fire; pull →
cycle weapon. Difficulty is tuned (monster damage halved, player weapon damage
doubled). With no left pad connected it falls back to tilt-only movement.
