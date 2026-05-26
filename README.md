# recurse-ringcon-hacking

Reverse-engineering the Nintendo **Ring-Con** over Bluetooth Low Energy (BLE)
to build a low-latency game controller from it.

## The core idea

The **Ring-Con is a passive accessory — it has no radio of its own.** It plugs
into a **Joy-Con**, and its flex/squeeze data rides out over the Joy-Con's
Bluetooth connection. So everything here actually talks to a *Joy-Con*; the
Ring-Con data is read through it.

We're building this empirically, in stages, rather than from a spec:

| Stage | What it does |
|-------|--------------|
| 1. Scan | List nearby BLE devices, identify the Joy-Con |
| 2. Identify | Connect to the Joy-Con, dump GATT services/characteristics |
| 3. Listen | Subscribe to notifications, log the raw hex |
| 4. Decode | Parse the byte patterns from observed data |
| 5. Stream | Clean real-time parsed output — the controller foundation |

## A little BLE background

BLE devices have two phases:

- **Advertising** — before any connection, a device broadcasts small packets so
  others can discover it (name, signal strength, manufacturer data). Scanning
  reads these.
- **Connected** — after you connect, you get full GATT access (services and
  characteristics). The Ring-Con's service UUIDs only become visible *after*
  you connect, so don't expect to see them in a scan.

Nintendo's Bluetooth SIG company ID is **`0x057E`** — a reliable way to flag a
Nintendo device in a scan, alongside matching known Joy-Con names.

## Getting started

Requires **Python 3.12+**. From the repo root:

```bash
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate

pip install -r requirements.txt
```

[`bleak`](https://github.com/hbldh/bleak) is the cross-platform BLE library
doing the real work; it talks to the OS Bluetooth stack (WinRT on Windows,
CoreBluetooth on macOS, BlueZ on Linux) with no extra setup.

## Hardware setup

To scan/connect, put the Joy-Con in **pairing mode**: hold its small round
**sync button** (on the flat rail side, between the SL/SR buttons) until the
four player lights run back and forth. If it was recently bonded to a Switch or
another computer, unpair it there first — a bonded Joy-Con won't advertise.

## Collaborating

Standard GitHub flow:

```bash
git clone https://github.com/tommygents/recurse-ringcon-hacking.git
cd recurse-ringcon-hacking
# work, then:
git add -A && git commit -m "..."
git push
```

Cross-platform line endings are normalized via `.gitattributes`, so Windows and
macOS/Linux checkouts won't fight over CRLF/LF.
