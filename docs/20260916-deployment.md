# Deployment — where the container line falls

**Date:** 2026-09-16
**Status:** accepted
**Branch:** `feat/web-ui`

## Summary

Docker is worth having here for **dependency management**, not for running the
telescope. `rawpy`, `gphoto2` and `opencv-python` are the awkward wheels and a
locked image makes "does it install on a clean machine" a one-liner. But the
camera path must stay **native**: gphoto2 claims USB exclusively and the adapter
releases the desktop's gvfs mount before doing so, which a container cannot do.

So: one image, used for plate-solving and CI. Capture runs under `uv run`.

## Which do I run?

| Task | Native (`uv run`) | Container | Why |
|---|---|---|---|
| `batch` (solve a folder) | works | **recommended** | No camera. Heaviest dependency surface — exactly what the image is for. |
| `solve` (single file) | works | works | Same as above. |
| `make test` / `make lint` / CI | **recommended** | works | No hardware needed either way; the image is the reproducibility check. |
| `grab` (download from card) | **required** | no | USB. |
| `capture` / `align` / `sequence` | **required** | no | USB + gvfs release. |
| Live view | **required** | no | USB, long-lived PTP session. |
| FastAPI server (`interfaces/api/`) | **required** | no | It drives the camera. Binds to localhost; no reason to containerise a single-user local UI. |

Short version: **if a camera is attached to the workflow, run it natively.**

## USB in a container — the verdict

**Not viable on this setup. Do not try it at the telescope.**

The reasoning, checked against the code and this host rather than assumed:

1. **The gvfs release cannot cross the boundary.** `unmount_gvfs()`
   (`camera_orchestrator/adapters/camera/gvfs.py:19-25`) shells out to
   `gio mount --list` / `gio mount -u`, and `GphotoCamera` calls it on connect
   (`adapters/camera/gphoto.py:45`). `gio` talks to the **session** D-Bus. A
   container has no session bus and no `gvfsd`, so `gio mount --list` inside the
   container returns the container's own (empty) mount list. It cannot see, let
   alone release, the host's mount. Confirmed on this machine:
   `gvfs-gphoto2-volume-monitor` and `gvfsd-fuse` are running as the desktop
   user, and they auto-mount the camera the moment it enumerates. The container
   then gets `[-53] Could not claim the USB device`, and the project's own
   automatic workaround is inert.
2. **Re-enumeration breaks a fixed `--device`.** The body auto-powers-off and
   drops USB (`CLAUDE.md`, domain notes). When it comes back it may appear on a
   different bus/device node, so a container started with
   `--device /dev/bus/usb/003/004` is pointing at a node that no longer exists.
   `-v /dev/bus/usb:/dev/bus/usb` survives re-enumeration — but it is a
   whole-bus grant, and it still does not solve (1): the host's gvfs re-mounts
   the camera on every re-enumeration, so every wake-up would need a manual
   host-side `gio mount -u` before the container could reclaim it.
3. **The non-root user in the image cannot open the device anyway.** On this
   host `/dev/bus/usb/*/*` is `crw-rw-r-- root root` (major 189). PTP needs
   write. So the container would have to run as root, plus a device cgroup rule
   (`c 189:* rmw`), discarding the image's non-root posture for the one workflow
   where a bug means a ruined night.

Each of these has a workaround; stacked, they mean a fragile USB path made
strictly more fragile, in exchange for nothing — this is a single-user tool on
one laptop. The convenience is not worth a `[-53]` at 2am.

## Docker-in-Docker — the real trap if you containerise solving

`DockerSolver` shells out to `docker run` (`adapters/solvers/docker.py:80-89`).
Run the app in a container and you need a Docker daemon reachable from inside
it; mounting `/var/run/docker.sock` is the usual answer. Two things to know:

- **The socket is root-equivalent on the host.** Anything that can talk to the
  daemon can start a privileged container that mounts `/`. Acceptable for your
  own laptop; not something to hand out.
