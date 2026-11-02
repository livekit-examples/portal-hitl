# Working on this tutorial

This repo teaches the full HITL loop with Portal: collect demonstrations, train
ACT, hand off between policy and human, and record corrections for retraining.
Keep it small and readable. The README leads with the numbered `tutorial/`.

## Structure

- `robot.py`, `teleoperator.py`, and `recorder.py` show the Portal flows.
- `_common.py` holds shared environment, token, and scheduling helpers.
- `utils/` contains hardware, preview, hotkey, and task-prompt details.
- `policies/act/` contains training, inference, and a SkyPilot example.
- `test/` checks behavior without hardware.

Use plain scripts (`uv run robot.py`), not a package or console entry points.
Keep explanations in the tutorials rather than creating a separate docs layer.
Update affected chapters with behavior changes.

## Rules

- All peers load `portal.yaml`; do not duplicate schema field lists in Python.
- Model tensors use training dataset order, read from `meta/info.json`.
- Control is explicit: `c` selects an operator. No background auto-claim loop.
- Keep recording separate from the control loops. Preserve action anchors and
  both archive timelines; let Portal stamp synchronized action send times.
- Run blocking hardware or inference off the event loop. Device calls must be
  serialized and finished before disconnecting hardware.
- Preserve camera/CAN recovery and the target-step limiter. A small clamp on
  tracking error limits torque under MIT control and can immobilize joints.
- Check installed library APIs when restoring older examples.

## Checks

```bash
uv run --group train python -m unittest test.test_act
uv run python -m test.test_teleop
uv run python -m test.test_resilience
# With livekit-server --dev:
uv run python -m test.test_pubsub
uv run python -m test.test_recorder
uv run python -m test.test_handoff
```
