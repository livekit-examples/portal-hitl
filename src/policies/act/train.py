#!/usr/bin/env python
"""Train ACT on a converted LeRobot dataset, resuming saved runs when available."""
from __future__ import annotations

import os
import runpy
import sys
from pathlib import Path


def find_resume_config(output_dir: Path) -> Path | None:
    checkpoints = output_dir / "checkpoints"
    if not checkpoints.is_dir():
        return None
    required = (
        "pretrained_model/train_config.json",
        "pretrained_model/model.safetensors",
        "pretrained_model/policy_preprocessor.json",
        "pretrained_model/policy_postprocessor.json",
        "training_state/training_step.json",
        "training_state/optimizer_state.safetensors",
        "training_state/optimizer_param_groups.json",
        "training_state/rng_state.safetensors",
    )
    candidates = sorted((p for p in checkpoints.iterdir() if p.is_dir() and p.name.isdigit()),
                        key=lambda p: int(p.name), reverse=True)
    for checkpoint in candidates:
        if all((checkpoint / name).is_file() for name in required):
            return checkpoint / "pretrained_model" / "train_config.json"
    return None


def training_args(env) -> list[str]:
    steps = int(env.get("NUM_STEPS", "100000"))
    batch = int(env.get("BATCH_SIZE", "64"))
    chunk = int(env.get("CHUNK_SIZE", "50"))
    lr = env.get("LR", "5e-5")
    if min(steps, batch, chunk) < 1:
        raise ValueError("NUM_STEPS, BATCH_SIZE, and CHUNK_SIZE must be positive")
    run_name = f"{env.get('RUN_NAME', 'act-rebot-b601')}_steps{steps}_bs{batch}_lr{lr}_chunk{chunk}"
    output = Path(env.get("OUTPUT_DIR", f"outputs/{run_name}"))
    resume = find_resume_config(output)
    if resume:
        return ["--resume=true", f"--config_path={resume}", f"--output_dir={output}"]
    if output.exists():
        raise FileExistsError(f"No resumable checkpoint in {output}; choose a new OUTPUT_DIR")
    interval = max(steps // 4, 1000)
    args = [
        "--policy.type=act",
        f"--dataset.repo_id={env.get('DATASET_REPO_ID', 'local/portal-hitl')}",
        f"--dataset.video_backend={env.get('VIDEO_BACKEND', 'pyav')}",
        f"--output_dir={output}",
        f"--policy.device={env.get('DEVICE', 'cuda')}",
        "--policy.use_amp=true",
        f"--policy.chunk_size={chunk}",
        f"--policy.n_action_steps={chunk}",
        f"--policy.optimizer_lr={lr}",
        "--policy.optimizer_lr_backbone=1e-5",
        f"--steps={steps}",
        f"--batch_size={batch}",
        f"--num_workers={env.get('NUM_WORKERS', '16')}",
        "--prefetch_factor=4",
        "--persistent_workers=true",
        "--tolerance_s=0.5",
        "--log_freq=100",
        f"--save_freq={interval}",
        "--env_eval_freq=0",
        "--policy.push_to_hub=false",
    ]
    if root := env.get("DATASET_ROOT"):
        args.append(f"--dataset.root={root}")
    return args


def main() -> None:
    import lerobot.common.train_utils as train_utils

    original = train_utils.update_last_checkpoint

    def update_last(checkpoint_dir):
        # Object-storage mounts may not support the optional "last" symlink.
        try:
            return original(checkpoint_dir)
        except OSError as exc:
            print(f"[train] could not update last symlink: {exc}")

    train_utils.update_last_checkpoint = update_last
    sys.argv[1:] = training_args(os.environ)
    runpy.run_module("lerobot.scripts.lerobot_train", run_name="__main__", alter_sys=True)


if __name__ == "__main__":
    main()