- **Bind-mount paths are resolved by the daemon, i.e. on the HOST.** This is the
  one that bites. `DockerSolver` creates its work directory with
  `tempfile.TemporaryDirectory()` (line 65) — inside the app container — copies
  the source image in, then passes `-v {work}:/data`. The host daemon looks up
  `{work}` on the **host** filesystem, does not find it, and (per Docker's bind
  semantics) creates an empty directory and mounts that. `solve-field` gets an
  empty `/data`, no `.wcs` is produced, and `solve()` returns `None` — which the
  rest of the code reads as **"no astrometric solution found"**. A configuration
  error disguised as a failed solve. The same applies to `index_dir`
  (`docker.py:39`, `abspath` inside the container).

**Mitigation, and the only reason `compose.yaml` works:** mirror host paths —
mount every relevant path at *the same path* inside the container (repo dir,
index dir, and a `TMPDIR` that also exists on the host), so a path resolves
identically on both sides. That is what `compose.yaml` does. It is a workaround,
not a fix.

Note there is no such problem running the CLI natively: `{work}` and the index
dir are already host paths.

## Parked — the HTTP solver removes the DinD problem entirely

`ApiSolver` (`adapters/solvers/api.py`) is a stub today. Implemented, it would
plate-solve by POSTing to a running `astrometry-api-server` instead of
`docker run` per frame — no `docker.sock`, no root-equivalence, no host-vs-
container path mirroring, just two compose services talking over HTTP. The
upstream blockers are resolved (self-contained local-exec server; image
published), and the design is written up in draft PR #5
(`docs/20260712-api-solver-integration.md`). It needs `ApiSolver` implemented
plus a `backend: docker|api` switch in `composition.build_solver()`. Out of
scope for this branch; recorded so it is not lost.

## What was built

- `Dockerfile` — multi-stage. Builder installs `build-essential`, `pkg-config`,
  `libgphoto2-dev`, `libraw-dev` and runs `uv sync --frozen --no-dev` against the
  committed `uv.lock`; runtime carries only the shared libraries (`libgphoto2-6`,
  `libraw20`, `libgl1`, `libglib2.0-0`, `libgomp1`) plus the `gphoto2` CLI that
  `adapters/camera/cli_grab.py:22` shells out to, and the Docker **client**
  binary for `DockerSolver`. Runs as non-root `app` (uid 1000, so files written
  into a bind-mounted `incoming/` are owned by the human).
- `.dockerignore` — keeps `incoming/` (tens of GB), `.venv/`, `.git/`, `docs/`,
  `tests/`, caches and `config.yaml` out of the build context. `config.yaml` is
  gitignored and stays **mounted, never baked**.
- `compose.yaml` — one `solve` service, host-path-mirrored. No camera service,
  deliberately.
- `make docker-build` / `make docker-shell`.

## Verification

Performed on 2026-09-16, Docker Engine 29.2.1, Compose v5.0.2:

- `docker build -t camera-orchestrator:dev .` — **succeeds**. Final image
  **869 MB** (`python:3.11-slim` + astropy/opencv/rawpy is simply a large
  dependency set; no build toolchain in the runtime layer).
- `docker run --rm camera-orchestrator:dev --help` — prints the full verb list
  (`batch, grab, capture, align, solve, sequence`).
- In-image smoke: `id` → `uid=1000(app)`; `import cv2, rawpy, astropy, gphoto2,
  numpy` all succeed (OpenCV 5.0.0); `gphoto2 2.5.28` and the Docker client
  present.
- `make help` still parses and lists the new targets.

Not verified (needs hardware / index files, and would have to run at the scope):

- An end-to-end `batch` solve from inside the container against real index files
  — i.e. the host-path-mirroring mitigation is reasoned from Docker's bind
  semantics and `docker.py:65,82-84`, not yet demonstrated on a real solve.
- The USB verdict is established from the code, the running `gvfs-gphoto2`
  monitor and `/dev/bus/usb` permissions on this host; no camera was attached
  during this work.
