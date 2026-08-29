#!/usr/bin/env python3
"""
vast_optimizer.py - keeps rented compute on vast.ai cheap.

    python3 vast_optimizer.py status             # what runs, what it costs
    python3 vast_optimizer.py check              # compute, change nothing
    python3 vast_optimizer.py check --vram 48    # target: 48 GB of VRAM
    python3 vast_optimizer.py run                # switch offers when it pays
    python3 vast_optimizer.py run --cap 0.9
    python3 vast_optimizer.py rent --vram 48 --yes --task

The optimizer manages, it does not shop. With no instance running it will at
most name the best offer - nothing is started. It only switches above 11 %
savings, because every switch costs a quarter hour of loading plus the risk
of not coming up; below that the bookkeeping costs more than it saves.

VRAM is counted as a sum
------------------------
What is searched for is not a card but an amount of memory. 48 GB are met by
two 24 GB cards just as well as by a single 48 GB card, and the pair is
usually far cheaper. That is why num_gpus == 1 appears nowhere: the search
runs over several GPU counts and what is checked is num_gpus * gpu_ram.
llama-server spreads the model across all visible cards by itself with
-ngl 99, and an instance's container always sees all of them.

Interruptible offers
--------------------
Bid offers (interruptible) are allowed and often half the price. Whoever is
outbid loses the instance - so such an offer only counts when it clears the
same 11 %. The price used is not min_bid but our own bid: min_bid plus a 15 %
markup, so that the next cent does not cost us the machine.

Why always the new one first, then the old one
----------------------------------------------
A new instance downloads 18 GB of model and needs minutes before it answers.
Destroying the old one first leaves nothing at all during that time, and if
the new one fails to come up, both are gone. So: start the new one, wait for
HTTP 200 from /health, and only then destroy the old one. If the new one
never becomes healthy it is destroyed and the old one keeps running.
"""

import argparse
import csv
import json
import math
import os
import re
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

VASTAI = "/home/gh/venv_vastai/bin/vastai"
# The key allowed to rent. Without this file vastai falls back to its own key
# in ~/.config/vastai/vast_api_key, which belongs to the host account and may
# only rent its own machines.
CLIENT_KEY_FILE = "/home/gh/.config/vastai/vast_api_key_kunde"
TOKEN_FILE = os.path.expanduser("~/.config/llm_fern/api_key")
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "state.json")

# Everything produced while setting up a rented machine lives in one place: a
# running log plus one timestamped file per rental - from signing the contract
# through the model download to the first answer. When a machine fails to come
# up, that file is where to look.
VAST_DIR = "/home/gh/vast"
LOGFILE = os.path.join(VAST_DIR, "optimizer.log")

# Same setup as gpu_mieten.py - a prebuilt image, because compiling on a
# machine that bills by the hour is wasted money, and -hf because the instance
# sits on a ~900 Mbit/s line.
IMAGE = "ghcr.io/ggml-org/llama.cpp:server-cuda"
MODEL = "unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF:Q4_K_M"
PORT = 8080

THRESHOLD = 0.11         # more than 11 % cheaper, otherwise no switch
# What a bid is worth is decided by the guaranteed price, not by min_bid: the
# bid is half of what the same class costs as a guaranteed instance, and never
# less than min_bid, which vast.ai rejects outright. Nothing is added on top of
# min_bid - paying a quarter above the minimum is paying for nothing, and if
# the bid loses, the attempt list walks on and ends at a guaranteed machine
# anyway. That is the cheaper way to lose a bid than to overpay every hour.
BID_OF_ONDEMAND = 0.5
BID_MARKUP = 1.0         # no surcharge on min_bid when no reference is known
_ondemand_ref = 0.0      # cheapest guaranteed $/h for the current target
TARGET_VRAM = 24         # GB in total, change with --vram
PRICE_CAP = 0.60         # $/h, change with --cap
DISK_GB = 60
CONTEXT = 32768
MIN_INET = 200           # Mbit/s
MIN_RELIABILITY = 0.97
MIN_HOLD_MIN = 45        # minutes of quiet after a switch
SWITCHES_PER_HOUR = 1
# The server loads the model from local disk, that is minutes at most. The
# long wait was only ever needed because -hf downloaded during startup.
HEALTH_TIMEOUT_S = 8 * 60
HEALTH_POLL_S = 30
GPU_COUNTS = (1, 2, 3, 4, 6, 8)
# An offer whose instance died during setup is remembered and skipped for a
# day. Without that the search picks the cheapest offer again a minute later -
# and the cheapest offer is exactly the one that just failed.
BAD_OFFER_HOURS = 24

# SSH into the rented machine. Without it the only view inside is
# `vastai logs`, which returns a stale snapshot - during an 18 GB download
# that means flying blind. SSH needs two things: a key registered on the
# account, and an instance created with --ssh, because in --args mode
# llama-server is the entrypoint and vast's sshd never runs.
SSH_KEY = "/home/gh/.ssh/vastai"
SSH_OPTS = ["-i", SSH_KEY, "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR",
            "-o", "ConnectTimeout=15", "-o", "BatchMode=yes"]
SERVER_LOG = "/var/log/llama-server.log"
# In the llama.cpp image the binary is not on PATH, it sits in /app.
SERVER_BIN = "/app/llama-server"

# The model is fetched by our own downloader instead of llama-server's -hf.
# Reason, measured twice today: -hf pulls through a single connection, and
# Hugging Face throttles per connection - two rentals sat 22 minutes at zero
# progress. download_llm.py uses many short-lived connections, which is the
# whole point of it.
DOWNLOADER = "/home/gh/download_llm.py"
MODEL_URL = ("https://huggingface.co/unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF"
             "/resolve/main/Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf")
MODEL_PATH = "/root/models/qwen3-coder-30b-a3b-q4km.gguf"
MODEL_BYTES = 18556689568
SSH_WAIT_S = 10 * 60
# The download decides whether a machine is worth anything, and it shows its
# hand within a minute: sample the file after 10 s and again after 40 s. A box
# that pulls at 20 Mbit/s needs two hours for the model and has to go now, not
# after a 25 minute timeout that costs the whole time.
DOWNLOAD_PROBE_S = 10
DOWNLOAD_MEASURE_S = 30
MIN_DOWNLOAD_MBIT = 100.0
DOWNLOAD_LOG = "/root/download.log"

# The build task handed to the rented machine once it stands.
BUILD_DRIVER = "/home/gh/bau_treiber.sh"
RENT_PROJECT = "/home/gh/vast_optimizer_miet"

# The work the rented GPU is for: 256k unanalysed posts in wagodb.nitter_content
# on heissa.de. The analyzer runs there, the model runs on the rented machine,
# so the endpoint has to be pushed over whenever it changes - an interruptible
# instance can be gone at any moment.
WORKER_HOST = "gh@heissa.de"
WORKER_CMD = ("setsid nohup python3 /home/gh/python/content_analyzer.py "
              "--llm-url {endpoint}/v1/chat/completions --workers 16 "
              "> /home/gh/python/analyzer_gpu.log 2>&1 < /dev/null &")
WORKER_CHECK = "pgrep -f 'content_analyzer.py --llm-url' | head -1"


# ------------------------------------------------------------- outside world

def client_key() -> list[str]:
    """--api-key before the subcommand, when we have one of our own."""
    try:
        k = open(CLIENT_KEY_FILE).read().strip()
    except OSError:
        return []
    return ["--api-key", k] if k else []


def vast(*args: str) -> str:
    """The only way out. Everything passes through here so the tests have to
    replace exactly one place and never spend real money."""
    e = subprocess.run([VASTAI, *client_key(), *args],
                       capture_output=True, text=True)
    if e.returncode != 0:
        raise RuntimeError(f"vastai {' '.join(args)}: {e.stderr.strip()[:400]}")
    return e.stdout


def vast_json(*args: str):
    raw = vast(*args)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        raise RuntimeError("unreadable answer from vastai: " + raw[:300])


