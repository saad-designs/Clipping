"""Rolling stream recorder.

    streamlink/yt-dlp → stdout  |  ffmpeg -c copy -f segment  →  data/buffer/<channel>/<unix_start>.ts

No re-encoding, so each stream costs almost no CPU. Old segments are deleted after
`buffer_minutes`, except ones a pending clip still needs.
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Callable

from watcher.cutter import SEGMENT_PATTERN, list_segments, safe_name

log = logging.getLogger("recorder")

SourceCmd = Callable[[str], list[str]]           # channel key -> command that writes MPEG-TS to stdout
OnSegment = Callable[[str, Path, float], None]   # (channel, finished segment, its unix start)


def fake_cmd(sample: str) -> SourceCmd:
    """Loop a local video in real time, for testing without a live stream."""
    return lambda _ch: ["ffmpeg", "-hide_banner", "-loglevel", "error", "-re", "-stream_loop", "-1",
                        "-i", sample, "-c", "copy", "-f", "mpegts", "-"]


class Recorder:
    def __init__(self, channel: str, seg_dir: Path, source_cmd: SourceCmd, segment_seconds: int,
                 on_segment: OnSegment, offline_retry: float):
        self.channel = channel
        self.dir = seg_dir
        self.source_cmd = source_cmd
        self.seg = segment_seconds
        self.on_segment = on_segment
        self.offline_retry = offline_retry
        self._seen: set[Path] = set()
        self._task: asyncio.Task | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if not self.running:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._flush(final=True)

    async def _loop(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        while True:
            started = time.time()
            await self._run_once()
            self._flush(final=True)
            # Exited fast = channel offline or blocked. Exited after a while = stream dropped.
            wait = self.offline_retry if time.time() - started < 30 else 5
            log.info("%s: recorder stopped; retry in %ds", self.channel, wait)
            await asyncio.sleep(wait)

    async def _run_once(self) -> None:
        # streamlink -> (copied by us) -> ffmpeg. A plain copy loop instead of an OS pipe works the same
        # on Windows, macOS and Linux; at a few Mbps it costs almost no CPU.
        src = await asyncio.create_subprocess_exec(
            *self.source_cmd(self.channel), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        ff = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
            "-map", "0:v:0?", "-map", "0:a:0?", "-c", "copy",
            "-f", "segment", "-segment_time", str(self.seg), "-reset_timestamps", "1",
            "-strftime", "1", str(self.dir / f"{SEGMENT_PATTERN}.ts"),
            stdin=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        log.info("%s: recording", self.channel)

        async def pump() -> None:
            try:
                while True:
                    chunk = await src.stdout.read(1 << 16)
                    if not chunk:
                        break
                    ff.stdin.write(chunk)
                    await ff.stdin.drain()
            except (ConnectionError, BrokenPipeError):
                pass
            finally:
                try:
                    ff.stdin.close()
                except Exception:
                    pass

        tasks = [asyncio.create_task(pump()), asyncio.create_task(src.wait()), asyncio.create_task(ff.wait())]
        try:
            while True:
                done, _ = await asyncio.wait(tasks[1:], timeout=1)
                self._flush()
                if done:
                    break
        finally:
            for p in (src, ff):
                if p.returncode is None:
                    try:
                        p.terminate()
                    except ProcessLookupError:
                        pass
            for p in (src, ff):
                try:
                    await asyncio.wait_for(p.wait(), 5)
                except asyncio.TimeoutError:
                    p.kill()
            tasks[0].cancel()

    def _flush(self, final: bool = False) -> None:
        """Report segments that are complete (a newer one exists, or recording ended)."""
        segs = list_segments(self.dir) if self.dir.exists() else []
        done = segs if final else segs[:-1]
        for start, path in done:
            if path not in self._seen:
                self._seen.add(path)
                try:
                    self.on_segment(self.channel, path, start)
                except Exception as e:
                    log.warning("%s: segment handler failed: %s", self.channel, e)

    def cleanup(self, keep_after: float) -> int:
        removed = 0
        for start, path in list_segments(self.dir) if self.dir.exists() else []:
            if start < keep_after:
                path.unlink(missing_ok=True)
                self._seen.discard(path)
                removed += 1
        return removed


class RecorderPool:
    def __init__(self, buffer_root: Path, source_cmd: SourceCmd, segment_seconds: int,
                 on_segment: OnSegment, max_concurrent: int, offline_retry: float = 120):
        self.root = buffer_root
        self.source_cmd = source_cmd
        self.seg = segment_seconds
        self.on_segment = on_segment
        self.max = max_concurrent
        self.offline_retry = offline_retry
        self.recorders: dict[str, Recorder] = {}

    def recording(self) -> list[str]:
        return [ch for ch, r in self.recorders.items() if r.running]

    def is_recording(self, channel: str) -> bool:
        r = self.recorders.get(channel)
        return r is not None and r.running

    def start(self, channel: str) -> None:
        if self.is_recording(channel):
            return
        active = sum(r.running for r in self.recorders.values())
        if active >= self.max:
            log.warning("not recording %s: max_concurrent=%d reached (chat still monitored)", channel, self.max)
            return
        rec = self.recorders.get(channel) or Recorder(
            channel, self.root / safe_name(channel), self.source_cmd, self.seg,
            self.on_segment, self.offline_retry)
        self.recorders[channel] = rec
        rec.start()

    async def stop(self, channel: str) -> None:
        if channel in self.recorders:
            await self.recorders[channel].stop()

    async def stop_all(self) -> None:
        for ch in list(self.recorders):
            await self.stop(ch)

    def cleanup(self, buffer_seconds: float, protect: Callable[[str], float | None]) -> None:
        now = time.time()
        for ch, rec in self.recorders.items():
            keep_after = now - buffer_seconds
            needed = protect(ch)
            if needed is not None:
                keep_after = min(keep_after, needed - self.seg)
            rec.cleanup(keep_after)
