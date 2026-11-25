"""Studio command line.

    python -m studio check              # is everything set up? (ffmpeg, encoder, whisper, server)
    python -m studio run                # normal mode: download approved clips + edit them, every few minutes
    python -m studio sync               # just download approved clips
    python -m studio edit [FILES]       # edit everything in the inboxes (or the given files)
    python -m studio vod URL --campaign NAME [--top 8]
    python -m studio posted FILE        # move a finished clip to posted/ after you upload it
"""
from __future__ import annotations

import argparse
import logging
import shutil
import sys
import time
from pathlib import Path

from studio import config
from studio.client import Client, ServerError
from studio.editor import encode, pipeline, transcribe
from studio.sync import sync

log = logging.getLogger("studio")


def _edit_all(cfg, files=None, layout=None) -> int:
    targets = [Path(f) for f in files] if files else pipeline.pending(cfg["output_dir"])
    ok = 0
    for f in targets:
        try:
            pipeline.edit(f, cfg, layout)
            ok += 1
        except Exception as e:
            log.error("editing %s failed: %s", f.name, e)
    return ok


def cmd_check(cfg, _args) -> None:
    print("ffmpeg:   ", "ok" if shutil.which("ffmpeg") else "MISSING: install ffmpeg and make sure it's on PATH")
    print("encoder:  ", encode.pick(cfg["editing"].get("encoder", "auto")))
    print("whisper:  ", f"ok (model {cfg['captions']['whisper_model']})" if transcribe.available()
          else "not installed: pip install faster-whisper (clips will have no captions)")
    print("output:   ", cfg["output_dir"].resolve())
    try:
        camps = Client(cfg["server_url"], cfg["token"]).get("/api/campaigns")
        print("server:   ", f"ok, {len(camps)} campaigns: {', '.join(c['name'] for c in camps)}")
    except ServerError as e:
        print("server:   ", e)


def cmd_sync(cfg, _args) -> None:
    new = sync(Client(cfg["server_url"], cfg["token"]), cfg["output_dir"])
    print(f"{len(new)} new clips downloaded")


def cmd_edit(cfg, args) -> None:
    print(f"{_edit_all(cfg, args.files, args.layout)} clips ready")


def cmd_run(cfg, _args) -> None:
    client = Client(cfg["server_url"], cfg["token"])
    log.info("running: checking for approved clips every %s min (Ctrl+C to stop)", cfg["poll_minutes"])
    while True:
        try:
            sync(client, cfg["output_dir"])
        except ServerError as e:
            log.warning("%s", e)
        _edit_all(cfg)
        time.sleep(float(cfg["poll_minutes"]) * 60)


def cmd_vod(cfg, args) -> None:
    from studio import vod
    rules = {}
    try:
        camps = {c["name"]: c for c in Client(cfg["server_url"], cfg["token"]).get("/api/campaigns")}
        c = camps.get(args.campaign, {})
        rules = {k: c.get(k) for k in ("min_seconds", "max_seconds", "required_text", "layout")}
    except ServerError:
        log.info("server not reachable: using default clip rules for this VOD")
    clips = vod.run(args.source, args.campaign, cfg, args.top, rules, args.cookies, not args.no_transcribe)
    print(f"{len(clips)} moments picked")
    if not args.no_edit:
        print(f"{_edit_all(cfg, clips)} clips ready in {cfg['output_dir'] / args.campaign / 'ready'}")


def cmd_posted(cfg, args) -> None:
    for f in map(Path, args.files):
        posted = f.parent.parent / "posted"
        posted.mkdir(exist_ok=True)
        for extra in (f, f.with_suffix(".caption.txt"), f.with_suffix(".json")):
            if extra.exists():
                shutil.move(str(extra), posted / extra.name)
        print(f"moved {f.name} to posted/")


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="studio", description="Clipping studio (laptop side)")
    p.add_argument("--config", default="studio.yaml")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check")
    sub.add_parser("sync")
    sub.add_parser("run")
    e = sub.add_parser("edit")
    e.add_argument("files", nargs="*")
    e.add_argument("--layout", choices=["blur", "crop", "facecam"])
    v = sub.add_parser("vod")
    v.add_argument("source", help="VOD/video URL, or a local file")
    v.add_argument("--campaign", required=True)
    v.add_argument("--top", type=int)
    v.add_argument("--cookies", help="cookies.txt for sites that need a login")
    v.add_argument("--no-transcribe", action="store_true", help="skip the keyword transcript for chat-less VODs")
    v.add_argument("--no-edit", action="store_true", help="only pick and cut moments into the inbox")
    po = sub.add_parser("posted")
    po.add_argument("files", nargs="+")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S")
    cfg = config.load(args.config)
    try:
        {"check": cmd_check, "sync": cmd_sync, "run": cmd_run, "edit": cmd_edit, "vod": cmd_vod,
         "posted": cmd_posted}[args.cmd](cfg, args)
    except KeyboardInterrupt:
        pass
    except ServerError as e:
        sys.exit(str(e))