def token() -> str:
    """The llama-server access token. A rented GPU sits in the open internet;
    without --api-key anyone could use it. If the token is missing it is
    created here - an empty --api-key in the launch command would be worse
    than none, because then the next argument slides into its place."""
    if os.path.exists(TOKEN_FILE):
        t = open(TOKEN_FILE).read().strip()
        if t:
            return t
    os.makedirs(os.path.dirname(TOKEN_FILE), mode=0o700, exist_ok=True)
    t = secrets.token_urlsafe(32)
    with open(TOKEN_FILE, "w") as f:
        f.write(t)
    os.chmod(TOKEN_FILE, 0o600)
    report(f"  access token created: {TOKEN_FILE}")
    return t


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def report(text: str, logfile: str | None = None) -> None:
    """Writes one timestamped line into the running log and, when a setup file
    is given, into that as well."""
    line = f"{now_utc().astimezone().strftime('%Y-%m-%d %H:%M:%S')}  {text}"
    print(line)
    for target in (LOGFILE, logfile):
        if not target:
            continue
        try:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "a") as f:
                f.write(line + "\n")
        except OSError:
            pass  # a missing log must never block a switch


def setup_logfile(instance_id: int | str) -> str:
    """One file per setup, named after time and instance."""
    stamp = now_utc().astimezone().strftime("%Y%m%d-%H%M%S")
    return os.path.join(VAST_DIR, f"setup_{stamp}_{instance_id}.log")


def container_log(instance_id: int, lines: int = 12) -> str:
    """Fetches the log from inside the container. While llama-server pulls the
    18 GB of model this is the only place where progress shows at all - from
    the outside the instance merely says 'loading'."""
    try:
        raw = vast("logs", str(instance_id), "--tail", str(lines))
    except RuntimeError as e:
        return f"(no container log: {str(e)[:120]})"
    return "\n".join(f"      | {z}" for z in raw.strip().splitlines()[-lines:])


# ------------------------------------------------------------------ measuring

def vram_gb(d: dict) -> float:
    """Total VRAM in GB. vast.ai reports gpu_ram per card in MB, hence times
    the card count. gpu_total_ram exists too but is not filled in every
    answer - so it serves only as a fallback."""
    per_card = float(d.get("gpu_ram") or 0)
    cards = int(d.get("num_gpus") or 1)
    total = per_card * cards
    if not total:
        total = float(d.get("gpu_total_ram") or 0)
    return total / 1024.0


def price(offer: dict, ondemand_ref: float | None = None) -> float:
    """What the hour really costs.

    For fixed offers that is dph_total. For bid offers dph_total is only the
    host's base rate - what gets paid is our own bid: half the guaranteed
    price of the same class, but never below min_bid, or vast.ai rejects it
    outright. The rest of dph_total (disk, bandwidth) is charged anyway and
    stays in."""
    total = float(offer.get("dph_total") or 0)
    if not offer.get("interruptible"):
        return total
    base = float(offer.get("dph_base") or 0)
    around = max(0.0, total - base)
    floor = float(offer.get("min_bid") or base)
    ref = _ondemand_ref if ondemand_ref is None else ondemand_ref
    bid = max(floor, ref * BID_OF_ONDEMAND) if ref else floor * BID_MARKUP
    # A bid above the guaranteed price would be absurd: then the machine that
    # cannot be outbid at all is the cheaper one.
    if ref and bid > ref:
        bid = ref
    return bid + around


def suitable(offer: dict, target_vram: float, cap: float) -> tuple[bool, str]:
    """An offer is suitable when it can take over the running instance's work.
    The price comparison comes later - here it is only about whether the model
    fits on it at all."""
    if vram_gb(offer) + 1e-9 < target_vram:
        return False, (f"only {vram_gb(offer):.0f} GB on "
                       f"{offer.get('num_gpus')} cards")
    if not offer.get("rentable", True):
        return False, "not rentable"
    if float(offer.get("disk_space") or 0) < DISK_GB:
        return False, "not enough disk"
    if float(offer.get("inet_down") or 0) < MIN_INET:
        return False, "too slow on the network"
    if float(offer.get("reliability2") or 0) < MIN_RELIABILITY:
        return False, "not reliable enough"
    if price(offer) > cap:
        return False, f"above the cap ({price(offer):.3f} $/h)"
    return True, "suitable"


# ------------------------------------------------------------------ searching

def _query(cards: int, target_vram: float) -> str:
    """vast.ai's query language takes gpu_ram in GB while the answer comes in
    MB - a trap one overlooks exactly once."""
    per_card = math.ceil(target_vram / cards)
    return (f"num_gpus={cards} gpu_ram>={per_card} "
            f"disk_space>={DISK_GB} inet_down>={MIN_INET} "
            f"reliability>{MIN_RELIABILITY} rentable=true")


def _recent(entries: dict | None, now: datetime) -> set:
    out = set()
    for key, when in (entries or {}).items():
        t = _as_time(when)
        if t and now - t < timedelta(hours=BAD_OFFER_HOURS):
            out.add(int(key))
    return out


def bad_offers(st: dict | None = None, now: datetime | None = None) -> set:
    """Offer ids that failed recently."""
    return _recent((state_read() if st is None else st).get("bad_offers"),
                   now or now_utc())


def bad_machines(st: dict | None = None, now: datetime | None = None) -> set:
    """Machine ids that failed recently. The machine is what stays: an offer
    disappears the moment somebody rents it and comes back under a new id,
    while the broken box behind it stays broken. Machine 46751059's box took
    two rentals today, both dead within minutes."""
    return _recent((state_read() if st is None else st).get("bad_machines"),
                   now or now_utc())


def mark_bad_offer(offer_id: int, reason: str, machine_id: int | None = None,
                   report_it: bool = False) -> None:
    st = state_read()
    for feld, wert in (("bad_offers", offer_id), ("bad_machines", machine_id)):
        if wert is None:
            continue
        bad = dict(st.get(feld) or {})
        bad[str(wert)] = now_utc().isoformat()
        # Keep the list short - the newest twenty are enough to stop a loop.
        st[feld] = dict(sorted(bad.items(), key=lambda kv: kv[1])[-20:])
    state_write(st)
    report(f"  offer {offer_id}"
           f"{f' (machine {machine_id})' if machine_id else ''} noted as bad "
           f"for {BAD_OFFER_HOURS} h: {reason}")
    if report_it and machine_id:
        report_machine(machine_id, reason)


