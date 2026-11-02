"""ACT dataset mapping and training setup; no checkpoint or hardware required."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import torch
from livekit.portal import OperatorConfig

from policies.act.inference import action_dict, build_batch, dataset_fields
from policies.act.train import find_resume_config, training_args
from _common import CONFIG_PATH


class ActTests(unittest.TestCase):
    def test_dataset_order_and_schema_validation(self):
        cfg = OperatorConfig.from_yaml_file(CONFIG_PATH, "test")
        names = list(reversed([f.name for f in cfg.state_schema]))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "meta").mkdir()
            info = {"features": {key: {"names": names} for key in ("observation.state", "action")}}
            (root / "meta/info.json").write_text(json.dumps(info))
            self.assertEqual(dataset_fields(root, cfg), (names, names))
            info["features"]["action"]["names"] = ["wrong"]
            (root / "meta/info.json").write_text(json.dumps(info))
            with self.assertRaises(ValueError):
                dataset_fields(root, cfg)

    def test_batch_and_action_mapping(self):
        pixels = np.full((2, 3, 3), 255, dtype=np.uint8)
        frame = SimpleNamespace(data=pixels.tobytes(), width=3, height=2)
        obs = SimpleNamespace(state={"a": 1, "b": 2}, frames={"wrist": frame})
        batch = build_batch(obs, ["wrist"], ["b", "a"], "Pick")
        self.assertEqual(batch["observation.state"].tolist(), [2, 1])
        self.assertEqual(tuple(batch["observation.images.wrist"].shape), (3, 2, 3))
        self.assertEqual(batch["observation.images.wrist"].max().item(), 1)
        self.assertEqual(action_dict(torch.tensor([[3., 4.]]), ["b", "a"]), {"b": 3., "a": 4.})
        for values in ([[1.]], [[float("nan"), 2.]]):
            with self.assertRaises(ValueError):
                action_dict(torch.tensor(values), ["a", "b"])

    def test_training_configuration_and_existing_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "run"
            env = {"OUTPUT_DIR": str(output), "DATASET_ROOT": "data/demo", "NUM_STEPS": "20000"}
            args = training_args(env)
            self.assertIn("--dataset.root=data/demo", args)
            self.assertIn("--save_freq=5000", args)
            self.assertIn("--policy.type=act", args)
            output.mkdir()
            sentinel = output / "keep.txt"
            sentinel.write_text("keep")
            with self.assertRaises(FileExistsError):
                training_args(env)
            self.assertEqual(sentinel.read_text(), "keep")
            self.assertIsNone(find_resume_config(output))

    def test_resume_skips_partial_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            checkpoint = output / "checkpoints/001000"
            required = [
                "pretrained_model/train_config.json", "pretrained_model/model.safetensors",
                "pretrained_model/policy_preprocessor.json", "pretrained_model/policy_postprocessor.json",
                "training_state/training_step.json", "training_state/optimizer_state.safetensors",
                "training_state/optimizer_param_groups.json", "training_state/rng_state.safetensors",
            ]
            for name in required:
                path = checkpoint / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch()
            (output / "checkpoints/002000").mkdir()
            expected = checkpoint / "pretrained_model/train_config.json"
            self.assertEqual(find_resume_config(output), expected)
            args = training_args({"OUTPUT_DIR": str(output)})
            self.assertIn("--resume=true", args)
            self.assertIn(f"--config_path={expected}", args)
            self.assertIn(f"--output_dir={output}", args)

    def test_training_flags_parse_with_installed_lerobot(self):
        import draccus
        from lerobot.configs.train import TrainPipelineConfig
        with tempfile.TemporaryDirectory() as tmp:
            args = training_args({"OUTPUT_DIR": str(Path(tmp) / "run"), "DEVICE": "cpu"})
            cfg = draccus.parse(TrainPipelineConfig, args=args)
            self.assertEqual(cfg.policy.type, "act")
            self.assertEqual(cfg.policy.chunk_size, 50)
            self.assertTrue(cfg.persistent_workers)


if __name__ == "__main__":
    unittest.main()
