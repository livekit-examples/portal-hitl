# test/

Hardware-free validation. The transport and recording suites need a local
LiveKit server; the resilience and teleoperator suites run without one. Synthetic
peers publish and consume over the real Portal stack using the real
`portal.yaml`, so the wire contract, the action gate, the synchronizer,
keypoints and the archive are all exercised exactly as in production.
Only the lerobot hardware drivers are stubbed out — `FakeRecorder` stubs
nothing at all, because the recorder has no hardware to stub.

```bash
livekit-server --dev &                  # ws://localhost:7880, devkey/secret

uv run python -m test.test_pubsub       # transport, gate, frame<->state sync
uv run python -m test.test_recorder     # three peers, archive round-trip, alignment
uv run python -m test.test_degraded     # the same under sender-side degradation
uv run python -m test.test_resilience   # bus faults + the speed cap    (no server)
uv run python -m test.test_teleop       # anchor, task switching, keys   (no server)
```

Each script prints PASS/FAIL per check and exits non-zero on failure.
Credentials default to `livekit-server --dev`; set `LIVEKIT_URL` /
`LIVEKIT_API_KEY` / `LIVEKIT_API_SECRET` to point somewhere else.

Archives are read back with `livekit.portal.convert.read_archive`, the reader
`livekit-portal convert` uses, so the tests check alignment through the same
code that builds a dataset. The harness explicitly uses
`Selection(max_stale_us=0)`: each action must match state and frames at precisely
its anchor timestamp. Normal conversion instead permits samples up to 200 ms
old by default (`--max-stale-ms`); these tests deliberately use the stricter
rule so stale fallback cannot hide a timestamp mismatch.

## How alignment is checked

Everything rests on one trick. Each robot tick stamps its own index into
the state (`shoulder_pan.pos`) *and* into the camera frame (as the column
position of a white bar). The operator decodes the bar out of the frame it
is flying on and stamps that tick into the action it sends back
(`elbow_flex.pos`) — while anchoring the action to that same frame's
timestamp. So a correctly aligned archive satisfies, with no tolerance:

```
/observation/shoulder_pan.pos == /action/elbow_flex.pos     (mod TICK_WRAP)
```

The modulo is the bar wrapping: state carries the raw tick, the frame can
only carry `TICK_WRAP` of them before the bar runs off the right edge.

Two things make this a real check rather than a tautology. The anchor is
the **shipped `View.anchor()`** — `FakeOperator` holds a real one and feeds
it every frame, so the alignment every suite asserts is produced by the code
that runs on the rig, not by a copy of it in the harness. Decoding the bar is
the harness's only addition, and it reads the frame that anchor names, so the
two cannot drift apart. And the values are compared **after a round trip
through the file**: reading the `.rrd` back gives one arrow row per
`robot_time`, and the state and the action have to have landed on the same
row for the comparison to happen at all. A mispaired action is an exact
integer mismatch; an action filed at the wrong instant does not share a row
with any state and shows up as a collapse in the `pair` column.

The bar's position independently verifies which frame is on the row. A
position, unlike a flat grey level, either resolves or is absent: a
half-initialised frame of uniform grey would decode as a perfectly plausible
tick, so `tick_from_frame` returns None rather than a plausible-looking lie.

## The scripts

| File | What it is |
|---|---|
| `__init__.py` | local-server env defaults, set before any test module imports |
| `_harness.py` | tick encoding/decoding, the robot subprocess, reading an archive back into paired rows, the `Checks` reporter |
| `fake_peers.py` | `FakeRobot` / `FakeOperator` / `FakeRecorder` — the three processes minus hardware — and `Impairment` |
| `test_pubsub.py` | connect, gate, observation rate, frame↔state sync, transport metrics |
| `test_recorder.py` | all three peers over a real link; reads the `.rrd` back and checks alignment, the archived frames and the keypoints |
| `test_degraded.py` | sweeps impairment profiles and asserts alignment holds under all of them |
| `test_teleop.py` | the operator-side logic the wire tests drive past: `View.anchor`, task switching, the session prompt over a pty, and both keyboard taps; needs neither a server nor hardware |
| `test_resilience.py` | the two layers between the network and the motors: `_ResilientBus` against a follower that fails on demand, and `_StepLimiter` turning a discontinuity into a ramp; needs neither a server nor hardware |
| `peer_robot.py` | the robot peer as its own process — see below |
| `netsim.py` | real transport shaping via pf + dummynet (needs sudo — see below) |

## Two tiers of network simulation

**Tier 1 — `test_degraded.py`.** Degrades the *publisher*: schedule jitter,
dropped state, dropped frames, whole-stream stalls. No privileges needed,
deterministic (seeded), and it makes Portal's synchronizer see the same
irregular input it would in the field. This is what to run by default.

