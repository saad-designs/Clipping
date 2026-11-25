"""Cuts clips out of the rolling segment buffer, without re-encoding.

Segments are named <unix_start>.ts, so segment i covers [start_i, start_{i+1}).
A clip for a moment at T covers [T - pre_roll, T + post_roll], rounded out to segment
edges. Studio (on the laptop) trims precisely later, when it re-encodes anyway.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

log = logging.getLogger("cutter")


def list_segments(seg_dir: Path) -> list[tuple[float, Path]]:
    out = []
    for p in seg_dir.glob("*.ts"):
        try:
            out.append((float(p.stem), p))
        except ValueError:
            continue
    return sorted(out)


def segments_for(seg_dir: Path, begin: float, end: float) -> list[tuple[float, Path]]:
    segs = list_segments(seg_dir)
    chosen = []
    for i, (start, path) in enumerate(segs):
        nxt = segs[i + 1][0] if i + 1 < len(segs) else float("inf")
        if start < end and nxt > begin:
            chosen.append((start, path))
    return chosen


async def cut(segs: list[Path], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    lst = out_path.with_suffix(".txt")
    lst.write_text("".join(f"file '{p.resolve().as_posix()}'\n" for p in segs), encoding="utf-8")
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "concat", "-safe", "0", "-i", str(lst),
            "-c", "copy", "-bsf:a", "aac_adtstoasc", "-movflags", "+faststart", str(out_path),
            stderr=asyncio.subprocess.PIPE)
        _, err = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg concat failed: {err.decode(errors='replace')[-500:]}")
    finally:
        lst.unlink(missing_ok=True)


@dataclass
class _Job:
    moment_id: int
    channel: str
    at: float


class Cutter:
    """Queue of moments waiting for their post-roll footage to be recorded."""

    def __init__(self, db, buffer_root: Path, clips_root: Path, pre_roll: int, post_roll: int,
                 segment_seconds: int):
        self.db = db
        self.buffer_root = buffer_root
        self.clips_root = clips_root
        self.pre, self.post, self.seg = pre_roll, post_roll, segment_seconds
        self.jobs: list[_Job] = []

    def enqueue(self, moment_id: int, channel: str, at: float) -> None:
        self.jobs.append(_Job(moment_id, channel, at))

    def oldest_needed(self, channel: str) -> float | None:
        """Earliest buffer time a pending cut still needs (cleanup must keep it)."""
        times = [j.at - self.pre for j in self.jobs if j.channel == channel]
        return min(times) if times else None

    def _ready(self, job: _Job, now: float) -> bool:
        end = job.at + self.post
        segs = list_segments(self.buffer_root / safe_name(job.channel))
        if segs and segs[-1][0] >= end:   # a newer segment started → the one holding `end` is complete
            return True
        return now > end + self.seg * 3 + 30   # stream ended / stalled: cut whatever exists

    async def run(self) -> None:
        while True:
            now = time.time()
            for job in [j for j in self.jobs if self._ready(j, now)]:
                self.jobs.remove(job)
                await self._do(job)
            await asyncio.sleep(2)

    async def _do(self, job: _Job) -> None:
        seg_dir = self.buffer_root / safe_name(job.channel)
        chosen = segments_for(seg_dir, job.at - self.pre, job.at + self.post)
        # The newest segment may still be being written; drop it unless the stream stopped.
        all_segs = list_segments(seg_dir)
        if chosen and all_segs and chosen[-1][1] == all_segs[-1][1] and time.time() - all_segs[-1][0] < self.seg * 3:
            chosen = chosen[:-1]
        if not chosen:
            log.warning("moment #%d: no footage in buffer for %s", job.moment_id, job.channel)
            return
        stamp = datetime.fromtimestamp(job.at).strftime("%Y%m%d_%H%M%S")
        out = self.clips_root / safe_name(job.channel) / f"{job.moment_id}_{stamp}.mp4"
        try:
            await cut([p for _, p in chosen], out)
        except Exception as e:
            log.error("moment #%d: cut failed: %s", job.moment_id, e)
            return
        self.db.set_clip(job.moment_id, str(out), chosen[0][0])
        log.info("✂️  clip ready #%d  %s  (moment at %ds into the clip)", job.moment_id, out,
                 round(job.at - chosen[0][0]))


def safe_name(channel: str) -> str:
    return channel.replace(":", "_")
