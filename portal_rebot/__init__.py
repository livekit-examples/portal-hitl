"""Teleop data collection on LiveKit Portal.

`livekit.portal` reads `RUST_LOG` when its shared library loads -- the
`from livekit.portal import ...` at the top of each `__main__.py`, long before
`main()` runs -- so it has to be set here and not in `load_env()`. The two
silenced sources are noise for this rig specifically: `sync_buffer`'s
[sync-drop] warnings describe the synchronizer path nothing here consumes, and
libwebrtc's h264 receive-queue chatter arrives at frame rate under any load.
`RUST_LOG` from the shell still wins.
"""
import os

os.environ.setdefault(
    "RUST_LOG",
    "warn,livekit_portal::sync_buffer=error,libwebrtc=error",
)