def report_machine(machine_id: int, reason: str) -> bool:
    """Tells vast.ai what happened, with a reason.

    Kept, but off by default: /machines/<id>/reports/ answers GET and HEAD
    only, POST and PUT both come back 404 "predicate mismatch". There is no
    public way to file a complaint, so the blacklist and the reason in
    vast_rentals are what we have."""
    key = ""
    try:
        key = open(CLIENT_KEY_FILE).read().strip()
    except OSError:
        pass
    if not key:
        return False
    body = json.dumps({
        "machine_id": machine_id,
        "reason": (f"Instance exited within minutes of creation, twice, "
                   f"before sshd came up: {reason}. Image "
                   f"{IMAGE}, launched with --ssh --direct and an entrypoint "
                   f"that only sleeps, so nothing of ours could have ended "
                   f"the container. Same image starts and runs normally on "
                   f"other machines.")}).encode()
    req = urllib.request.Request(
        f"https://console.vast.ai/api/v0/machines/{machine_id}/reports/",
        data=body, method="POST",
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            report(f"  machine {machine_id} reported to vast.ai (HTTP {r.status})")
            return True
    except urllib.error.HTTPError as e:
        report(f"  report for machine {machine_id} refused: HTTP {e.code} "
               f"{e.read()[:160].decode('utf-8', 'replace')}")
    except Exception as e:
        report(f"  report for machine {machine_id} failed: {str(e)[:120]}")
    return False


def offers(target_vram: float, cap: float, with_bids: bool = True) -> list:
    """Searches across several GPU counts. A single query would miss either
    the pairs or the big single cards: 48 GB exists as 1x48, 2x24, 4x12 - and
    the cheapest variant is usually not the one that comes to mind first."""
    global _ondemand_ref
    found: dict = {}
    skip = bad_offers()
    skip_machines = bad_machines()
    # Guaranteed offers are searched first: their cheapest price is the
    # yardstick every bid is measured against.
    kinds = [("on-demand", False)] + ([("bid", True)] if with_bids else [])
    _ondemand_ref = 0.0
    for kind, is_bid in kinds:
        for cards in GPU_COUNTS:
            if cards * 200 < target_vram:   # no card has 200 GB
                continue
            try:
                raw = vast_json("search", "offers", _query(cards, target_vram),
                                "--type", kind, "-o", "dph_total", "--raw")
            except RuntimeError as e:
                report(f"  search {kind} {cards}x failed: {e}")
                continue
            for o in raw or []:
                o = dict(o)
                o["interruptible"] = is_bid
                if o["id"] in skip or o.get("machine_id") in skip_machines:
                    continue
                ok, _ = suitable(o, target_vram, cap)
                if not ok:
                    continue
                if not is_bid:
                    dph = float(o.get("dph_total") or 0)
                    if dph and (not _ondemand_ref or dph < _ondemand_ref):
                        _ondemand_ref = dph
                found[(o["id"], is_bid)] = o
    return sorted(found.values(), key=price)


# ------------------------------------------------------------------ instances

def instances() -> list:
    return vast_json("show", "instances", "--raw") or []


def running_instance() -> dict | None:
    """The instance this is about. More than one is not intended - were there
    several, the most expensive one would be the interesting one, because it
    drives the bill."""
    live = [i for i in instances()
            if str(i.get("actual_status") or "").lower() != "exited"]
    if not live:
        return None
    return max(live, key=lambda i: float(i.get("dph_total") or 0))


def endpoint(i: dict) -> str | None:
    host = i.get("public_ipaddr")
    mapping = (i.get("ports") or {}).get(f"{PORT}/tcp")
    if not host or not mapping:
        return None
    return f"http://{str(host).strip()}:{mapping[0]['HostPort']}"


def healthy(address: str) -> bool:
    """HTTP 200 from /health means: model loaded, server answering. 503 means
    it is still loading - not an error, just not there yet."""
    head = {"Authorization": f"Bearer {token()}"}
    try:
        r = urllib.request.Request(address + "/health", headers=head)
        return urllib.request.urlopen(r, timeout=20).status == 200
    except urllib.error.HTTPError as e:
        return e.code == 200
    except Exception:
        return False


def wait_until_healthy(instance_id: int, timeout_s: int = HEALTH_TIMEOUT_S,
                       logfile: str | None = None,
                       marks: dict | None = None) -> bool:
    """Waits until the server answers and writes along the way: instance
    state, vast.ai's status message and the last lines from the container. So
    afterwards one file says how long the model took to load, and what went
    wrong when it never finished."""
    started = time.time()
    deadline = started + timeout_s
    last_state = ""
    while time.time() < deadline:
        elapsed = int(time.time() - started)
        i = next((x for x in instances() if x.get("id") == instance_id), None)
        if i:
            state = str(i.get("actual_status") or i.get("cur_state") or "?")
            msg = str(i.get("status_msg") or "").strip().replace("\n", " ")[:160]
            address = endpoint(i)
            if marks is not None and state.lower() == "running" \
                    and "container_s" not in marks:
                marks["container_s"] = elapsed
            if state != last_state or elapsed % 120 < HEALTH_POLL_S:
                report(f"  [{elapsed//60:3d} min] {instance_id}: {state}"
                       f"{'  ' + msg if msg else ''}"
                       f"{'  ' + address if address else ''}", logfile)
                if logfile:
                    inside = download_progress(instance_id)
                    report("    inside:\n" + inside if inside
                           else "    container:\n" + container_log(instance_id),
                           logfile)
                last_state = state
            if address and healthy(address):
                if marks is not None:
                    marks["healthy_s"] = elapsed
                    marks["address"] = address
                report(f"  instance {instance_id} healthy after "
                       f"{elapsed//60} min {elapsed%60} s: {address}", logfile)
                return True
            if state.lower() in ("exited", "error"):
                # The instance is about to be destroyed, and with it every
                # trace of why it died. So pull a long container log first:
                # an instance that exits after two minutes has said in there
                # what it was missing, and afterwards nobody can ask anymore.
                report(f"  instance {instance_id} sits at '{state}' - giving up",
                       logfile)
                tail = (ssh_run(instance_id,
                                f"tail -n 80 {SERVER_LOG} 2>/dev/null")
                        or container_log(instance_id, lines=80))
                report("    last container output before the end:\n" + tail,
                       logfile)
                if marks is not None:
                    marks["exit_state"] = state
                    marks["exit_log"] = tail
                return False
        else:
            report(f"  [{elapsed//60:3d} min] {instance_id}: not in the "
                   f"instance list yet", logfile)
        time.sleep(HEALTH_POLL_S)
    report(f"  instance {instance_id} was not healthy after "
           f"{timeout_s//60} minutes", logfile)
    return False


def server_command(model: str, context: int) -> str:
    """The llama-server command line - identical in both launch modes, so a
    machine started over SSH serves exactly what an entrypoint machine does."""
    return (f"-hf {model} --host 0.0.0.0 --port {PORT} "
            f"-ngl 99 --jinja --api-key {token()} "
            f"--ctx-size {context} --parallel 1 --metrics")


def launch(offer: dict, context: int, model: str,
           logfile: str | None = None, with_ssh: bool = True) -> int | None:
    """Starts the replacement instance - the same setup as the first time,
    otherwise the new machine would be cheaper but not the same machine.

    With SSH the instance keeps vast's own entrypoint and llama-server is
    started by --onstart-cmd. That costs nothing and buys the one thing that
    was missing so far: a way in while the model downloads."""
    container_args = server_command(model, context)
    cmd = ["create", "instance", str(offer["id"]),
           "--image", IMAGE, "--disk", str(DISK_GB),
           "--env", f"-p {PORT}:{PORT}"]
    if with_ssh:
        cmd += ["--ssh", "--direct"]
    if offer.get("interruptible"):
        # Without our own bid it would not be a bid but a failure. The flag is
        # called --bid_price; vastai does not know --price.
        cmd += ["--bid_price", f"{price(offer):.4f}"]
    if with_ssh:
        # The image's entrypoint is llama-server itself. Without arguments it
        # exits at once and takes the container with it - three instances
        # died that way today, all reported as 'exited' two minutes in. So
        # the entrypoint becomes a shell that simply waits; the model is
        # fetched over SSH afterwards and the server started on the local
        # file. -hf here would pull the same 18 GB through the one throttled
        # connection in parallel.
        cmd += ["--onstart-cmd",
                f"touch {SERVER_LOG}; mkdir -p {os.path.dirname(MODEL_PATH)}",
                "--entrypoint", "/bin/bash",
                "--raw", "--args", "-c", "sleep infinity"]
    else:
        # --raw has to come before --args: everything after --args is
        # swallowed into the container's command line, vastai's flags too.
        cmd += ["--raw", "--args", *container_args.split()]
    tok = token()
    report("  launch command: vastai " + " ".join(
        b.replace(tok, "<token>") if tok else b for b in cmd), logfile)
    e = subprocess.run([VASTAI, *client_key(), *cmd],
                       capture_output=True, text=True)
    out = (e.stdout or "").strip()
    err = (e.stderr or "").strip()
    report(f"  vastai exit code {e.returncode}", logfile)
    if out:
        report("    stdout: " + out[:800], logfile)
    if err:
        report("    stderr: " + err[:800], logfile)
    if e.returncode != 0:
        raise RuntimeError(err[:400] or "vastai reported an error without text")
    try:
        answer = json.loads(out)
    except json.JSONDecodeError:
        # Depending on the version vastai answers either as JSON or as a
        # sentence with the contract number in it. Both are usable as long as
        # the number is there.
        hit = re.search(r"(?:new_contract|contract|instance)\D{0,12}(\d{5,})",
                        out)
        if not hit:
            raise RuntimeError("no instance number in the answer: "
                               + (out[:200] or "(empty)"))
        return int(hit.group(1))
    new = answer.get("new_contract") or answer.get("id")
    if not new:
        raise RuntimeError("answer without a contract number: " + out[:200])
    return int(new)


def ssh_target(instance_id: int) -> tuple[str, str] | None:
    """(host, port) for SSH.

    The direct route is the machine's own address with the host port that
    vast mapped onto container port 22. That works as soon as sshd is up.
    `vastai ssh-url` names the proxy ssh5.vast.ai instead, and that one
    refused every connection today - so it is only the fallback."""
    for i in instances():
        if i.get("id") != instance_id:
            continue
        host = i.get("public_ipaddr")
        mapping = (i.get("ports") or {}).get("22/tcp")
        if host and mapping:
            return str(host).strip(), str(mapping[0]["HostPort"])
    try:
        url = vast("ssh-url", str(instance_id)).strip()
    except RuntimeError:
        return None
    m = re.search(r"ssh://[^@]+@([^:]+):(\d+)", url)
    return (m.group(1), m.group(2)) if m else None


def ssh_run(instance_id: int, command: str, timeout: int = 60,
            quiet: bool = True) -> str | None:
    """One command on the rented machine. None means: no way in.

    Failures used to vanish silently, and 'download did not start' then said
    nothing about whether ssh timed out, was refused, or the command itself
    failed. With quiet=False the reason lands in the log."""
    target = ssh_target(instance_id)
    if not target or not os.path.exists(SSH_KEY):
        if not quiet:
            report(f"  ssh: no target for instance {instance_id}")
        return None
    host, port = target
    try:
        # -n and </dev/null both matter: a command that backgrounds something
        # leaves the channel open as long as any child still holds stdin, and
        # then ssh waits until the timeout instead of returning at once.
        e = subprocess.run(["ssh", "-n", *SSH_OPTS, "-p", port, f"root@{host}",
                            command], capture_output=True, text=True,
                           timeout=timeout, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        if not quiet:
            report(f"  ssh: no answer within {timeout} s for: {command[:80]}")
        return None
    except OSError as ex:
        if not quiet:
            report(f"  ssh: {ex}")
        return None
    if e.returncode != 0 and not (e.stdout or "").strip():
        if not quiet:
            report(f"  ssh: exit {e.returncode} for {command[:60]}: "
                   f"{(e.stderr or '').strip()[:200]}")
        return None
    return ((e.stdout or "") + (e.stderr or "")).strip()


def scp_to(instance_id: int, local: str, remote: str) -> bool:
    """Copies one file onto the rented machine."""
    target = ssh_target(instance_id)
    if not target or not os.path.exists(local):
        return False
    host, port = target
    try:
        # -O forces the old scp protocol: the sshd in these images often has
        # no sftp subsystem, and modern scp defaults to sftp and fails.
        e = subprocess.run(["scp", "-O", *SSH_OPTS, "-P", port, local,
                            f"root@{host}:{remote}"],
                           capture_output=True, text=True, timeout=120)
    except (subprocess.TimeoutExpired, OSError):
        return False
    return e.returncode == 0


def ensure_running(instance_id: int, logfile: str | None = None,
                   marks: dict | None = None) -> bool:
    """A bid instance is created even when the bid does not win: vast answers
    "success": false, the contract exists, and the instance sits at
    cur_state 'stopped' forever. That looked like six broken machines today.
    One attempt to start it, then the offer is simply not available."""
    for versuch in range(2):
        here = next((x for x in instances() if x.get("id") == instance_id), None)
        if not here:
            time.sleep(10)
            continue
        state = str(here.get("cur_state") or "").lower()
        intended = str(here.get("intended_status") or "").lower()
        if state == "running" or intended == "running":
            return True
        report(f"  instance {instance_id} is '{state}' (intended "
               f"'{intended}') - asking vast.ai to start it", logfile)
        try:
            vast("start", "instance", str(instance_id))
        except RuntimeError as e:
            report(f"  start refused: {str(e)[:160]}", logfile)
        time.sleep(20)
    here = next((x for x in instances() if x.get("id") == instance_id), None)
    state = str((here or {}).get("cur_state") or "gone").lower()
    if state == "running":
        return True
    if marks is not None:
        marks["exit_state"] = f"stayed {state}"
    report(f"  instance {instance_id} stays '{state}' - the bid did not win",
           logfile)
    return False


def wait_for_ssh(instance_id: int, timeout_s: int = SSH_WAIT_S,
                 logfile: str | None = None,
                 marks: dict | None = None) -> bool:
    """Waits until the machine accepts a login. Everything else in the setup
    happens through this door, so it is the first thing that has to work."""
    started = time.time()
    while time.time() - started < timeout_s:
        here = next((x for x in instances() if x.get("id") == instance_id), None)
        state = str((here or {}).get("actual_status") or "").lower()
        if state in ("exited", "error"):
            # Polling a corpse for ten minutes helps nobody, and it costs
            # the whole time. A machine that dies before sshd is up is
            # broken, not slow.
            report(f"  instance {instance_id} died before ssh ({state})",
                   logfile)
            if marks is not None:
                marks["exit_state"] = state
            return False
        if ssh_run(instance_id, "true", 30) is not None:
            elapsed = int(time.time() - started)
            if marks is not None:
                marks["ssh_s"] = elapsed
            report(f"  ssh open after {elapsed} s", logfile)
            return True
        time.sleep(15)
    report(f"  no ssh after {timeout_s//60} minutes", logfile)
    return False


def _remote_progress(instance_id: int, path: str) -> tuple[int, int]:
    """(finished file, bytes on disk so far).

    download_llm.py writes into <ziel>.stuecke/ and only assembles the whole
    file at the very end. Looking at the target file alone therefore shows
    zero for the entire download - which is what made a running download look
    like one that never started."""
    out = ssh_run(instance_id,
                  f"stat -c %s {path} 2>/dev/null || echo 0; "
                  f"du -sb {path}.stuecke 2>/dev/null | cut -f1 || echo 0", 40)
    zahlen = []
    for zeile in (out or "").splitlines():
        zeile = zeile.strip()
        if zeile.isdigit():
            zahlen.append(int(zeile))
    fertig = zahlen[0] if zahlen else 0
    stuecke = zahlen[1] if len(zahlen) > 1 else 0
    return fertig, fertig + stuecke


def fetch_model(instance_id: int, url: str, path: str,
                logfile: str | None = None,
                marks: dict | None = None,
                claimed_mbits: float = 0.0) -> bool:
    """Pulls the model with our own downloader, from the rented machine's own
    fat line - and judges the machine by the rate it actually delivers.

    The download runs in the background and is measured over 30 seconds. Below
    MIN_DOWNLOAD_MBIT the rental ends right there: 18.6 GB at 20 Mbit/s is two
    hours of paid waiting, and the money is better spent on the next offer."""
    started = time.time()
    if claimed_mbits:
        # vast.ai measures inet_down per machine, so the download time is
        # predictable before a single byte moves - and afterwards it can be
        # held against what really arrived.
        report(f"  vast.ai claims {claimed_mbits:.0f} Mbit/s -> "
               f"{MODEL_BYTES * 8 / claimed_mbits / 1e6 / 60:.1f} min for "
               f"{MODEL_BYTES/1e9:.1f} GB", logfile)
    ssh_run(instance_id, f"mkdir -p {os.path.dirname(path)}", 30)
    have_python = (ssh_run(instance_id, "command -v python3 || true", 60) or "")
    if scp_to(instance_id, DOWNLOADER, "/root/download_llm.py") \
            and have_python.strip():
        cmd = (f"cd /root && setsid nohup python3 download_llm.py '{url}' "
               f"-o {path} --stroeme 8 > {DOWNLOAD_LOG} 2>&1 < /dev/null & "
               f"echo started")
        way = "download_llm.py"
    else:
        report("  no python3 on the image - falling back to curl", logfile)
        cmd = (f"setsid nohup curl -sL --retry 3 -o {path} '{url}' "
               f"> {DOWNLOAD_LOG} 2>&1 < /dev/null & echo started")
        way = "curl"
    # The answer to this call is worthless: even detached, ssh keeps the
    # channel open until the timeout, and a running download then looks like
    # a failed start. Twice today a perfectly good machine was destroyed for
    # it. What counts is whether the file grows.
    ssh_run(instance_id, cmd, 20)
    report(f"  download started ({way}), measuring the rate", logfile)

    time.sleep(DOWNLOAD_PROBE_S)
    _, first = _remote_progress(instance_id, path)
    if first == 0:
        # Nothing at all after the probe: then it really did not start.
        time.sleep(DOWNLOAD_PROBE_S)
        _, first = _remote_progress(instance_id, path)
        if first == 0:
            report("  nothing arrived. On the machine: "
                   + (ssh_run(instance_id, f"tail -n 5 {DOWNLOAD_LOG} 2>&1", 40)
                      or "(no log)"), logfile)
            return False
    time.sleep(DOWNLOAD_MEASURE_S)
    _, second = _remote_progress(instance_id, path)
    rate = (second - first) * 8 / DOWNLOAD_MEASURE_S / 1e6
    if marks is not None:
        marks["download_mbits"] = round(rate, 1)
        marks["download_way"] = way
    report(f"  rate after {DOWNLOAD_PROBE_S + DOWNLOAD_MEASURE_S} s: "
           f"{rate:.0f} Mbit/s ({second/1e9:.2f} GB on disk)"
           + (f", promised were {claimed_mbits:.0f}" if claimed_mbits else ""),
           logfile)
    if rate < MIN_DOWNLOAD_MBIT:
        tail = ssh_run(instance_id, f"tail -n 3 {DOWNLOAD_LOG}", 40) or ""
        report(f"  too slow, giving this machine up. Downloader said: "
               f"{tail[:200]}", logfile)
        return False

    # From here the rate is known, so the deadline is a fact, not a guess:
    # twice the time the measured rate needs for what is still missing.
    rest_s = (MODEL_BYTES - second) * 8 / max(rate, 1) / 1e6
    # Plus time for assembling the pieces into one file at the end - that is
    # disk work, not network, and it happens after the last byte arrives.
    deadline = time.time() + min(2 * rest_s + 420, 40 * 60)
    report(f"  expecting the model in about {rest_s/60:.0f} min", logfile)
    while time.time() < deadline:
        time.sleep(30)
        done, seen = _remote_progress(instance_id, path)
        if done >= MODEL_BYTES * 0.99:
            break
        report(f"    {seen/1e9:.1f} of {MODEL_BYTES/1e9:.1f} GB"
               + (" (assembling)" if seen >= MODEL_BYTES * 0.99 else ""),
               logfile)
    bytes_here, _ = _remote_progress(instance_id, path)
    elapsed = max(1, int(time.time() - started))
    if bytes_here < MODEL_BYTES * 0.99:
        report(f"  download incomplete: {bytes_here/1e9:.1f} GB of "
               f"{MODEL_BYTES/1e9:.1f} GB after {elapsed//60} min", logfile)
        return False
    mbits = bytes_here * 8 / elapsed / 1e6
    if marks is not None:
        marks["download_s"] = elapsed
        marks["download_mbits"] = round(mbits, 1)
    report(f"  model complete: {bytes_here/1e9:.1f} GB in {elapsed//60} min "
           f"{elapsed%60} s = {mbits:.0f} Mbit/s average ({way})", logfile)
    return True


def start_server(instance_id: int, path: str, context: int,
                 logfile: str | None = None, slots: int = 1) -> bool:
    """Starts llama-server on the downloaded file. -m instead of -hf: the
    model is already there, nothing may be fetched again."""
    # The binary lives in /app and links against libraries next to it. Called
    # by absolute path from elsewhere it dies with "libllama-server-impl.so:
    # cannot open shared object file" - the image's entrypoint never had that
    # problem because it starts inside /app.
    cmd = (f"cd /app && LD_LIBRARY_PATH=/app setsid nohup ./llama-server "
           f"-m {path} --host 0.0.0.0 --port {PORT} -ngl 99 --jinja "
           f"--api-key {token()} --ctx-size {context} --parallel {slots} "
           f"--metrics "
           f">> {SERVER_LOG} 2>&1 < /dev/null & echo started")
    # Same trap as with the download: ssh keeps the channel open while the
    # server runs, so the return value says nothing. What counts is whether
    # the process is there afterwards.
    ssh_run(instance_id, cmd, 20)
    time.sleep(10)
    laeuft = ssh_run(instance_id, "pgrep -f llama-server | head -1", 40)
    if not (laeuft or "").strip().isdigit():
        report("  server did not come up. Log on the machine:\n"
               + (ssh_run(instance_id, f"tail -n 8 {SERVER_LOG}", 40) or "(none)"),
               logfile)
        return False
    report(f"  server running as pid {laeuft.strip()}", logfile)
    return True


def download_progress(instance_id: int) -> str:
    """What the machine is doing right now, seen from inside: the tail of the
    server log and how much of the model is already on disk. `vastai logs`
    only ever returns an old snapshot, which is worthless during a download."""
    out = ssh_run(instance_id,
                  f"tail -n 4 {SERVER_LOG} 2>/dev/null; "
                  f"du -sh /root/.cache/llama.cpp 2>/dev/null | tail -1", 45)
    if not out:
        return ""
    return "\n".join(f"      > {z}" for z in out.splitlines()[-6:])


def destroy(instance_id: int) -> None:
    """-y is not cosmetic: without it vastai asks, reads EOF from a
    non-interactive shell, prints 'Aborted.' and still exits 0. On
    2026-08-29 an instance kept running for 25 minutes while the log said it
    had been destroyed. So: confirm, then verify."""
    try:
        vast("destroy", "instance", str(instance_id), "-y")
    except RuntimeError as e:
        report(f"  instance {instance_id} could not be destroyed: {e}")
        return
    time.sleep(3)
    still = [i for i in instances() if i.get("id") == instance_id]
    if still:
        report(f"  WARNING: instance {instance_id} is still listed as "
               f"'{still[0].get('actual_status')}' - it keeps costing money")
    else:
        report(f"  instance {instance_id} destroyed")


# ---------------------------------------------------------------------- state

def state_read() -> dict:
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def state_write(d: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(d, f, indent=2, sort_keys=True)


def cooldown(st: dict, now: datetime) -> str | None:
    """Returns the reason why no switch is allowed right now, or None. Without
    this brake the instance wanders in circles on fluctuating prices and
    spends more time loading models than computing."""
    last = st.get("last_switch")
    if last:
        try:
            t = datetime.fromisoformat(last)
        except ValueError:
            return None
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        age = now - t
        if age < timedelta(minutes=MIN_HOLD_MIN):
            left = timedelta(minutes=MIN_HOLD_MIN) - age
            return (f"minimum hold time still running for "
                    f"{int(left.total_seconds()//60)} min")
    last_hour = [w for w in st.get("switches", [])
                 if _as_time(w) and now - _as_time(w) < timedelta(hours=1)]
    if len(last_hour) >= SWITCHES_PER_HOUR:
        return f"already {len(last_hour)} switch(es) in the last hour"
    return None


def _as_time(s: str) -> datetime | None:
    try:
        t = datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


# -------------------------------------------------------------------- verdict

class Verdict:
    def __init__(self, old, best, saving, switch, reason):
        self.old = old
        self.best = best
        self.saving = saving        # fraction, 0.12 = 12 %
        self.switch = switch
        self.reason = reason

    def __repr__(self):
        return (f"<Verdict switch={self.switch} "
                f"saving={self.saving:.3f} reason={self.reason!r}>")


def evaluate(old: dict | None, candidates: list, target_vram: float,
             cap: float, st: dict | None = None,
             now: datetime | None = None) -> Verdict:
    """The whole judgement in one place, without network and without side
    effects - which is why this is what the tests check."""
    fit = [o for o in candidates if suitable(o, target_vram, cap)[0]]
    best = min(fit, key=price) if fit else None
    if old is None:
        return Verdict(None, best, 0.0, False,
                       "no instance running - nothing will be started")
    if best is None:
        return Verdict(old, None, 0.0, False, "no suitable offer")
    old_price = float(old.get("dph_total") or 0)
    if old_price <= 0:
        return Verdict(old, best, 0.0, False,
                       "price of the running instance unknown")
    saving = (old_price - price(best)) / old_price
    if saving <= THRESHOLD:
        return Verdict(old, best, saving, False,
                       f"only {saving*100:.1f} % cheaper, threshold is "
                       f"{THRESHOLD*100:.0f} %")
    blocked = cooldown(st or {}, now or now_utc())
    if blocked:
        return Verdict(old, best, saving, False, blocked)
    return Verdict(old, best, saving, True, f"{saving*100:.1f} % cheaper")


# ------------------------------------------------------------------ switching

def switch_instance(v: Verdict, context: int, model: str) -> bool:
    """Start the new instance, wait, destroy the old one - and on every
    failure keep the old one rather than lose both."""
    old_id = v.old["id"]
    report(f"  switching: {old_id} ({float(v.old.get('dph_total') or 0):.3f} $/h) "
           f"-> offer {v.best['id']} ({price(v.best):.3f} $/h, "
           f"{v.best.get('num_gpus')}x {v.best.get('gpu_name')}, "
           f"{vram_gb(v.best):.0f} GB"
           f"{', interruptible' if v.best.get('interruptible') else ''})")
    logfile = setup_logfile(v.best["id"])
    try:
        new_id = launch(v.best, context, model, logfile)
    except RuntimeError as e:
        report(f"  launch refused: {e} - the old instance stays", logfile)
        return False
    if not new_id:
        report("  vast.ai returned no instance number - the old one stays",
               logfile)
        return False
    report(f"  instance {new_id} started, waiting for /health", logfile)
    if not wait_until_healthy(new_id, logfile=logfile):
        report("  the new one never came up - destroying it, the old one stays")
        destroy(new_id)
        mark_bad_offer(v.best["id"], "not healthy", v.best.get("machine_id"))
        return False
    destroy(old_id)
    st = state_read()
    switches = list(st.get("switches", []))[-20:]
    switches.append(now_utc().isoformat())
    state_write({
        "instance": new_id,
        "predecessor": old_id,
        "price_dph": round(price(v.best), 4),
        "previous_dph": round(float(v.old.get("dph_total") or 0), 4),
        "gpus": v.best.get("num_gpus"),
        "gpu_name": v.best.get("gpu_name"),
        "vram_gb": round(vram_gb(v.best)),
        "interruptible": bool(v.best.get("interruptible")),
        "last_switch": now_utc().isoformat(),
        "switches": switches,
    })
    i = next((x for x in instances() if x.get("id") == new_id), None)
    registry_upsert(endpoint(i) if i else None, v.best, new_id)
    report(f"  done: {old_id} -> {new_id}, {v.saving*100:.1f} % saved")
    return True


# ------------------------------------------------------- procedure and timing

# Every rental runs the same way so the numbers stay comparable: contract,
# container, model loaded, first answer measured. The figures go into a CSV -
# one row per machine, readable side by side.
TIMES_CSV = "times.csv"
TIMES_COLUMNS = ["timestamp", "instance", "offer", "machine", "gpu", "gpus",
                 "vram_gb", "interruptible", "price_dph", "location", "model",
                 "mode", "container_s", "ssh_s", "inet_claimed_mbits",
                 "download_s", "download_mbits", "download_way", "healthy_s",
                 "tok_s",
                 "prompt_tok_s", "nvme_write_mbs", "nvme_read_mbs",
                 "nvme_source", "outcome"]

# 512 MB is enough: large enough that the write cache does not decide the
# result, small enough that the test does not noticeably extend the rental.
NVME_MB = 512
NVME_CMD = (
    "dd if=/dev/zero of=/root/nvmetest bs=1M count={mb} oflag=direct 2>&1; "
    "sync; "
    "dd if=/root/nvmetest of=/dev/null bs=1M iflag=direct 2>&1; "
    "rm -f /root/nvmetest")


def measure_speed(address: str, logfile: str | None = None) -> tuple[float, float]:
    """One small request, so the timings carry not just the loading time but
    the machine's throughput as well. Without that number the cheapest machine
    would always look best, even at half the speed."""
    question = json.dumps({
        "messages": [{"role": "user",
                      "content": "Write isprime(int n) in C. Code only."}],
        "max_tokens": 200, "temperature": 0.2}).encode()
    head = {"Authorization": f"Bearer {token()}",
            "Content-Type": "application/json"}
    try:
        d = json.load(urllib.request.urlopen(urllib.request.Request(
            address + "/v1/chat/completions", data=question, headers=head),
            timeout=300))
        t = d.get("timings", {})
        out = float(t.get("predicted_per_second") or 0)
        inp = float(t.get("prompt_per_second") or 0)
        report(f"  measured: {out:.1f} tok/s generated, {inp:.0f} tok/s prompt",
               logfile)
        return out, inp
    except Exception as e:
        report(f"  measurement failed: {str(e)[:160]}", logfile)
        return 0.0, 0.0


def _dd_mbs(text: str) -> list[float]:
    """dd reports 'copied, 1.23 s, 456 MB/s' - with a comma in some locales.
    Wanted are the throughput figures in the order they appear."""
    values = []
    for number, unit in re.findall(r"([\d.,]+)\s*([kMG])B/s", text):
        try:
            v = float(number.replace(",", "."))
        except ValueError:
            continue
        values.append(v * {"k": 1 / 1024, "M": 1, "G": 1024}[unit])
    return values


def measure_nvme(instance_id: int, offer: dict,
                 logfile: str | None = None) -> tuple[float, float, str]:
    """Write and read on the rented machine's disk, in MB/s."""
    raw = ssh_run(instance_id, NVME_CMD.format(mb=NVME_MB), 180) or ""
    source = "dd over ssh"
    if len(_dd_mbs(raw)) < 2:
        # No SSH (or none yet): vastai execute is the second way in.
        try:
            raw = vast("execute", str(instance_id), NVME_CMD.format(mb=NVME_MB))
            source = "dd via vastai execute"
        except RuntimeError as e:
            report(f"  NVMe test not possible: {str(e)[:140]}", logfile)
            raw = ""
    values = _dd_mbs(raw)
    if len(values) >= 2:
        report(f"  NVMe: {values[0]:.0f} MB/s write, {values[1]:.0f} MB/s read "
               f"({source})", logfile)
        return values[0], values[1], source
    claimed = float(offer.get("disk_bw") or 0)
    report(f"  NVMe: no own result, vast.ai claims {claimed:.0f} MB/s", logfile)
    return claimed, claimed, "vast.ai disk_bw"


def write_times(row: dict) -> str:
    """Appends one row to times.csv, with a header the first time."""
    path = os.path.join(VAST_DIR, TIMES_CSV)
    try:
        os.makedirs(VAST_DIR, exist_ok=True)
        fresh = not os.path.exists(path)
        with open(path, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=TIMES_COLUMNS,
                                    extrasaction="ignore")
            if fresh:
                writer.writeheader()
            writer.writerow(row)
    except OSError as e:
        report(f"  times not written: {e}")
    db_write_times(row)
    return path


def time_row(offer: dict, instance_id, marks: dict, model: str,
             outcome: str) -> dict:
    return {
        "timestamp": now_utc().astimezone().strftime("%Y-%m-%d %H:%M:%S"),
        "instance": instance_id,
        "offer": offer.get("id"),
        # The machine outlives the offer: offers come and go, a broken box
        # stays broken. Blacklist and reports therefore key on this.
        "machine": offer.get("machine_id"),
        "gpu": offer.get("gpu_name"),
        "gpus": offer.get("num_gpus"),
        "vram_gb": round(vram_gb(offer)),
        "interruptible": int(bool(offer.get("interruptible"))),
        "price_dph": round(price(offer), 4),
        "location": offer.get("geolocation"),
        "model": model,
        "mode": marks.get("mode", ""),
        "container_s": marks.get("container_s", ""),
        "ssh_s": marks.get("ssh_s", ""),
        # What vast.ai promises for this machine, next to what it delivered.
        # The gap is the interesting number: today a machine advertised as
        # 1300 Mbit/s pulled at 1 Mbit/s.
        "inet_claimed_mbits": round(float(offer.get("inet_down") or 0), 1),
        "download_s": marks.get("download_s", ""),
        "download_mbits": marks.get("download_mbits", ""),
        "download_way": marks.get("download_way", ""),
        "healthy_s": marks.get("healthy_s", ""),
        "tok_s": round(marks.get("tok_s", 0), 2),
        "prompt_tok_s": round(marks.get("prompt_tok_s", 0), 1),
        "nvme_write_mbs": round(marks.get("nvme_write", 0), 1),
        "nvme_read_mbs": round(marks.get("nvme_read", 0), 1),
        "nvme_source": marks.get("nvme_source", ""),
        "outcome": outcome,
        # Not part of the CSV (DictWriter ignores extras) but kept for the
        # database: where to read the whole story, and what the machine said
        # with its last breath.
        "setup_log": marks.get("setup_log", ""),
        "exit_log": marks.get("exit_log", ""),
    }


DB_TABLE = "vast_rentals"
DB_SCHEMA = f"""CREATE TABLE IF NOT EXISTS {DB_TABLE} (
    id             BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    timestamp      DATETIME NOT NULL,
    instance       BIGINT,
    offer          BIGINT,
    machine        BIGINT,
    gpu            VARCHAR(80),
    gpus           INT,
    vram_gb        INT,
    interruptible  TINYINT(1),
    price_dph      DECIMAL(10,4),
    location       VARCHAR(120),
    model          VARCHAR(200),
    mode           VARCHAR(30),
    container_s    INT,
    ssh_s          INT,
    inet_claimed_mbits DECIMAL(10,1),
    download_s     INT,
    download_mbits DECIMAL(10,1),
    download_way   VARCHAR(30),
    healthy_s      INT,
    tok_s          DECIMAL(10,2),
    prompt_tok_s   DECIMAL(10,1),
    nvme_write_mbs DECIMAL(10,1),
    nvme_read_mbs  DECIMAL(10,1),
    nvme_source    VARCHAR(40),
    outcome        VARCHAR(60),
    setup_log      VARCHAR(255),
    exit_log       TEXT,
    PRIMARY KEY (id),
    KEY idx_instance (instance),
    KEY idx_machine (machine),
    KEY idx_timestamp (timestamp)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""


def db_write_times(row: dict) -> bool:
    """Second home for the same numbers: the job database. The CSV is for
    reading side by side, the table for asking questions - which GPU comes up
    fastest, which offer failed twice. Fails quietly, a rental must never
    depend on the database being there."""
    try:
        import pymysql
    except ImportError:
        return False
    spalten = [c for c in TIMES_COLUMNS] + ["setup_log", "exit_log"]
    werte = [row.get(c) if row.get(c) != "" else None for c in spalten]
    try:
        conn = pymysql.connect(**DB, charset="utf8mb4")
        with conn, conn.cursor() as cur:
            cur.execute(DB_SCHEMA)
            # The table outlives the columns: whenever a new measurement is
            # added here, the existing table lacks it and every insert fails.
            cur.execute(f"SHOW COLUMNS FROM {DB_TABLE}")
            da = {z[0] for z in cur.fetchall()}
            for spalte, typ in (("machine", "BIGINT"), ("mode", "VARCHAR(30)"),
                                ("ssh_s", "INT"), ("download_s", "INT"),
                                ("download_mbits", "DECIMAL(10,1)"),
                                ("download_way", "VARCHAR(30)"),
                                ("inet_claimed_mbits", "DECIMAL(10,1)"),
                                ("setup_log", "VARCHAR(255)"),
                                ("exit_log", "TEXT")):
                if spalte not in da:
                    cur.execute(f"ALTER TABLE {DB_TABLE} ADD COLUMN "
                                f"`{spalte}` {typ}")
            cur.execute(
                f"INSERT INTO {DB_TABLE} ({', '.join('`'+c+'`' for c in spalten)}) "
                f"VALUES ({', '.join(['%s'] * len(spalten))})", werte)
            conn.commit()
        return True
    except Exception as e:
        report(f"  rental not written to the database: {str(e)[:160]}")
        return False


# ------------------------------------------------------------------- registry

# The llm_models table on dell is where every tool looks up which model is
# reachable where: the web interface builds its dropdown from it, the batch
# poller takes the address from the endpoint column, and kicode uses the same
# address as --endpunkt. Whoever rents a machine and does not register it
# there owns a GPU nobody knows about.
DB = dict(host="localhost", user="gh", password="a12345", database="wagodb")
RENT_MODEL_KEY = "MIETGPU"


def registry_upsert(address: str | None, offer: dict,
                    instance_id: int, active: bool = True) -> bool:
    """Registers the rented machine (or deregisters it). Fails quietly when
    pymysql is missing or the database is unreachable - a running instance
    matters more than its registry entry."""
    try:
        import pymysql
    except ImportError:
        report("  pymysql missing - no entry in llm_models")
        return False
    endpoint_text = (f"{(address or '').replace('http://', '')} "
                     f"(vast.ai {instance_id}, {offer.get('num_gpus')}x "
                     f"{offer.get('gpu_name')}, GPU)")
    try:
        conn = pymysql.connect(**DB, charset="utf8mb4")
        with conn, conn.cursor() as cur:
            cur.execute(
                """INSERT INTO llm_models
                     (model_key, model_id, provider, endpoint, display_name,
                      cost_estimate, is_default, active, sort_order, notes)
                   VALUES (%s, 'local', 'local', %s, %s, %s, 0, %s, 1, %s)
                   ON DUPLICATE KEY UPDATE
                     endpoint=VALUES(endpoint), display_name=VALUES(display_name),
                     cost_estimate=VALUES(cost_estimate), active=VALUES(active),
                     notes=VALUES(notes)""",
                (RENT_MODEL_KEY, endpoint_text,
                 f"rented GPU {offer.get('num_gpus')}x {offer.get('gpu_name')} "
                 f"({vram_gb(offer):.0f} GB)",
                 f"{price(offer):.3f} $/h rent"
                 + (", interruptible" if offer.get("interruptible") else ""),
                 1 if active else 0,
                 f"rented by vast_optimizer.py, instance {instance_id}"))
            conn.commit()
        report(f"  registered in llm_models as {RENT_MODEL_KEY}: {endpoint_text}")
        return True
    except Exception as e:
        report(f"  entry in llm_models failed: {e}")
        return False


# ------------------------------------------------------- renting and assigning

def assign_task(address: str, project: str = RENT_PROJECT) -> bool:
    """Hands the freshly rented machine the same task the CPU model at home is
    working on. The driver keeps running as its own service, even when this
    session ends - a build takes hours."""
    if not os.path.exists(BUILD_DRIVER):
        report(f"  no build driver at {BUILD_DRIVER} - no task assigned")
        return False
    unit = f"bau-miet-{int(time.time())}"
    cmd = ["systemd-run", "--user", "--collect", "--unit", unit,
           BUILD_DRIVER, address, project]
    e = subprocess.run(cmd, capture_output=True, text=True)
    if e.returncode != 0:
        report(f"  task could not be started: {e.stderr.strip()[:200]}")
        return False
    report(f"  task assigned: {unit} builds in {project} against {address}")
    return True


def rent_one(best: dict, a) -> int:
    """One attempt at one offer: rent, set up, measure, hand over the task."""
    logfile = setup_logfile(best["id"])
    report(f"setup begins. offer {best['id']}, {price(best):.3f} $/h, "
           f"{best.get('num_gpus')}x {best.get('gpu_name')} "
           f"({vram_gb(best):.0f} GB), {best.get('geolocation', '?')}, "
           f"model {a.model}", logfile)
    try:
        new_id = launch(best, a.context, a.model, logfile,
                        with_ssh=not a.no_ssh)
    except RuntimeError as e:
        report(f"  launch refused: {e}", logfile)
        return 1
    if not new_id:
        report("  vast.ai returned no instance number", logfile)
        return 1
    report(f"  instance {new_id} started, waiting for /health "
           f"(up to {HEALTH_TIMEOUT_S//60} minutes)", logfile)
    marks: dict = {"setup_log": logfile,
                   "mode": "hf-entrypoint" if a.no_ssh else "ssh+downloader"}

    def give_up(reason: str) -> int:
        report(f"  {reason} - destroying the instance so it costs nothing",
               logfile)
        destroy(new_id)
        mark_bad_offer(best["id"], reason, best.get("machine_id"))
        path = write_times(time_row(best, new_id, marks, a.model, reason))
        report(f"  times: {path}", logfile)
        return 1

    if not ensure_running(new_id, logfile, marks):
        return give_up("bid not accepted")
    if not a.no_ssh:
        # Always the same steps, in the same order, each timed: door open,
        # model here, server up, server answering. That is what makes two
        # rentals comparable at all.
        if not wait_for_ssh(new_id, logfile=logfile, marks=marks):
            return give_up("no ssh")
        if not fetch_model(new_id, a.url, MODEL_PATH, logfile, marks,
                           float(best.get("inet_down") or 0)):
            return give_up("download too slow or incomplete")
        if not start_server(new_id, MODEL_PATH, a.context, logfile, a.slots):
            return give_up("server did not start")
    if not wait_until_healthy(new_id, logfile=logfile, marks=marks):
        return give_up(marks.get("exit_state", "never came up"))
    marks["tok_s"], marks["prompt_tok_s"] = measure_speed(
        marks.get("address", ""), logfile)
    (marks["nvme_write"], marks["nvme_read"],
     marks["nvme_source"]) = measure_nvme(new_id, best, logfile)
    i = next((x for x in instances() if x.get("id") == new_id), None)
    address = endpoint(i) if i else None
    st = state_read()
    st.update({"instance": new_id, "price_dph": round(price(best), 4),
               "gpus": best.get("num_gpus"),
               "gpu_name": best.get("gpu_name"),
               "vram_gb": round(vram_gb(best)),
               "interruptible": bool(best.get("interruptible")),
               "endpoint": address,
               "rented_at": now_utc().isoformat()})
    state_write(st)
    report(f"  ready after {marks.get('healthy_s', 0)//60} min "
           f"{marks.get('healthy_s', 0)%60} s: {address}", logfile)
    path = write_times(time_row(best, new_id, marks, a.model, "ready"))
    report(f"  times: {path}", logfile)
    registry_upsert(address, best, new_id)
    if a.task and address:
        if assign_task(address, a.project):
            report(f"  task running, build progress in {a.project}/bau.log",
                   logfile)
    report(f"  setup log: {logfile}")
    return 0


def rent(a) -> int:
    """Rent the best suitable offer, let it set itself up, wait until the
    model answers - and then hand it the task. Without --yes it only shows
    what would be rented: from the start the clock is running.

    One offer is not enough. Machines hang on the image pull, bids are not
    accepted, downloads crawl - today six rentals in a row failed for three
    different reasons. So the list is worked through until one machine
    stands, and every failure is blacklisted on the way."""
    candidates = offers(a.vram, a.cap, with_bids=not a.no_bid)
    if not candidates:
        report("  no suitable offer found")
        return 1
    if not a.yes:
        best = candidates[0]
        report(f"  best offer {best['id']}: {price(best):.3f} $/h, "
               f"{best.get('num_gpus')}x {best.get('gpu_name')} "
               f"({vram_gb(best):.0f} GB), {best.get('geolocation', '?')}"
               f"{', interruptible' if best.get('interruptible') else ''}")
        report("  this costs money from the start. Repeat with --yes.")
        return 0
    for nr, best in enumerate(candidates[:a.attempts], 1):
        report(f"attempt {nr} of {min(a.attempts, len(candidates))}: "
               f"offer {best['id']}, {price(best):.3f} $/h, "
               f"{best.get('num_gpus')}x {best.get('gpu_name')} "
               f"({vram_gb(best):.0f} GB), {best.get('geolocation', '?')}"
               f"{', interruptible' if best.get('interruptible') else ''}")
        if rent_one(best, a) == 0:
            return 0
    report(f"  {a.attempts} attempts, no machine came up")
    return 1


def worker_running() -> bool:
    """Is the analyzer working on heissa.de?"""
    try:
        e = subprocess.run(["ssh", "-n", "-o", "ConnectTimeout=15",
                            "-o", "BatchMode=yes", WORKER_HOST, WORKER_CHECK],
                           capture_output=True, text=True, timeout=45)
    except (subprocess.TimeoutExpired, OSError):
        return False
    return (e.stdout or "").strip().isdigit()


def start_worker(endpoint: str) -> bool:
    """Points the analyzer at the current endpoint and starts it. An old run
    against a dead machine is stopped first - it would only pile up errors."""
    befehl = ("pkill -f 'content_analyzer.py --llm-url'; sleep 2; "
              + WORKER_CMD.format(endpoint=endpoint) + " echo started")
    try:
        e = subprocess.run(["ssh", "-n", "-o", "ConnectTimeout=15",
                            "-o", "BatchMode=yes", WORKER_HOST, befehl],
                           capture_output=True, text=True, timeout=60)
    except (subprocess.TimeoutExpired, OSError) as ex:
        report(f"  analyzer could not be started: {str(ex)[:120]}")
        return False
    ok = e.returncode == 0
    report(f"  analyzer {'started' if ok else 'refused'} on {WORKER_HOST} "
           f"against {endpoint}")
    return ok


def watch(a) -> int:
    """One tick of the watchdog, meant for cron.

    Three cases, in this order: nothing rented -> rent and put the analyzer
    to work; rented but not answering -> throw it away, the next tick rents
    again; rented and healthy -> look whether somebody sells the same thing
    more than 11 % cheaper, and keep the analyzer running.

    An interruptible instance can disappear between two ticks, which is
    exactly why this exists."""
    st = state_read()
    live = running_instance()
    if not live:
        report("watch: nothing rented")
        if not a.yes:
            report("  (without --yes nothing is rented)")
            return 0
        rc = rent(a)
        if rc == 0 and a.analyze:
            endpoint_now = state_read().get("endpoint")
            if endpoint_now:
                start_worker(endpoint_now)
        return rc
    address = endpoint(live)
    if not address or not healthy(address):
        report(f"watch: instance {live['id']} does not answer - destroying it")
        destroy(live["id"])
        registry_upsert(None, {"num_gpus": live.get("num_gpus"),
                               "gpu_name": live.get("gpu_name"),
                               "gpu_ram": live.get("gpu_ram"),
                               "dph_total": live.get("dph_total")},
                        live["id"], active=False)
        return 1
    report(f"watch: {live['id']} healthy at {address}, "
           f"{float(live.get('dph_total') or 0):.3f} $/h")
    if a.analyze and not worker_running():
        report("  analyzer is not running")
        start_worker(address)
    return run_once(a, for_real=a.yes)


# ------------------------------------------------------------------- commands

def show_status(a) -> None:
    old = running_instance()
    if not old:
        print("  no instance running. Nothing is being charged.")
        return
    print(f"  instance {old['id']}  {old.get('actual_status', '?')}  "
          f"{old.get('num_gpus')}x {old.get('gpu_name', '?')}  "
          f"{vram_gb(old):.0f} GB  {float(old.get('dph_total') or 0):.3f} $/h")
    print(f"    uptime: {float(old.get('duration') or 0)/3600:.2f} h "
          f"= ${float(old.get('duration') or 0)/3600*float(old.get('dph_total') or 0):.2f}")
    print(f"    endpoint: {endpoint(old) or 'not mapped yet'}")
    st = state_read()
    if st.get("last_switch"):
        print(f"    last switch: {st['last_switch']} "
              f"(from {st.get('predecessor')}, {st.get('previous_dph')} -> "
              f"{st.get('price_dph')} $/h)")


def run_once(a, for_real: bool) -> int:
    old = running_instance()
    candidates = offers(a.vram, a.cap, with_bids=not a.no_bid)
    report(f"{'run' if for_real else 'check'}: target {a.vram:.0f} GB, "
           f"cap {a.cap:.2f} $/h, {len(candidates)} suitable offers")
    v = evaluate(old, candidates, a.vram, a.cap, state_read())
    if v.best:
        report(f"  best offer {v.best['id']}: {price(v.best):.3f} $/h, "
               f"{v.best.get('num_gpus')}x {v.best.get('gpu_name')} "
               f"({vram_gb(v.best):.0f} GB), {v.best.get('geolocation', '?')}"
               f"{', interruptible' if v.best.get('interruptible') else ''}")
    if v.old:
        report(f"  running: {v.old['id']} at "
               f"{float(v.old.get('dph_total') or 0):.3f} $/h")
    report(f"  verdict: {v.reason}")
    if not v.switch:
        return 0
    if not for_real:
        report("  (check - nothing is switched)")
        return 0
    return 0 if switch_instance(v, a.context, a.model) else 1


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    p.add_argument("command", nargs="?", default="check",
                   choices=["check", "run", "status", "rent", "watch"])
    p.add_argument("--vram", type=float, default=TARGET_VRAM,
                   help="target VRAM in GB in total (48 may also be 2x24)")
    p.add_argument("--cap", type=float, default=PRICE_CAP,
                   help="maximum price per hour in dollars")
    p.add_argument("--context", type=int, default=CONTEXT)
    p.add_argument("--model", default=MODEL)
    p.add_argument("--no-bid", action="store_true",
                   help="ignore interruptible (bid) offers")
    p.add_argument("--yes", action="store_true",
                   help="rent: actually rent, this costs money from the start")
    p.add_argument("--task", action="store_true",
                   help="rent: hand the rented machine the build task at once")
    p.add_argument("--analyze", action="store_true",
                   help="watch: keep the post analyzer on heissa.de pointed at "
                        "the current endpoint")
    p.add_argument("--project", default=RENT_PROJECT,
                   help="directory the rented machine builds in")
    p.add_argument("--no-ssh", action="store_true",
                   help="no SSH: llama-server as entrypoint, model via -hf "
                        "(the slow way, kept for comparison)")
    p.add_argument("--slots", type=int, default=1,
                   help="parallel slots of llama-server: one long job needs "
                        "one, a queue of short jobs needs many")
    p.add_argument("--attempts", type=int, default=3,
                   help="how many offers to try before giving up")
    p.add_argument("--url", default=MODEL_URL,
                   help="direct URL of the model file for the downloader")
    a = p.parse_args()
    if a.command == "status":
        show_status(a)
        return 0
    if a.command == "rent":
        return rent(a)
    if a.command == "watch":
        return watch(a)
    return run_once(a, for_real=(a.command == "run"))


if __name__ == "__main__":
    sys.exit(main())
