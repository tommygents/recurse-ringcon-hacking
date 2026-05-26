"""SkiFree-ish game driven by Ring-Con.

  Push the ring inward (squeeze together) → speed up.
  Pull the ring outward (stretch apart)  → slow down.
  Tilt / rotate the ring like a steering wheel → turn left/right.

Steering reads the Joy-Con accelerometer. At game start we sample a couple
of seconds of "rest" orientation; tilt is then measured as the change in
one accel axis relative to that rest. Different grips / orientations may
need a different axis — start with the default and use the arrow keys '['
and ']' at runtime to swap the steering axis if it feels wrong.
"""

import math
import random
import sys
import threading
import time

import pygame

from monitor import JoyCon, find_joycon, parse_report

WIDTH, HEIGHT = 480, 720
FPS = 60

# Skier
SKIER_Y = int(HEIGHT * 0.32)
SKIER_R = 14
SKIER_COLOR = (40, 60, 200)

# World
SNOW = (240, 248, 255)
TREE_COLOR = (15, 110, 30)
ROCK_COLOR = (110, 110, 110)
TEXT = (20, 20, 40)

TREE_W, TREE_H = 22, 36
ROCK_R = 13
OBSTACLE_INTERVAL_MS = 350    # avg time between spawns at base speed

# Speed model. Strain delta maps roughly to speed delta. Push (strain>rest)
# adds, pull (strain<rest) subtracts.
BASE_SPEED = 3.0      # pixels-per-frame at rest strain
MAX_SPEED = 10.0
MIN_SPEED = 0.6
SPEED_PER_DELTA = 0.7  # extra speed per unit |strain - rest|

# Steering. Tilt is normalized to roughly [-1, 1].
ACCEL_TILT_SCALE = 4000.0  # raw accel delta that corresponds to "full tilt"
STEER_RATE = 6.0           # max horizontal pixels/frame at full tilt
TILT_SIGN = -1             # flip if rotating one way steers the wrong way

CALIB_FRAMES = 90  # ~1.5s of samples to average for rest accel/strain


class JoyConReader(threading.Thread):
    """Reads strain + accel continuously; exposes the latest values."""

    def __init__(self):
        super().__init__(daemon=True)
        self.strain = None
        self.accel = None
        self.status = "connecting"
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

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
                if not rep:
                    continue
                if rep["strain"] is not None:
                    self.strain = rep["strain"]
                if rep["accel"] is not None:
                    self.accel = rep["accel"]
        finally:
            jc.close()


def spawn_obstacle():
    kind = "tree" if random.random() < 0.7 else "rock"
    x = random.randint(20, WIDTH - 20)
    return {"x": float(x), "y": float(HEIGHT + 30), "kind": kind, "passed": False}


def obstacle_rect(o):
    if o["kind"] == "tree":
        return pygame.Rect(o["x"] - TREE_W // 2, o["y"] - TREE_H,
                           TREE_W, TREE_H)
    return pygame.Rect(o["x"] - ROCK_R, o["y"] - ROCK_R, ROCK_R * 2, ROCK_R * 2)


