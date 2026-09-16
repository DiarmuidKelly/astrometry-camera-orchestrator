# camera-orchestrator — reproducible image for the NON-CAMERA paths.
#
# What this image is for:
#   * batch / solve  — plate-solving folders of images (no camera involved)
#   * CI and "does it build on a clean machine" reproducibility
#   * pinning the awkward wheels (rawpy, gphoto2, opencv) behind uv.lock
#
# What this image is NOT for: live capture (capture / align / sequence / live
# view / the FastAPI server driving a camera). gphoto2 needs exclusive USB and
# the gvfs release the adapter performs (adapters/camera/gvfs.py:19) cannot
# reach the host's session bus from inside a container. Run those natively.
# See docs/20260916-deployment.md.

# ---------------------------------------------------------------------------
# Stage 1 — builder: compiles anything without a wheel, produces /app/.venv
# ---------------------------------------------------------------------------
FROM python:3.11-slim-bookworm AS builder

# uv is copied in as a static binary rather than pip-installed.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never

# Build toolchain + headers. gphoto2 (python-gphoto2) needs libgphoto2-dev +
# pkg-config when it has to build from sdist; rawpy needs libraw-dev likewise.
# Wheels are preferred when available — these are the fallback path, and they
# stay in this stage so the runtime layer never carries a compiler.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        pkg-config \
        libgphoto2-dev \
        libraw-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependency layer first: it only busts when the lockfile or metadata changes.
COPY pyproject.toml uv.lock VERSION README.md ./
RUN uv sync --frozen --no-dev --no-install-project

# Then the project itself.
COPY camera_orchestrator/ ./camera_orchestrator/
COPY main.py ./
RUN uv sync --frozen --no-dev

# ---------------------------------------------------------------------------
# Stage 2 — runtime: shared libs only, no compiler, non-root
# ---------------------------------------------------------------------------
FROM python:3.11-slim-bookworm AS runtime

# libgphoto2-6 / libgphoto2-port12 — the python gphoto2 bindings link these.
# libgl1 + libglib2.0-0 — opencv-python's manylinux wheel links them.
# libgomp1 — OpenMP runtime used by opencv/numpy kernels.
# libraw20 — rawpy.
# gphoto2 — the CLI binary; adapters/camera/cli_grab.py:22 shells out to it.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgphoto2-6 \
        libgphoto2-port12 \
        libraw20 \
        libgl1 \
        libglib2.0-0 \
        libgomp1 \
        gphoto2 \
    && rm -rf /var/lib/apt/lists/*

# Docker CLI only — no daemon. DockerSolver (adapters/solvers/docker.py:81)
# shells out to `docker run`, so solving from inside this image requires a
# socket mounted in. Read the Docker-in-Docker section of the deployment doc
# before you do that: it is root-equivalent on the host, and the solver's bind
# mounts are resolved by the HOST daemon, so paths must be mirrored.
COPY --from=docker:cli /usr/local/bin/docker /usr/local/bin/docker

# Non-root. uid/gid 1000 matches the typical single-user desktop, so files
# written into bind-mounted incoming/ come out owned by the human.
RUN groupadd --gid 1000 app && \
    useradd --uid 1000 --gid 1000 --create-home --shell /bin/bash app

COPY --from=builder --chown=app:app /app /app

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER app
WORKDIR /app

ENTRYPOINT ["camera-orchestrator"]
CMD ["--help"]
