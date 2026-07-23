# Session model: `align` + `sequence` verbs and a session manifest

Date: 2026-07-23

## Motivation

Captured frames — especially the *blank* ones (darks/bias) and card-only lights —
land as an undifferentiated pile with no record of what they are, when they were
shot, or where the camera was pointing. This adds a **session** concept: a folder
that owns a `session.json` manifest, written by two verbs that both target it.

## Verbs (interface layer)

Three top-level shapes, no hidden global state:

- **`align [--name X]`** — capture one JPEG, plate-solve it, write an annotated
  preview. Named → records the solved target into `X`'s manifest. Unnamed →
  ephemeral preview in the parent, no manifest (the throwaway "am I on the sky"
  check). Reuses the existing solve path.
- **`sequence [--name X] --lights N --darks N --bias N`** — fire the phases
  (RAW). Named → appends phase records to `X`'s manifest. Unnamed → loose frames
  in the parent, no manifest. (Replaces the old `session` verb.)
- No `session` verb and **no current-session pointer** — a sticky "current"
  session is a stale mode you trip over (align 50 min later silently rewriting a
  target whose frames are already shot). The session name is explicit each time.

### Where output lands (align + sequence share this rule)

```
--name X given  → session folder  <root>/<YYYYMMDD>-X   (created if absent)
no --name       → <root> directly, loose, NO manifest
```

`<root>` is `--out` or `grab.out_dir` from config. The folder is date-prefixed
for readability/sorting and resolves deterministically within a day, so `align`
and `sequence` with the same `--name` hit the same folder. (Crossing midnight
mid-session is an accepted edge case — a rare hand-driven scenario.)

### The one guard: target lock

Requiring the name kills *silent* accidents but not deliberate re-typing. So:

> While a session has **no phase records**, `align --name X` may write/refine the
> target freely (the framing loop). **Once `sequence` has recorded frames under
> X, the target locks** — a later `align --name X` refuses unless `--force`.

Keyed on *frames exist*, so it never blocks the pre-sequence loop but always
protects a completed run. Loose mode can't corrupt anything (no manifest).

## Manifest (domain layer)

`domain/models/session.py`, Pydantic value objects:

```python
class TargetInfo(BaseModel):
    solved: bool
    center_ra_deg: float | None
    center_dec_deg: float | None
    scale_arcsec_per_px: float | None
    preview: str | None    # annotated filename in the session folder
    frame: str | None      # the align frame filename

class PhaseRecord(BaseModel):
    kind: PhaseKind
    count: int             # frames ACTUALLY captured
    iso / shutter / aperture / bulb_seconds
    started_at / ended_at  # UTC
    files: list[str]       # basenames; card-side names (card-only) or local (download)

class SessionManifest(BaseModel):
    schema_version: int = 1
    session_id: str        # the folder name
    name: str | None
    download: bool
    target: TargetInfo | None      # set by align
    started_at / ended_at: datetime | None   # the sequence run window
    phases: list[PhaseRecord]      # appended by sequence
```

Design choices:
- **Settings are per-phase**, not session-level — bias overrides shutter to
  1/4000, so a session-level shutter would lie.
- **`count` is frames actually captured** (`CaptureResult.frames_captured`).
- **`download` disambiguates `files`**: `false` → names are on the card;
  `true` → they're in the folder next to the manifest. Filename reuse is not
  guarded (by the time the counter wraps, we've moved on).
- No SQLite yet — behind a port, so a `SqliteSessionRepository` is a drop-in when
  a cross-night query need (calibration reuse) actually appears.

## Card-only filenames: reconnect + poll (learned the hard way)

Card-only capture (the sequence default) never learns filenames — nothing is
downloaded — and the camera makes this genuinely awkward. What was tried, live on
the 5D Mark II, and why each failed:

1. **`list_files()` diff within the capture session** → empty. libgphoto2 caches
   the directory listing per session; the just-written files don't appear until a
   reconnect.
2. **Drain `FILE_ADDED` events at end of phase** → dropped frames (3 of 5). The
   camera coalesces/drops add-events under rapid card-only fire, so events are
   unreliable for *counting*.
3. **Reconnect once and list** → short, with frames leaking into the next phase.
   The card's write buffer flushes with a lag, so an immediate re-list misses the
   last writes; they then land outside the phase boundary.
4. **Reconnect and poll until `expected` files appear** (shipped) → reliable.

So `CaptureService`, when `record_files` is set (opt-in; plain `capture`
unaffected), snapshots `list_files()` before firing, then after the capture
session closes **reopens fresh sessions and polls** (`_await_new_card_files`)
until the pre-fire diff shows `request.count` new names, or a timeout (~30 s),
logging `Card listing incomplete` and recording best-effort if it can't. The card
is the source of truth; the event stream is not. Download mode already has the
local paths and uses the reliable per-frame `wait_for_new_files`.

Note: the images are always captured — every count matched the card listing in
testing. Only the *event notifications* were lossy. For very large sequences the
buffer-flush poll may time out (count stays correct, some names omitted); the
parked `import`/reconcile command is the clean long-term answer.

## Persistence (ports + adapters)

- `domain/ports/session_manifest.py::SessionManifestRepository` —
  `save(manifest, session_dir)`, `load(session_dir) -> manifest|None`,
  `exists(session_dir)`.
- `adapters/storage/session_manifest.py::SidecarSessionRepository` — writes
  `<session_dir>/session.json`. Mirrors `SidecarSolveRepository`.

## Application wiring

- `AlignService.align(request, session_dir=None, force=False)` — loose when
  `session_dir` is None; otherwise load manifest, enforce the lock, solve into the
  session folder, write `target`, save.
- `SequenceService.run(request, session_dir=None, before_phase=None)` (renamed
  from `SessionService`) — build per-phase records with timestamps + the
  `list_files` diff, append to the manifest, save when `session_dir` is set.
  Returns the manifest. Injectable clock for deterministic tests.
- Format: `sequence` shoots **RAW** (lean card); `align` uses `select="jpeg"` as
  today. Swapping the camera format between the two is acceptable.

## Files

- `domain/models/session.py` — add `TargetInfo`, `PhaseRecord`, `SessionManifest`;
  rename `SessionRequest` → `SequenceRequest`; drop `SessionResult`.
- `domain/models/camera.py` — add `CaptureResult.card_frames`.
- `domain/ports/session_manifest.py` — new port.
- `adapters/storage/session_manifest.py` — new adapter.
- `application/capture_service.py` — `record_files` diff for card-only.
- `application/align_service.py` — session_dir + repo + lock.
- `application/sequence_service.py` — renamed; manifest-writing.
- `composition.py` — `build_session_repository`, updated `build_*` wiring.
- `interfaces/cli.py` — `align`/`sequence` verbs, name→folder resolution, lock.
- `README.md` — document the trio; `session …` → `sequence …`.
- tests — manifest round-trip, lock, `list_files` diff, name resolution.

## Out of scope (parked)

- SQLite calibration index / cross-night reuse queries.
- An `import`/`reconcile` command (card → session folders by manifest).
- `session --status/--list/--close` niceties.