The invariant it asserts: **degradation may age rows or cost them, but it
may never file one against the wrong observation.** A dropped frame moves
the operator's anchor backwards, so the action lands on an older
`robot_time` — which is still the timestamp of the frame it was actually
answering. Degradation therefore separates the profiles on the `pair` and
`lag` columns rather than on correctness. Profiles: `clean`, `jitter`,
`camera-drops`, `state-drops`, `stalls`. Run one with
`PORTAL_TEST_PROFILE=stalls`.

What it cannot model: delay, loss and bandwidth limits *on the wire*.

**Tier 2 — `netsim.py`.** Shapes the actual sockets with pf + dummynet on
LiveKit's ports (TCP 7880/7881 signaling, UDP 7882 media and data channel),
so the peers experience real latency, jitter, loss and a bandwidth cap.

```bash
uv run python -m test.netsim show wan     # print what it would do, change nothing
uv run python -m test.netsim status       # inspect (no sudo)
sudo uv run python -m test.netsim wan     # apply
sudo uv run python -m test.netsim off     # remove and restore
```

Profiles: `lan`, `wifi`, `wan` (60ms/0.5%/50Mbit), `bad-wan`
(150ms/2%/10Mbit), `satellite` (600ms/1%/5Mbit).

> **This edits the system packet filter.** Other software may be using pf
> too — VPNs, Tailscale, Docker, firewall tools. `netsim` saves the current
> ruleset and pf's enabled/disabled state to `/tmp/portal-netsim-state.json`
> before touching anything, loads its own rules into a dedicated anchor
> rather than replacing the main ruleset, and `off` restores both. The state
> file survives a crash, so `off` still works afterwards. Read
> `show <profile>` before running it the first time.

## Other environment knobs

| Variable | Effect |
|---|---|
| `PORTAL_TEST_CODEC` | swap every video codec in `portal.yaml` for this run. The shipped config is `h264` (WebRTC media); `mjpeg` moves video onto the reliable data channel, which is how you reproduce a data-channel bandwidth problem against a Portal one |
| `PORTAL_TEST_PROFILE` | run a single degradation profile |
| `PORTAL_TEST_KEEP_RRD` | keep the recorded `.rrd` instead of deleting it |
| `RUST_LOG=warn` | quiet the LiveKit client logs |

## Peers must run in separate processes

`test/peer_robot.py` exists because publisher and subscriber cannot share
an interpreter. With both in one process:

* WebRTC video panics on the subscribe path (`video frame missing
  user_timestamp` in `video.rs`) and **no frames arrive at all**, for
  every WebRTC codec — h264, vp8, vp9, av1 alike.
* Even where it runs, one process encoding two tracks while decoding two
  more is CPU-starved, which triggers libwebrtc's default
  MAINTAIN_FRAMERATE degradation and silently rescales 640×480 to
  320×240.

Both look exactly like library bugs and are neither. Two processes is
also the topology the rig ships in, so the harness now uses it
everywhere; the byte-stream codecs (mjpeg/png/raw) are unaffected either
way, which is a useful control when isolating a WebRTC problem.

## Measured baselines

On an unloaded host against a local server, Portal 0.3.0rc1, shipped
`h264` codec:

| quantity | value |
|---|---|
| delivered resolution | 640×480, matching what was published |
| observation rate | ~26 fps at a 30 fps tick |
| actions paired in a 4s episode | **120 of 120**, every one with the exact frame it answered |
| frames dropped by the archive writer | 0 |

Every action survives to disk however many answered one frame: `RrdSink` files
each on `action_time` as well as `robot_time`, and Portal's own tests assert
it by anchoring several actions to one frame. Without that second index a
240-tick run loses 25–28% of its actions under jitter, camera drops or stalls,
concentrated in exactly the moments where the video stuttered.

Under degradation the numbers stretch rather than turning into misalignment —
`state-drops` loses the rows whose state never arrived, and everything that
survives is still paired exactly:

```
  profile           rows    pair   unanc  st.pub  fr.pub    drop   stale     bad
  clean              120     120       0     225     225       1       1       0
  jitter             120     120       0     224     224       1       6       0
  camera-drops       121     121       0     221     166       1      48       0
  state-drops         98      98       0     175     221       0       0       0
  stalls             120     120       0     175     175       2      11       2
```

The `stalls` row is a known regression since pre-0.3 Portal `main`, still present in 0.3.0rc1: right after a stall
the robot publishes two ticks a few hundred microseconds apart, and one frame
arrives carrying the other's timestamp. The same profile is clean on Portal
0.2.5. See [Known issues](../docs/operations.md#known-issues).

`bad` is the column that must stay zero. `pair` falling is degradation
doing what degradation does; `bad` rising would be the design failing.
