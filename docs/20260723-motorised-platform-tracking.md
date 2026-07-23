# Motorised aiming platform + coarse tracking (future)

**Date:** 2026-07-23
**Status:** idea / parked — not to be built for some time
**Scope:** terrestrial aiming + coarse astro pointing and "keep-in-region" tracking
(NOT precision sidereal tracking / long exposures)

## Goal

A two-axis (alt-az) motorised platform to aim ~1 kg of camera:
- Terrestrial target aiming.
- Coarse "go roughly to this object" astro pointing.
- Optional **coarse tracking** to keep a target in roughly the same region across
  many short subs (for stacking) — explicitly not long-exposure guiding.

## Hardware

- Raspberry Pi + **Waveshare Stepper Motor HAT rev 2.1** (2-channel → drives both axes).
- Two NEMA-class bipolar steppers (e.g. the 1.5 A / 1.8°/step motor on hand — confirm
  holding torque + R/L from the datasheet before sizing).

## Mechanics (chosen approach — "super simple")

| Axis | Motion | Drive | Why |
|---|---|---|---|
| **Altitude (tilt)** | limited arc | **leadscrew linear actuator** (Tr8×2 + anti-backlash POM nut) pushing a lever | self-locking → **holds through power loss, never collapses**; accurate; slow is fine |
| **Azimuth (pan)** | continuous | **GT2 belt** on a large base/turntable bearing, with tensioner | vertical axis doesn't fight gravity (no collapse); low-backlash; accurate |

Design notes:
- Priorities: **accuracy over speed**; no strong hold needed, but must **not collapse on power loss** — the self-locking leadscrew gives this for free (it holds where it stops; it does not slowly unwind).
- The **pivot bearing carries the camera weight**, not the leadscrew; the screw only sets the angle.
- Keep the actuator roughly perpendicular to the lever at mid-range (best advantage, most linear response); avoid shallow-angle extremes.
- **Endstops** for homing (open-loop steppers; a lost step is permanent until re-home).
- Mostly **3D-printed** structure in **PETG/ASA** (not PLA — it creeps under load and softens in sun). Metal for the wear parts (leadscrew; buy matched worm/wheel sets if a worm is ever used) + **clamp-style couplings** (not set-screw) sized to the 5 mm NEMA shaft.
- Angle ↔ screw-position on the tilt axis is **non-linear** (law of cosines on the lever triangle) — a few lines of trig in software.

## Tracking approach

Alt-az tracking causes **field rotation** (the frame rotates around the centred
target). This is a **non-issue** for the intended use: many short subs that get
**registered/de-rotated by the stacker (Siril)**. It would only matter for a single
long exposure, which is out of scope.

**Preferred: plate-solve-and-recentre (closed loop)** — reuses the existing solver:

```
point → capture sub → plate-solve (actual centre RA/Dec)
→ compare to desired centre → convert drift to alt/az nudges → move steppers
→ repeat every few subs
```

Self-correcting: tolerant of imperfect levelling, north alignment, and gear slop —
each solve measures the true position. Correct every few subs (Earth ≈ 15°/hr ≈
15 arcsec/s; a ~10° full-frame field drifts slowly). Preferred over open-loop
alt-az rate computation, which needs precise alignment and drifts.

Gotchas: backlash on reversals (bias corrections one direction; anti-backlash nut +
belt tension); avoid the **zenith keyhole** (azimuth rate spikes overhead).

## Architecture fit

Drops into the hexagonal layout as another port + adapter + service:
- **`Platform` (or `Mount`) port** — `move(az, alt)` / `nudge(...)` / `home()`.
- **Adapter** — Raspberry-Pi + Waveshare-HAT stepper driver.
- **Tracking application service** — composes the existing **camera** port + **solver**
  + the new **platform** port: `capture → solve → correct → loop`.

## EU sourcing (Germany-friendly; avoids AliExpress/customs)

- Worm gears / power transmission: **Mädler** (maedler.de), **igus** (plastic worm sets/bearings).
- Steppers (incl. geared): **Nanotec** (DE), **StepperOnline/OMC** (EU warehouse).
- Frame / extrusion / bearings: **Motedis**, **Dold Mechatronik**, **igus**, **Kugellager-Express**.
- Maker bits / belts / drivers: **BerryBase**, **Eckstein**, **Watterott**, **Exp-Tech**, **Reichelt**, **Conrad**, **RS**.
