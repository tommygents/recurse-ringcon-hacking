"""Unit tests for JoyConReader._on_strain — no hardware, no threads.

We instantiate the reader, monkeypatch time.monotonic via flappy.time, and
feed strain values directly to _on_strain, then read flaps out of the queue.
"""

import queue
import unittest
from unittest import mock

import flappy
from flappy import (
    FLAP_ONSET_DELTA,
    FLAP_REFRACTORY_MS,
    JoyConReader,
    STRAIN_MAX,
    STRAIN_REST,
)


class FakeClock:
    """Controllable monotonic clock; returns seconds (flappy multiplies by 1000)."""

    def __init__(self, start=1000.0):
        self.now = start

    def advance_ms(self, ms):
        self.now += ms / 1000.0

    def __call__(self):
        return self.now


def drain(q):
    out = []
    while True:
        try:
            out.append(q.get_nowait())
        except queue.Empty:
            return out


class OnStrainTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        patcher = mock.patch.object(flappy.time, "monotonic", self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.reader = JoyConReader()

    def feed(self, values, ms_between=20):
        for v in values:
            self.reader._on_strain(v)
            self.clock.advance_ms(ms_between)

    def test_single_push_emits_one_flap(self):
        # Rest → push past onset (0x0E, delta=4) → back to rest.
        self.feed([0x0A, 0x0C, 0x0E, 0x0C, 0x0A])
        flaps = drain(self.reader.flaps)
        self.assertEqual(len(flaps), 1, f"expected 1 flap, got {flaps}")

    def test_single_pull_emits_one_flap(self):
        # Rest → pull past onset (0x06, delta=4) → back to rest.
        self.feed([0x0A, 0x08, 0x06, 0x08, 0x0A])
        flaps = drain(self.reader.flaps)
        self.assertEqual(len(flaps), 1, f"expected 1 flap (pull), got {flaps}")

    def test_held_squeeze_emits_one_flap(self):
        # Settle at rest, then jam to 0x0E and hold for many calls. The first
        # sample defines rest (so we send rest first), then a held squeeze
        # should fire exactly one flap — _armed stays False without a return
        # to rest.
        self.feed([0x0A] * 3)
        self.feed([0x0E] * 50, ms_between=20)
        flaps = drain(self.reader.flaps)
        self.assertEqual(len(flaps), 1, f"expected 1 flap for held squeeze, got {flaps}")

    def test_strength_scales_with_magnitude(self):
        # Light push 0x0E.
        self.reader._on_strain(0x0A)
        self.clock.advance_ms(20)
        self.reader._on_strain(0x0E)
        light_flap = self.reader.flaps.get_nowait()
        # Re-arm.
        self.clock.advance_ms(FLAP_REFRACTORY_MS + 20)
        self.reader._on_strain(0x0A)
        self.clock.advance_ms(20)
        # Full push 0x14.
        self.reader._on_strain(0x14)
        full_flap = self.reader.flaps.get_nowait()
        self.assertGreater(full_flap, light_flap,
                           f"full ({full_flap}) should be > light ({light_flap})")
        self.assertGreaterEqual(light_flap, 0.0)
        self.assertLessEqual(full_flap, 1.0)

    def test_two_consecutive_squeezes(self):
        # First squeeze, return to rest, second squeeze.
        self.feed([0x0A, 0x0E, 0x0A], ms_between=20)
        self.clock.advance_ms(FLAP_REFRACTORY_MS + 20)
        self.feed([0x0E, 0x0A], ms_between=20)
        flaps = drain(self.reader.flaps)
        self.assertEqual(len(flaps), 2, f"expected 2 flaps, got {flaps}")

    def test_actual_rest_shifted_away_from_constant(self):
        # Repro for the field bug: user's actual rest position sits above
        # FLAP_ONSET_DELTA away from STRAIN_REST. Strain values oscillate but
        # never come within FLAP_REARM_DELTA of STRAIN_REST. The first crossing
        # may flap (or not), but each *intentional* squeeze past it should also
        # produce a flap. Today this never re-arms.
        # Simulate: rest sits at 0x0D (delta=3), user pumps to 0x12 (delta=8).
        actual_rest = 0x0D
        squeeze = 0x12
        self.feed([actual_rest] * 5)        # idle at the user's rest
        flaps_after_idle = drain(self.reader.flaps)
        # Now perform three deliberate squeezes back to rest.
        for _ in range(3):
            self.clock.advance_ms(FLAP_REFRACTORY_MS + 50)
            self.feed([squeeze, actual_rest], ms_between=20)
        flaps_after_squeezes = drain(self.reader.flaps)
        self.assertGreaterEqual(
            len(flaps_after_squeezes), 3,
            f"expected at least 3 flaps for 3 squeezes (idle gave {flaps_after_idle}); "
            f"got {flaps_after_squeezes}"
        )

    def test_refractory_blocks_rapid_double(self):
        # First flap.
        self.reader._on_strain(0x0A)
        self.reader._on_strain(0x0E)
        first = drain(self.reader.flaps)
        self.assertEqual(len(first), 1)
        # Return to rest to re-arm.
        self.reader._on_strain(0x0A)
        # Squeeze again immediately, before refractory window expires.
        self.clock.advance_ms(FLAP_REFRACTORY_MS // 4)
        self.reader._on_strain(0x0E)
        second = drain(self.reader.flaps)
        self.assertEqual(len(second), 0,
                         f"refractory should block second flap, got {second}")


if __name__ == "__main__":
    unittest.main()
