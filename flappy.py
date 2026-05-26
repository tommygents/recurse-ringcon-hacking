"""Flappy-bird-style game where a squeeze of the Ring-Con makes the bird flap.

Strain values from the Ring-Con (per Ringcon-Driver):
    0x00 = fully pulled outward, 0x0A ≈ rest, 0x14 = fully pushed inward.
A push past FLAP_THRESHOLD triggers one flap; you must relax below
FLAP_RESET before the next push registers (so a held squeeze doesn't auto-flap).
Spacebar works as a keyboard fallback.
"""

import math
import queue
import random
import sys
import threading
import time

import pygame

from monitor import JoyCon, find_joycon, parse_report

WIDTH, HEIGHT = 480, 640
FPS = 60

GRAVITY = 0.45
FLAP_V_MIN = 6.0  # weakest squeeze upward kick
FLAP_V_MAX = 18.0  # full-squeeze upward kick
BIRD_X = 110
BIRD_R = 16

PIPE_W = 70
PIPE_GAP = 180
PIPE_SPEED = 3.0
PIPE_SPAWN_MS = 1500

# Ring-Con strain is bidirectional: 0x0A ≈ rest, 0x14 = pushed inward,
# 0x00 = pulled outward. Trigger a flap on either direction crossing the onset
# delta from rest, and re-arm when the user returns within rearm delta. A short
# refractory window stops a held gesture from auto-firing and lets quick
# pumps register without needing a full relax.
STRAIN_REST = 0x0A
STRAIN_MAX = 0x14
FLAP_ONSET_DELTA = 2     # |strain - rest| ≥ this → flap
FLAP_REARM_DELTA = 1     # |strain - rest| ≤ this → ready to flap again
FLAP_REFRACTORY_MS = 100
KEY_FLAP_STRENGTH = 0.5  # spacebar maps to a mid-strength flap

SKY = (135, 206, 235)
GROUND = (139, 69, 19)
PIPE_COLOR = (34, 139, 34)
BIRD_COLOR = (255, 215, 0)
TEXT = (20, 20, 20)


class JoyConReader(threading.Thread):
    """Owns the HID handle on a background thread.

    Exposes the latest raw strain for UI, plus a queue of flap events
    detected at HID-report rate (≈60Hz) — much lower latency than running
    edge detection in the 60fps render loop.
    """

    def __init__(self):
        super().__init__(daemon=True)
        self.strain = None
        self.status = "connecting"
        self.flaps = queue.Queue()
        self._stop = threading.Event()
        self._armed = True
        self._last_flap_ms = 0.0
        # Calibrated at runtime from the first observed strain. STRAIN_REST is
        # only a nominal estimate; real rest drifts per-device and per-grip and
        # often sits several units off. Using a fixed constant means a user
        # whose rest is far from STRAIN_REST never re-arms (delta stays large).
        self._rest = None
        # Peak |strain - rest| since the last flap fired. Used to decide
        # when the user has "released enough" to re-arm without requiring
        # a full return to rest (which is what blocked rapid pumping).
        self._peak_delta = 0.0

    def stop(self):
        self._stop.set()

    def _on_strain(self, strain):
        self.strain = strain
        if self._rest is None:
            self._rest = strain
        delta = abs(strain - self._rest)
        now_ms = time.monotonic() * 1000.0

        if delta > self._peak_delta:
            self._peak_delta = delta

        # Re-arm either by returning near rest OR by releasing FLAP_ONSET_DELTA
        # from the peak of the current gesture. The peak-relative path lets a
        # user pumping between, say, 0x0C and 0x12 register every cycle even
        # though they never come back within FLAP_REARM_DELTA of rest.
        if not self._armed and (
            delta <= FLAP_REARM_DELTA
            or (self._peak_delta - delta) >= FLAP_ONSET_DELTA
        ):
            self._armed = True
            self._peak_delta = delta

        if (self._armed
                and delta >= FLAP_ONSET_DELTA
                and (now_ms - self._last_flap_ms) >= FLAP_REFRACTORY_MS):
            # Normalize by the smaller of the two possible spans so push and
            # pull max out near 1.0.
            span = min(STRAIN_MAX - self._rest, self._rest)
            strength = max(0.0, min(1.0, (delta - FLAP_ONSET_DELTA) /
                                    max(1, span - FLAP_ONSET_DELTA)))
            self.flaps.put(strength)
            self._armed = False
            self._last_flap_ms = now_ms
            self._peak_delta = delta

        if delta <= FLAP_REARM_DELTA:
            # Slowly track rest while idle so slow grip drift doesn't lock us
            # out again. Heavy bias toward the existing estimate (small alpha)
            # so a single noisy sample can't shift rest into a squeeze.
            self._rest = self._rest * 0.95 + strain * 0.05

    def run(self):
        info = find_joycon()
        if not info:
            self.status = "no Joy-Con found"
            return
        jc = JoyCon(info["path"])
        if not jc.init_ringcon():
            self.status = "Ring-Con init failed"
            jc.close()
            return
        self.status = "connected"
        try:
            while not self._stop.is_set():
                buf = jc.read(timeout_ms=50)
                if not buf:
                    continue
                rep = parse_report(buf)
                if rep and rep["strain"] is not None:
                    self._on_strain(rep["strain"])
        finally:
            jc.close()


