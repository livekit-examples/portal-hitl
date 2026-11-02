# Hardware-free checks

Run from the repo root. ACT checks need the training dependency group:

```bash
uv run --group train python -m unittest test.test_act
uv run --group dream python -m unittest test.test_dream1
uv run python -m test.test_teleop
uv run python -m test.test_resilience
```

For transport and archive checks, start `livekit-server --dev` in another
terminal. Test credentials default to that local server.

```bash
uv run python -m test.test_pubsub
uv run python -m test.test_recorder
uv run python -m test.test_handoff
uv run python -m test.test_degraded
```

| Suite | Checks |
|---|---|
| `test_act` | Dataset field order, tensor mapping, training arguments, existing-output handling |
| `test_dream1` | Arm-file checks, camera roles, frame crop, unit conversion, target ramp |
| `test_teleop` | Control cycling, frame anchors, task selection, keyboard input, preview |
| `test_resilience` | CAN failure recovery and target-step limits |
| `test_pubsub` | Discovery, control gate, observation transport |
| `test_recorder` | Episode markers and frame/state/action alignment after an archive round trip |
| `test_handoff` | Human → policy → human actions recorded in one episode |
| `test_degraded` | Alignment under jitter, drops, and stalls |

Synthetic peers encode tick indices in joint state, a white bar in video, and
returned actions. The archive reader checks they agree using exact timestamp
selection (`max_stale_us=0`). Normal conversion permits 200 ms of staleness.

Portal 0.3.0rc1 has a known timestamp regression after closely spaced ticks in
the `stalls` profile: a frame can carry its neighbour’s timestamp. That profile
may fail even when the other suites pass. A mismatched row is a failure, not an
acceptable effect of network degradation.

`PORTAL_TEST_PROFILE` selects one degradation profile; `PORTAL_TEST_CODEC`
overrides the video codec; `PORTAL_TEST_KEEP_RRD` retains test archives.
`netsim.py` provides optional macOS pf/dummynet transport shaping. Inspect
`uv run python -m test.netsim show wan` before applying it: it modifies the
system packet filter and requires sudo for changes.
