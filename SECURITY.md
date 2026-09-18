# Security model

**Last reviewed:** 2026-09-18 (the fixes below landed; written ahead of them on
2026-09-16, when the HTTP API was added)

Until this project grew a web UI it had no network surface at all — it was a CLI
that talked to a camera over USB. `camera-orchestrator serve` changes that, and
this file records what is and is not defended, so the next person does not have
to re-derive it.

## What this is

A single-user tool on a laptop at a telescope. It is **not** multi-tenant, has no
user accounts, and is not intended to be exposed to the internet. Decisions here
are sized to that, deliberately.

## Threat model

Two adversaries are realistic. Everything else is noise.

**(A) A web page you open in a browser on the same laptop.**
This one reaches the service *even when it is bound to `127.0.0.1`*, because the
request comes from your own machine. A firewall does not help: it filters
packets arriving from other hosts, and this is one local process talking to
another. This is the threat model that applies by default, all the time.

**(B) Anyone on the same network, once `--host 0.0.0.0` is used.**
Star party, campsite, hotel wifi, phone hotspot. Only applies when you opt in to
LAN binding for phone access.

## Defended

- **Cross-site WebSocket hijacking (A).** `WS /api/ws` validates the `Origin`
  header before `accept()` and closes with **1008** otherwise. WebSockets are
  exempt from the same-origin policy, so without this any page you visited could
  read your job history and send `cancel`/`confirm` — including skipping a
  lens-cap prompt and silently ruining a calibration set. The rule, exactly:
  an **absent** Origin is allowed (a non-browser client — `curl`, a script, a
  native app — which has no ambient authority to abuse), a **loopback** origin is
  allowed whatever its port, and anything else must match the `Host` the request
  actually arrived on, compared as scheme + host + port. Matching on the arriving
  Host rather than on a configured name is what keeps the phone working: bound to
  `0.0.0.0` the page's origin *is* the LAN address the phone typed.
- **DNS rebinding (A).** `TrustedHostMiddleware` validates `Host` against an
  allow-list built at `serve` time: loopback (`localhost`, `127.0.0.1`, `::1`)
  plus either the host `--host` named, or — for a wildcard bind — this machine's
  own hostname and addresses. Never `*`. Without it, a page can resolve a name it
  controls to `127.0.0.1` and issue same-origin requests, which turns every (B)
  finding into an (A) finding. The middleware is installed by `serve`, which is
  the only entry point that owns a socket; `create_app()` without a `bind_host`
  (tests, embedding) does not install it.
- **Arbitrary file read (B).** `GET /api/files/raw` and the browse routes confine
  every path to a root fixed at `serve` time. The root is **not** read from
  config per request: `grab.out_dir` is writable over the API, so sourcing the
  confinement root from it made the confinement decorative — one `PUT
  /api/config` setting it to `/` was enough to read `/etc/passwd`.
- **Writes outside the capture tree (B).** `folder`, `file` and `out_dir` on the
  batch, solve, capture, align and sequence job routes resolve through the same
  confinement and 400 on escape. `out_dir` is checked *after* the session folder
  is composed, so a `name` of `../../..` is caught too, and the confined path is
  the one the job then uses — the directory checked is the directory written.
- **Docker argument injection (B).** `solver.image` is validated as a registry
  reference by a Pydantic validator on `SolverConfig` — so it is refused at
  config load *and* by `PUT /api/config` (422), before anything is written to
  disk — and the `docker run` argv is terminated with `--` before the image.
  It is interpolated at a position where docker is still parsing options, so
  `--privileged`, `--entrypoint` and `-v /:/host` were all reachable through a
  config write. The docker group is root-equivalent
  (`docs/20260916-deployment.md`), so this was host root.
- **YAML deserialisation.** `Config.load` uses `yaml.safe_load`; the round-trip
  writer uses `ruamel.yaml` in round-trip mode. Neither constructs arbitrary
  Python objects.
- **Resource exhaustion over the socket.** Inbound frames are capped at 1 MiB by
  `websockets`; the outbound buffer is bounded and drops rather than growing.

## Accepted, not defended