def draw_obstacle(screen, o):
    if o["kind"] == "tree":
        cx = int(o["x"])
        top = int(o["y"]) - TREE_H
        # Triangle for foliage, small trunk.
        pygame.draw.polygon(screen, TREE_COLOR, [
            (cx, top),
            (cx - TREE_W // 2, top + TREE_H - 6),
            (cx + TREE_W // 2, top + TREE_H - 6),
        ])
        pygame.draw.rect(screen, (90, 50, 20),
                         (cx - 3, top + TREE_H - 8, 6, 8))
    else:
        pygame.draw.circle(screen, ROCK_COLOR,
                           (int(o["x"]), int(o["y"])), ROCK_R)
        pygame.draw.circle(screen, (60, 60, 60),
                           (int(o["x"]), int(o["y"])), ROCK_R, 2)


def draw_skier(screen, x, tilt):
    # Body
    pygame.draw.circle(screen, SKIER_COLOR, (int(x), SKIER_Y), SKIER_R)
    pygame.draw.circle(screen, (0, 0, 0), (int(x), SKIER_Y), SKIER_R, 2)
    # Skis rotate slightly with tilt for feedback.
    angle = tilt * 0.6
    cos_a, sin_a = math.cos(angle), math.sin(angle)
    for ski_dx in (-7, 7):
        # local ski endpoints (-10, +5) along the ski axis, offset sideways
        p1_local = (-12, ski_dx)
        p2_local = (12, ski_dx)
        p1 = (int(x + p1_local[0] * cos_a - p1_local[1] * sin_a),
              SKIER_Y + SKIER_R + int(p1_local[0] * sin_a + p1_local[1] * cos_a))
        p2 = (int(x + p2_local[0] * cos_a - p2_local[1] * sin_a),
              SKIER_Y + SKIER_R + int(p2_local[0] * sin_a + p2_local[1] * cos_a))
        pygame.draw.line(screen, (40, 40, 40), p1, p2, 3)


def main():
    pygame.init()
    screen = pygame.display.set_mode((WIDTH, HEIGHT))
    pygame.display.set_caption("Ring Ski")
    clock = pygame.time.Clock()
    font = pygame.font.SysFont("Helvetica", 22)
    big_font = pygame.font.SysFont("Helvetica", 44, bold=True)
    small = pygame.font.SysFont("Helvetica", 16)

    reader = JoyConReader()
    reader.start()

    # Game state
    skier_x = WIDTH / 2
    obstacles = []
    last_spawn_ms = 0
    distance = 0.0
    game_over = False

    # Calibration state (filled before play starts)
    rest_accel = None
    rest_strain = None
    accel_samples = []
    strain_samples = []
    steering_axis = 0  # 0=x, 1=y, 2=z — toggle with [ and ]

    def reset():
        nonlocal skier_x, obstacles, last_spawn_ms, distance, game_over
        nonlocal rest_accel, rest_strain, accel_samples, strain_samples
        skier_x = WIDTH / 2
        obstacles = []
        last_spawn_ms = 0
        distance = 0.0
        game_over = False
        rest_accel = None
        rest_strain = None
        accel_samples = []
        strain_samples = []

    running = True
    while running:
        clock.tick(FPS)
        now = pygame.time.get_ticks()

        for evt in pygame.event.get():
            if evt.type == pygame.QUIT:
                running = False
            elif evt.type == pygame.KEYDOWN:
                if evt.key == pygame.K_ESCAPE:
                    running = False
                elif evt.key == pygame.K_LEFTBRACKET:
                    steering_axis = (steering_axis - 1) % 3
                elif evt.key == pygame.K_RIGHTBRACKET:
                    steering_axis = (steering_axis + 1) % 3
                elif evt.key == pygame.K_SPACE and game_over:
                    reset()
                elif evt.key == pygame.K_r:
                    reset()

        strain = reader.strain
        accel = reader.accel

        # ---- Calibration phase ----
        if (rest_accel is None and reader.status == "connected"
                and accel is not None and strain is not None):
            accel_samples.append(accel)
            strain_samples.append(strain)
            if len(accel_samples) >= CALIB_FRAMES:
                rest_accel = tuple(sum(s[i] for s in accel_samples)
                                   / len(accel_samples) for i in range(3))
                rest_strain = sum(strain_samples) / len(strain_samples)

        # ---- Derive inputs ----
        speed = BASE_SPEED
        tilt = 0.0
        if rest_accel is not None and accel is not None:
            tilt_raw = accel[steering_axis] - rest_accel[steering_axis]
            tilt = max(-1.0, min(1.0, TILT_SIGN * tilt_raw / ACCEL_TILT_SCALE))
        if rest_strain is not None and strain is not None:
            sd = strain - rest_strain
            speed = BASE_SPEED + sd * SPEED_PER_DELTA
            speed = max(MIN_SPEED, min(MAX_SPEED, speed))

        # ---- Update world ----
        if not game_over and rest_accel is not None:
            skier_x += tilt * STEER_RATE
            skier_x = max(SKIER_R, min(WIDTH - SKIER_R, skier_x))

            distance += speed
            # Spawn rate roughly proportional to speed so density stays similar.
            interval = OBSTACLE_INTERVAL_MS * (BASE_SPEED / max(0.5, speed))
            if now - last_spawn_ms > interval:
                obstacles.append(spawn_obstacle())
                last_spawn_ms = now

            for o in obstacles:
                o["y"] -= speed
            obstacles = [o for o in obstacles if o["y"] > -TREE_H]

            skier_rect = pygame.Rect(skier_x - SKIER_R, SKIER_Y - SKIER_R,
                                     SKIER_R * 2, SKIER_R * 2)
            for o in obstacles:
                if obstacle_rect(o).colliderect(skier_rect):
                    game_over = True
                    break

        # ---- Render ----
        screen.fill(SNOW)
        # Subtle snow texture.
        for i in range(40):
            sx = (i * 137 + (now // 30) % 400) % WIDTH
            sy = (i * 311 + (now // 8) % HEIGHT) % HEIGHT
            screen.fill((220, 230, 245), (sx, sy, 2, 2))

        for o in obstacles:
            draw_obstacle(screen, o)
        draw_skier(screen, skier_x, tilt)

        # HUD
        screen.blit(font.render(f"Distance: {int(distance/10)}", True, TEXT),
                    (12, 8))
        screen.blit(font.render(f"Speed: {speed:.1f}", True, TEXT),
                    (12, 34))
        screen.blit(small.render(f"axis [{('x','y','z')[steering_axis]}]"
                                 f"  tilt {tilt:+.2f}",
                                 True, TEXT), (12, 60))
        screen.blit(small.render(f"strain {strain}  rest {rest_strain}",
                                 True, TEXT), (12, 80))
        screen.blit(small.render(reader.status, True, TEXT),
                    (12, HEIGHT - 22))

        # Tilt indicator: horizontal bar with a notch.
        tx, ty, tw, th = WIDTH // 2 - 80, 8, 160, 14
        pygame.draw.rect(screen, (220, 220, 230), (tx, ty, tw, th))
        pygame.draw.rect(screen, (60, 60, 80), (tx, ty, tw, th), 2)
        center_x = tx + tw // 2
        pygame.draw.line(screen, (140, 140, 160),
                         (center_x, ty), (center_x, ty + th), 1)
        marker_x = center_x + int(tilt * (tw // 2 - 4))
        pygame.draw.rect(screen, (220, 60, 60), (marker_x - 3, ty - 2, 6, th + 4))

        if rest_accel is None:
            t = big_font.render("Hold steady…", True, TEXT)
            screen.blit(t, (WIDTH // 2 - t.get_width() // 2, HEIGHT // 2 - 60))
            if reader.status != "connected":
                t = font.render(reader.status, True, (180, 0, 0))
            else:
                pct = int(100 * len(accel_samples) / CALIB_FRAMES)
                t = font.render(f"Calibrating rest pose… {pct}%", True, TEXT)
            screen.blit(t, (WIDTH // 2 - t.get_width() // 2, HEIGHT // 2))
            t = small.render(
                "Hold the ring in neutral steering position; don't squeeze.",
                True, TEXT)
            screen.blit(t, (WIDTH // 2 - t.get_width() // 2, HEIGHT // 2 + 32))
        elif game_over:
            t = big_font.render("Wiped Out!", True, (180, 0, 0))
            screen.blit(t, (WIDTH // 2 - t.get_width() // 2, HEIGHT // 2 - 60))
            t = font.render(f"Distance: {int(distance/10)}", True, TEXT)
            screen.blit(t, (WIDTH // 2 - t.get_width() // 2, HEIGHT // 2 + 10))
            t = font.render("Space or R to retry", True, TEXT)
            screen.blit(t, (WIDTH // 2 - t.get_width() // 2, HEIGHT // 2 + 40))

        pygame.display.flip()

    reader.stop()
    pygame.quit()
    sys.exit(0)


if __name__ == "__main__":
    main()
