"""Hardware-free validation against a local `livekit-server --dev`."""
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

# Must precede the first huggingface_hub import, so it lives here rather
# than in a test script: this package's __init__ runs before any of them.
os.environ.setdefault("HF_HUB_OFFLINE", "1")

os.environ.setdefault("LIVEKIT_URL", "ws://localhost:7880")
os.environ.setdefault("LIVEKIT_API_KEY", "devkey")
os.environ.setdefault("LIVEKIT_API_SECRET", "secret")
