"""Real transport shaping for the local LiveKit link, via pf + dummynet.

    sudo uv run python -m test.netsim wan       # apply a profile
    sudo uv run python -m test.netsim off       # remove it
    uv run python -m test.netsim status         # inspect (no sudo needed)
    uv run python -m test.netsim show wan       # print the commands, apply nothing

`test/test_degraded.py` degrades the *publisher*, which needs no
privileges but cannot model the wire. This shapes the actual sockets --
delay, jitter, loss and bandwidth on LiveKit's signaling (TCP 7880),
RTC/TCP fallback (7881) and the media + data-channel path (UDP 7882) --
so Portal's synchronizer sees a genuinely degraded link.

SAFETY. This edits the system packet filter, which other software on the
machine may also be using (VPNs, Tailscale, Docker, firewall tools).
Before changing anything it writes the current main ruleset and pf's
enabled/disabled state to STATE_FILE, and `off` restores both -- including
after a crash, since the file survives the process. Our rules live in
their own anchor so the main ruleset is only appended to, never replaced.
"""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys

ANCHOR = "portalsim"
PORTS = "7880:7882"
PIPE = "1"
STATE_FILE = pathlib.Path("/tmp/portal-netsim-state.json")

# delay is one-way ms; plr is packet loss ratio; bw caps throughput.
PROFILES = {
    "lan":       dict(delay=1,   plr=0.0,    bw="1Gbit/s",  note="same-building baseline"),
    "wifi":      dict(delay=10,  plr=0.001,  bw="100Mbit/s", note="decent wifi"),
    "wan":       dict(delay=60,  plr=0.005,  bw="50Mbit/s",  note="cross-country teleop"),
    "bad-wan":   dict(delay=150, plr=0.02,   bw="10Mbit/s",  note="congested intercontinental"),
    "satellite": dict(delay=600, plr=0.01,   bw="5Mbit/s",   note="the geography argument, tested"),
}


def run(cmd: list[str], check: bool = True, stdin: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, input=stdin, text=True, capture_output=True, check=check)


def anchor_rules() -> str:
    return (f'dummynet in quick proto {{tcp,udp}} from any to any port {PORTS} pipe {PIPE}\n'
            f'dummynet out quick proto {{tcp,udp}} from any to any port {PORTS} pipe {PIPE}\n')


def pipe_cmd(p: dict) -> list[str]:
    return ["dnctl", "pipe", PIPE, "config",
            "delay", str(p["delay"]), "plr", str(p["plr"]), "bw", p["bw"]]


def show(name: str) -> int:
    p = PROFILES[name]
    print(f"# profile '{name}': {p['note']}")
    print(f"# {p['delay']}ms one-way delay, {p['plr']:.1%} loss, {p['bw']} cap "
          f"(round trip ~{p['delay']*2}ms)")
    print(" ".join(pipe_cmd(p)))
    print(f'# append to the main ruleset:  dummynet-anchor "{ANCHOR}"  /  anchor "{ANCHOR}"')
    print(f"# load into the anchor:  pfctl -a {ANCHOR} -f -")
    print(anchor_rules().rstrip())
    print("pfctl -E")
    return 0


def status() -> int:
    info = run(["pfctl", "-s", "info"], check=False)
    enabled = "Status: Enabled" in info.stdout
    print(f"pf: {'ENABLED' if enabled else 'disabled'}")
    if STATE_FILE.exists():
        saved = json.loads(STATE_FILE.read_text())
        print(f"netsim: ACTIVE (profile '{saved.get('profile')}'), saved state at {STATE_FILE}")
        print("  remove with:  sudo uv run python -m test.netsim off")
    else:
        print("netsim: not active")
    rules = run(["pfctl", "-a", ANCHOR, "-s", "rules"], check=False)
    if rules.stdout.strip():
        print(f"anchor '{ANCHOR}' rules:")
        for line in rules.stdout.strip().splitlines():
            print("   ", line)
    pipes = run(["dnctl", "list"], check=False)
    if pipes.stdout.strip():
        print("dummynet pipes:")
        for line in pipes.stdout.strip().splitlines():
            print("   ", line)
    return 0


def apply(name: str) -> int:
    p = PROFILES[name]
    if STATE_FILE.exists():
        print(f"netsim already active; run `off` first (state: {STATE_FILE})", file=sys.stderr)
        return 1

    info = run(["pfctl", "-s", "info"], check=False)
    was_enabled = "Status: Enabled" in info.stdout
    saved_rules = run(["pfctl", "-sr"], check=False).stdout
    STATE_FILE.write_text(json.dumps(
        {"profile": name, "was_enabled": was_enabled, "rules": saved_rules}, indent=1))
    print(f"saved pf state to {STATE_FILE} (pf was {'enabled' if was_enabled else 'disabled'})")

    run(pipe_cmd(p))
    print(f"configured pipe {PIPE}: {p['delay']}ms delay, {p['plr']:.1%} loss, {p['bw']}")

    # Append our anchors to the existing ruleset rather than replacing it.
    main = saved_rules
    if f'dummynet-anchor "{ANCHOR}"' not in main:
        main += f'\ndummynet-anchor "{ANCHOR}"\nanchor "{ANCHOR}"\n'
    run(["pfctl", "-f", "-"], stdin=main)
    run(["pfctl", "-a", ANCHOR, "-f", "-"], stdin=anchor_rules())
    if not was_enabled:
        run(["pfctl", "-E"], check=False)
    print(f"applied '{name}' to ports {PORTS} (tcp+udp)")
    print(f"round-trip added: ~{p['delay']*2}ms.  Remove with: sudo uv run python -m test.netsim off")
    return 0


def off() -> int:
    run(["pfctl", "-a", ANCHOR, "-F", "rules"], check=False)
    run(["dnctl", "-q", "flush"], check=False)
    if STATE_FILE.exists():
        saved = json.loads(STATE_FILE.read_text())
        run(["pfctl", "-f", "-"], stdin=saved["rules"], check=False)
        if not saved.get("was_enabled"):
            run(["pfctl", "-d"], check=False)
            print("pf disabled (restored to its prior state)")
        STATE_FILE.unlink()
        print(f"restored pf ruleset from {STATE_FILE}")
    else:
        print("no saved state; flushed the anchor and pipes only")
    return 0


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "status"
    if cmd == "status":
        return status()
    if cmd == "off":
        return off()
    if cmd == "show":
        return show(argv[2] if len(argv) > 2 else "wan")
    if cmd in PROFILES:
        return apply(cmd)
    print(f"usage: {argv[0]} [{'|'.join(PROFILES)}|off|status|show <profile>]", file=sys.stderr)
    print("\nprofiles:", file=sys.stderr)
    for n, p in PROFILES.items():
        print(f"  {n:10s} {p['delay']:>4}ms  loss {p['plr']:<6.1%} {p['bw']:>10s}   {p['note']}",
              file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