def spawn_pipe():
    gap_y = random.randint(80, HEIGHT - 120 - PIPE_GAP)
    return [float(WIDTH), gap_y, False]  # [x, gap_top_y, passed]


def reset_state():
    return {
        "bird_y": HEIGHT / 2,
        "bird_v": 0.0,
        "pipes": [],
        "score": 0,
        "game_over": False,
        "started": False,  # waits for first flap before physics run
        "last_spawn": 0,
    }


def draw_strain_meter(screen, strain):
    """Vertical bar with rest in the middle. Fills upward for push (>rest)
    and downward for pull (<rest). Red onset lines mark the flap threshold
    on both sides."""
    x, y, w, h = WIDTH - 36, 12, 22, 220
    pygame.draw.rect(screen, (240, 240, 240), (x, y, w, h))
    pygame.draw.rect(screen, (60, 60, 60), (x, y, w, h), 2)
    rest_y = y + int(h * (1 - STRAIN_REST / STRAIN_MAX))
    pygame.draw.line(screen, (140, 140, 140), (x, rest_y), (x + w, rest_y), 1)
    if strain is not None:
        delta = strain - STRAIN_REST
        delta_frac = delta / STRAIN_MAX
        bar_h = int(abs(delta_frac) * h)
        in_flap_zone = abs(delta) >= FLAP_ONSET_DELTA
        color = (220, 50, 50) if in_flap_zone else (60, 180, 60)
        if delta >= 0:
            pygame.draw.rect(screen, color, (x + 2, rest_y - bar_h, w - 4, bar_h))
        else:
            pygame.draw.rect(screen, color, (x + 2, rest_y, w - 4, bar_h))
    # Onset lines (above and below rest).
    for sign in (-1, 1):
        oy = rest_y - sign * int(h * FLAP_ONSET_DELTA / STRAIN_MAX)
        pygame.draw.line(screen, (200, 0, 0), (x - 4, oy), (x + w + 4, oy), 2)


