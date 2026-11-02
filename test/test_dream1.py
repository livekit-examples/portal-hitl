"""Dream-1 observation and action mapping; no robot, network, or API key required."""
import base64
import json
import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np
from livekit.portal import OperatorConfig

from _common import CONFIG_PATH
from policies.dream1.inference import (
    GRIPPER, camera_roles, chunk_rows, embodiment, encode_frame, load_rest_pose, step_toward, to_model, to_robot,
)


class Dream1Tests(unittest.TestCase):
    def setUp(self):
        cfg = OperatorConfig.from_yaml_file(CONFIG_PATH, "test")
        self.joints = [field.name for field in cfg.action_schema if field.name != GRIPPER]

    def test_arm_file_needs_a_confirmed_tool_center_point(self):
        arm = {"rest": [0.0] * len(self.joints)}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "arm.json"
            path.write_text(json.dumps(arm))
            with self.assertRaisesRegex(ValueError, "dreamcli tcp"):
                load_rest_pose(path)
            arm["tcp_calibration"] = {"status": "skipped"}
            path.write_text(json.dumps(arm))
            with self.assertRaisesRegex(ValueError, "not confirmed"):
                load_rest_pose(path)
            arm["tcp_calibration"] = {"status": "calibrated"}
            path.write_text(json.dumps(arm))
            self.assertEqual(load_rest_pose(path), arm["rest"])

    def test_embodiment_spans_are_contiguous(self):
        state = embodiment(6)["state"]
        self.assertEqual(state["right_arm"], [0, 6])
        self.assertEqual(state["left_eef_rot6"][1], 2 * (6 + 1 + 3 + 6))

    def test_camera_roles(self):
        self.assertEqual(camera_roles("wrist_r=arm_camera", ["arm_camera"]), {"wrist_r": "arm_camera"})
        for spec in ("top=arm_camera", "wrist_r=missing", ""):
            with self.assertRaises(ValueError):
                camera_roles(spec, ["arm_camera"])

    def test_frame_is_cropped_resized_and_bgr(self):
        rgb = np.zeros((480, 640, 3), np.uint8)
        rgb[:60] = 255  # outside the 16:9 crop
        rgb[60:420, :, 0] = 255  # red inside it
        jpeg = np.frombuffer(base64.b64decode(encode_frame(rgb)), np.uint8)
        bgr = cv2.imdecode(jpeg, cv2.IMREAD_COLOR)
        self.assertEqual(bgr.shape, (176, 320, 3))
        self.assertGreater(bgr[88, 160, 2], 200)  # red stays red
        self.assertLess(bgr[2, 160, 1], 50)  # the white band was cropped away

    def test_units_round_trip(self):
        state = {key: 10.0 * i for i, key in enumerate(self.joints)} | {GRIPPER: -135.0}
        row = to_model(state, self.joints, -270.0)
        self.assertAlmostEqual(row[1], np.radians(10.0))
        self.assertAlmostEqual(row[-1], 0.5)
        action = to_robot(row, self.joints, -270.0)
        for key, value in state.items():
            self.assertAlmostEqual(action[key], value)

    def test_step_toward_limits_each_entry(self):
        out = step_toward(np.zeros(3), np.array([1.0, -1.0, 1.0]), np.array([0.1, 0.1, 0.02]))
        np.testing.assert_allclose(out, [0.1, -0.1, 0.02])

    def test_chunk_rows_rejects_bad_replies(self):
        self.assertEqual(chunk_rows({"q_r_chunk": np.zeros((48, 7))}, 6).shape, (48, 7))
        for bad in (np.zeros((48, 6)), np.full((48, 7), np.nan)):
            with self.assertRaises(ValueError):
                chunk_rows({"q_r_chunk": bad}, 6)


if __name__ == "__main__":
    unittest.main()
