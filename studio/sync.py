"""Pull approved clips from the watcher into Campaigns/<campaign>/inbox/."""
from __future__ import annotations

import json
import logging
import re
import shutil
from datetime import datetime
from pathlib import Path

from studio.client import Client

log = logging.getLogger("sync")


def safe(name: str) -> str:
    return re.sub(r"[^\w\- .]", "_", name).strip() or "unnamed"


def campaign_dirs(out: Path, campaign: str) -> dict[str, Path]:
    root = out / safe(campaign)
    dirs = {k: root / k for k in ("inbox", "ready", "posted", "vods")}
    for d in dirs.values():
        d.mkdir(parents=True, exist_ok=True)
    return dirs


def clip_stem(channel: str, at: float, mid) -> str:
    return f"{safe(channel.replace(':', '_'))}_{datetime.fromtimestamp(at).strftime('%Y%m%d_%H%M%S')}_{mid}"


def write_sidecar(path: Path, data: dict) -> None:
    path.with_suffix(".json").write_text(json.dumps(data, indent=2), encoding="utf-8")


def sync(client: Client, out: Path) -> list[Path]:
    """Download every approved clip into each of its campaigns. Returns new inbox files."""
    campaigns = {c["name"]: c for c in client.get("/api/campaigns")}
    approved = client.get("/api/moments?status=approved&has_clip=true")
    new: list[Path] = []
    for m in approved:
        if not m["campaigns"]:
            log.warning("moment #%s is approved but has no campaign; pick one on the dashboard", m["id"])
            continue
        stem = clip_stem(m["channel"], m["at"], m["id"])
        first: Path | None = None
        for name in m["campaigns"]:
            rules = campaigns.get(name, {"name": name})
            dest = campaign_dirs(out, name)["inbox"] / f"{stem}.mp4"
            if not dest.exists():
                if first is None:
                    log.info("downloading #%s %s → %s", m["id"], m["channel"], name)
                    client.download(m["clip_url"], dest)
                else:
                    shutil.copy2(first, dest)
                new.append(dest)
            first = first or dest
            write_sidecar(dest, {
                "id": m["id"], "source": "live", "channel": m["channel"], "at": m["at"], "score": m["score"],
                "moment_offset": m.get("moment_offset"), "signals": m["signals"], "campaign": name,
                "rules": {k: rules.get(k) for k in ("min_seconds", "max_seconds", "required_text", "layout")},
            })
        client.post(f"/api/moments/{m['id']}/downloaded")
    return new