**There is no authentication.** With `--host 0.0.0.0`, anyone who can reach the
port can drive the camera, start and cancel exposures, and read the capture tree
and the images in it.

This is a deliberate decision, not an oversight. After the fixes above, the worst
a LAN adversary achieves is firing your shutter and reading your astrophotos —
annoying, not dangerous — and the cost of a token or a login is paid every night
by the one person using it. Revisit this if the tool ever runs unattended, is
used somewhere genuinely hostile, or gains anything worth stealing.

Mitigations that cost nothing:

- The default bind is `127.0.0.1`. Only pass `--host 0.0.0.0` when you actually
  need the phone, and prefer a hotspot you control over shared wifi.
- A host firewall that does not open the API port closes (B) entirely, even with
  the server bound to `0.0.0.0`. Worth having, but treat it as defence in depth
  rather than *the* control: it is host state, easily changed, and invisible from
  the code.

**A firewall does nothing for (A).** It filters packets arriving from other
hosts. The cross-site WebSocket case is your own browser connecting to
`127.0.0.1`, which never crosses a network interface, so no firewall rule and no
loopback binding can observe it, let alone stop it. That is why the `Origin`
check lives in the code. Do not remove it on the grounds that the port is
firewalled.

## Deliberately not implemented

Recorded so nobody assumes these exist, and so the reasoning can be argued with
rather than re-derived.

| Control | Why not |
|---|---|
| **Authentication of any kind** — no login, no token, no API key | One person, one laptop, one camera. Every request would pay a cost that only ever protects against someone already on the network. Revisit if the tool runs unattended or somewhere hostile. |
| **Authorisation / roles** | Follows from the above: there is one user and they may do everything. |
| **Loopback-only gating for the dangerous routes** | Considered and rejected. Restricting config writes and path-taking routes to `127.0.0.1` would have kept the phone workflow intact, but once the underlying primitives were fixed the remaining LAN capability is "fire the shutter and read the astrophotos", which does not justify the branch in every handler. |
| **TLS** | Plain HTTP. The traffic is a local MJPEG stream and job JSON; a certificate for a LAN IP is friction with nothing to protect. Also means `wss://` is unused — the client derives the scheme from `location`, so TLS would work if it were ever terminated in front. |
| **Rate limiting / request quotas** | No adversary worth rate-limiting. The camera is self-limiting: one job at a time, enforced in the job registry. |
| **CORS middleware** | None is configured, which is the safe default — cross-origin JSON requests are blocked by preflight without it. Adding permissive CORS would undo that. The WebSocket needs its own `Origin` check because WebSockets bypass the same-origin policy entirely. |
| **Audit logging of API actions** | The structured log records what the services did; it does not record who asked or from where. There is only one asker. |
| **Secrets handling** | There are none. No credentials, no tokens, no keys anywhere in config or code. `config.yaml` holds paths, optics and coordinates. |
| **Input size limits on HTTP bodies** | Left at the framework defaults. The WebSocket frame cap (1 MiB) comes from the `websockets` library default, not from us. |
| **Sandboxing the solver** | `DockerSolver` invokes the host docker daemon, which is root-equivalent. The image is now validated and the argv terminated, but a compromised solver image would still have whatever the daemon grants it. The image is pinned in config and is one we build. |

## Known-weak, tracked

- **The job registry is unbounded.** `_records` is never evicted and the whole
  history is serialised into the snapshot on every socket connect. A process left
  running for several nights grows without limit. Not exploitable at one user;
  it should be capped.
- **`PUT /api/config` is unauthenticated** like everything else. It can no longer
  redirect the browse root or reach docker flags, but it can still change ISO
  hints, the solver mode and the plate-solve search region.
- **The wildcard-bind Host allow-list is derived, not enumerated.** With `--host
  0.0.0.0` it is built from the hostname, what `getaddrinfo` returns for it, and
  the address on the interface holding the default route. On a multi-homed
  machine reached over an interface that is *not* the default route, the Host
  will not be on the list and the request is a 400 — a working-but-refused phone,
  not a hole. Fix by binding that interface explicitly (`--host <its address>`),
  which puts it on the list.

## Reporting

Personal project, no disclosure process. Open an issue.
