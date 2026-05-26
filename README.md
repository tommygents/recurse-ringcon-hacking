# recurse-ringcon-hacking

Reverse-engineering the Nintendo **Ring-Con** over **Bluetooth HID** to build a
low-latency game controller from it.

## The core idea

The **Ring-Con is a passive accessory — it has no radio of its own.** It plugs
into a **Joy-Con**, and its flex/squeeze data rides out over the Joy-Con's
Bluetooth connection. So everything here actually talks to a *Joy-Con*; the
Ring-Con data is read through it.

## How it works

The Joy-Con pairs as a **Bluetooth HID** device (not BLE/GATT). We drive it with
Nintendo's MCU subcommand protocol — a port of
[`ringrunnermg/Ringcon-Driver`](https://github.com/ringrunnermg/Ringcon-Driver):

1. Power on the Joy-Con's MCU and switch it into **Ring-Con mode** (CRC-checked
   `0x21` config writes).
2. Probe subcommand `0x59` to confirm the Ring-Con is attached.
3. Start polling and read each input report: a one-byte **strain** value
   (`0x00` fully pulled / `~0x0A` rest / `0x14` fully squeezed) plus the
   Joy-Con **accelerometer** for tilt.

`monitor.py` is both the driver and a standalone CLI that streams strain + IMU.

## The leg strap (running & squats)

Ring Fit also straps a **second, bare Joy-Con** (the left — no Ring-Con) to your
thigh to track **running in place** and **squats**, both from the IMU alone.
`LegTracker` (in `monitor.py`) reads them:

- **Squats** — detected from the *tilt of the gravity vector* away from a
  calibrated standing pose (the thigh rotates from vertical toward horizontal).
  Because it's measured as an angle from rest, it's orientation-agnostic: it
  works however you strap the Joy-Con on, and for either pad's axis-sign
  convention. A rep counts only when the lean *persists* (~0.3s), so a running
  stride's brief tilt doesn't false-count.
- **Running / sprinting** — a rolling average of gyro magnitude (an "energy"
  gate): the faster the thigh whips, the higher it reads.

Calibrate by holding still (strapped on) for a moment, then feed `(accel, gyro)`
samples to `LegTracker.update()` and read `.state`
(`rest` / `run` / `sprint` / `squat`) and `.squat_reps`. With both pads
connected, target them by side: `find_joycon(side="R")` for the Ring-Con,
`find_joycon(side="L")` for the leg.

## What's here

- **`monitor.py`** — the HID driver (the core): Ring-Con strain, the IMU, and
  `LegTracker` (leg-strap running/squat detection), plus a CLI streamer.
- **`flappy.py`** — squeeze the ring to flap. Careful input model: runtime
  rest-calibration, refractory window, peak-relative re-arm. (Space = fallback.)
- **`ski.py`** — squeeze to speed up / pull to slow down; tilt to steer.
- **`doom_ring.py`** — Ring-Con mod of the `doom/` submodule: tilt to move/turn,
  squeeze to fire, pull to cycle weapon.
- **`test_flappy.py`** — unit tests for the flap edge-detector (no hardware).
- **`test_leg.py`** + `leg_trace.csv` — replay a recorded worn-Joy-Con trace
  through `LegTracker` and assert squat counts / run detection (no hardware).
- **`doom/`** — git submodule: [`stanislavPetrovV/DOOM-style-game`](https://github.com/stanislavPetrovV/DOOM-style-game).

## Getting started

Requires **Python 3.12+**. Clone with submodules so `doom/` is populated:

```bash
git clone --recurse-submodules https://github.com/tommygents/recurse-ringcon-hacking.git
cd recurse-ringcon-hacking
# (already cloned without it? run: git submodule update --init)

python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate

pip install -r requirements.txt
```

Dependencies are **pip-only on Windows and macOS** — no compiler, no `brew`, no
manual DLL. [`hidapi`](https://pypi.org/project/hidapi/) (cython) ships prebuilt
wheels that bundle the native HID library; [`pygame-ce`](https://pyga.me/) draws
the games.

## Hardware setup

Pair the Joy-Con over Bluetooth — it shows up as a standard HID gamepad. Put it
in **pairing mode**: hold the small round **sync button** (on the flat rail
side, between the SL/SR buttons) until the four player lights run back and forth.
If it was recently bonded to a Switch or another computer, unpair it there first.
The **right** Joy-Con is the one the Ring-Con attaches to.

## Running

```bash
python monitor.py     # stream raw strain + IMU to the terminal
python flappy.py      # squeeze-to-flap
python ski.py         # strain = speed, tilt = steering
python doom_ring.py   # Ring-Con DOOM (needs the doom/ submodule)
python -m unittest test_flappy test_leg   # input-model tests, no hardware
```

## Collaborating

```bash
# after cloning (with --recurse-submodules):
git pull
git submodule update --init --recursive   # if doom/ changed
# work, then:
git add -A && git commit -m "..."
git push
```

Line endings are normalized via `.gitattributes`, so Windows and macOS/Linux
checkouts won't fight over CRLF/LF.