def main():
    pygame.init()
    screen = pygame.display.set_mode((WIDTH, HEIGHT))
    pygame.display.set_caption("Ring Flap")
    clock = pygame.time.Clock()
    font = pygame.font.SysFont("Helvetica", 26)
    big_font = pygame.font.SysFont("Helvetica", 56, bold=True)
    small = pygame.font.SysFont("Helvetica", 18)

    reader = JoyConReader()
    reader.start()

    state = reset_state()

    running = True
    while running:
        clock.tick(FPS)
        now = pygame.time.get_ticks()
        # Collect every flap event that arrived between frames, plus any
        # keyboard SPACE presses. Strongest wins for this frame.
        pending_strengths = []
        while True:
            try:
                pending_strengths.append(reader.flaps.get_nowait())
            except queue.Empty:
                break

        for evt in pygame.event.get():
            if evt.type == pygame.QUIT:
                running = False
            elif evt.type == pygame.KEYDOWN:
                if evt.key == pygame.K_ESCAPE:
                    running = False
                elif evt.key == pygame.K_SPACE:
                    pending_strengths.append(KEY_FLAP_STRENGTH)

        strain = reader.strain

        if pending_strengths:
            strength = max(pending_strengths)
            flap_velocity = -(FLAP_V_MIN + (FLAP_V_MAX - FLAP_V_MIN) * strength)
            if state["game_over"]:
                state = reset_state()
            elif not state["started"]:
                state["started"] = True
                state["last_spawn"] = now
                state["bird_v"] = flap_velocity
            else:
                state["bird_v"] = flap_velocity

        if state["started"] and not state["game_over"]:
            state["bird_v"] += GRAVITY
            state["bird_y"] += state["bird_v"]

            if now - state["last_spawn"] > PIPE_SPAWN_MS:
                state["pipes"].append(spawn_pipe())
                state["last_spawn"] = now

            for p in state["pipes"]:
                p[0] -= PIPE_SPEED
            state["pipes"] = [p for p in state["pipes"] if p[0] + PIPE_W > 0]

            bird_rect = pygame.Rect(BIRD_X - BIRD_R, state["bird_y"] - BIRD_R,
                                    BIRD_R * 2, BIRD_R * 2)
            for p in state["pipes"]:
                top = pygame.Rect(p[0], 0, PIPE_W, p[1])
                bot = pygame.Rect(p[0], p[1] + PIPE_GAP, PIPE_W,
                                  HEIGHT - p[1] - PIPE_GAP)
                if bird_rect.colliderect(top) or bird_rect.colliderect(bot):
                    state["game_over"] = True
                if not p[2] and p[0] + PIPE_W < BIRD_X - BIRD_R:
                    p[2] = True
                    state["score"] += 1

            if state["bird_y"] > HEIGHT - 30 or state["bird_y"] < 0:
                state["game_over"] = True
        elif not state["started"]:
            state["bird_y"] = HEIGHT / 2 + 8 * math.sin(now * 0.005)

        # Render
        screen.fill(SKY)
        for p in state["pipes"]:
            pygame.draw.rect(screen, PIPE_COLOR, (p[0], 0, PIPE_W, p[1]))
            pygame.draw.rect(screen, PIPE_COLOR,
                             (p[0], p[1] + PIPE_GAP, PIPE_W,
                              HEIGHT - p[1] - PIPE_GAP))
            pygame.draw.rect(screen, (20, 80, 20),
                             (p[0], 0, PIPE_W, p[1]), 3)
            pygame.draw.rect(screen, (20, 80, 20),
                             (p[0], p[1] + PIPE_GAP, PIPE_W,
                              HEIGHT - p[1] - PIPE_GAP), 3)
        pygame.draw.rect(screen, GROUND, (0, HEIGHT - 30, WIDTH, 30))

        pygame.draw.circle(screen, BIRD_COLOR, (BIRD_X, int(state["bird_y"])), BIRD_R)
        pygame.draw.circle(screen, (0, 0, 0), (BIRD_X, int(state["bird_y"])), BIRD_R, 2)
        # Tiny eye
        pygame.draw.circle(screen, (0, 0, 0),
                           (BIRD_X + 5, int(state["bird_y"]) - 4), 3)

        screen.blit(font.render(f"Score: {state['score']}", True, TEXT), (12, 8))
        screen.blit(small.render(reader.status, True, TEXT), (12, HEIGHT - 26))
        draw_strain_meter(screen, strain)

        if state["game_over"]:
            t = big_font.render("Game Over", True, (180, 0, 0))
            screen.blit(t, (WIDTH // 2 - t.get_width() // 2, HEIGHT // 2 - 80))
            t = font.render("Squeeze to retry  (or Space)", True, TEXT)
            screen.blit(t, (WIDTH // 2 - t.get_width() // 2, HEIGHT // 2 - 10))
            t = font.render(f"Score: {state['score']}", True, TEXT)
            screen.blit(t, (WIDTH // 2 - t.get_width() // 2, HEIGHT // 2 + 30))
        elif not state["started"]:
            if reader.status == "connected":
                t = big_font.render("Ring Flap", True, TEXT)
                screen.blit(t, (WIDTH // 2 - t.get_width() // 2, HEIGHT // 2 - 90))
                t = font.render("Squeeze to start", True, TEXT)
                screen.blit(t, (WIDTH // 2 - t.get_width() // 2, HEIGHT // 2 - 20))
            else:
                t = big_font.render("Waiting...", True, TEXT)
                screen.blit(t, (WIDTH // 2 - t.get_width() // 2, HEIGHT // 2 - 90))
                t = font.render(reader.status, True, (180, 0, 0))
                screen.blit(t, (WIDTH // 2 - t.get_width() // 2, HEIGHT // 2 - 20))
                t = small.render("Press any button on the Joy-Con to wake it,",
                                 True, TEXT)
                screen.blit(t, (WIDTH // 2 - t.get_width() // 2, HEIGHT // 2 + 30))
                t = small.render("or reconnect via System Settings → Bluetooth.",
                                 True, TEXT)
                screen.blit(t, (WIDTH // 2 - t.get_width() // 2, HEIGHT // 2 + 52))

        pygame.display.flip()

    reader.stop()
    pygame.quit()
    sys.exit(0)


if __name__ == "__main__":
    main()
