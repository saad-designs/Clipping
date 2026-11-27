"""Turn an edit plan into the finished vertical video.

One ffmpeg pass: trim -> layout (facecam on top / face crop / blur fit / game only) ->
zoom punch-ins -> logo -> captions + hook (ASS) -> sound hits mixed in -> loudness levelled.
An optional campaign intro/outro is joined in a second pass.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

from studio.editor import captions, encode

log = logging.getLogger("render")

W, H = 1080, 1920
SFX_DIR = Path(__file__).resolve().parent.parent / "assets" / "sfx"
SFX = {p.stem for p in SFX_DIR.glob("*.wav")}
CAM_SHARE = 0.38          # facecam_split: top part of the frame for the webcam
SFX_LEVEL = 0.6           # sounds sit under the voice (the files peak near full scale)
TARGET_LUFS = -14.0


def stream_gain(src: Path, start: float, dur: float) -> float:
    """dB to bring this part of the stream to -14 LUFS. Capped so near-silent clips aren't blown up into hiss."""
    import json
    import re
    r = subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", "-ss", f"{start:.2f}", "-t", f"{dur:.2f}", "-i", str(src),
                        "-vn", "-af", "loudnorm=print_format=json", "-f", "null", "-"], capture_output=True, text=True)
    m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", r.stderr)
    try:
        measured = float(json.loads(m.group(0))["input_i"]) if m else None
    except (ValueError, KeyError):
        measured = None
    if measured is None or measured < -70:     # silence / no audio: leave it alone
        return 0.0
    return max(-20.0, min(24.0, TARGET_LUFS - measured))


def _even(n: float) -> int:
    return int(n) // 2 * 2


def _rect(r) -> str:
    x, y, w, h = r
    return f"crop=iw*{w:.4f}:ih*{h:.4f}:iw*{x:.4f}:ih*{y:.4f}"


def avoid_cam(gx: float, cam, window: float, margin: float = 0.06) -> float:
    """Slide the game window (share `window` of the source width, placed at gx) off the webcam box,
    so the webcam isn't shown twice (once on top, once as a sliver in the game). Source-width units."""
    x, _, w, _ = cam
    left = gx * (1 - window)
    if left + window <= x - margin or left >= x + w + margin or window >= 1:
        return gx
    if x + w / 2 >= 0.5:       # webcam on the right: put the window's right edge left of it
        left = max(0.0, x - margin - window)
    else:
        left = min(1 - window, x + w + margin)
    return left / (1 - window)


def layout_graph(layout: str, facecam: dict | None, game_x: float = 0.5, blur_zoom: float = 1.25) -> tuple[str, dict]:
    """Filter graph from [0:v] to [lay] (1080x1920). Also returns where text fits best."""
    gx = min(max(game_x, 0.0), 1.0)
    if layout == "facecam_split" and facecam and facecam.get("cam"):
        cam_h = _even(H * CAM_SHARE)
        game_h = H - cam_h
        gx = avoid_cam(gx, facecam["cam"], W / (game_h * 16 / 9))   # streams are 16:9
        g = (f"[0:v]split=2[a][b];"
             f"[a]{_rect(facecam['cam'])},scale={W}:{cam_h}:force_original_aspect_ratio=increase,crop={W}:{cam_h}[cam];"
             # game: full height, a 1080:game_h window, slid horizontally by game_x
             f"[b]scale=-2:{game_h},crop={W}:{game_h}:(iw-{W})*{gx:.3f}:0[game];"
             f"[cam][game]vstack[lay]")
        return g, {"hook_y": cam_h, "caption_y": cam_h + int(game_h * 0.62)}
    if layout == "face_crop":
        fx = gx
        if facecam and facecam.get("face"):
            f = facecam["face"]
            fx = f[0] + f[2] / 2
        g = f"[0:v]scale=-2:{H},crop={W}:{H}:min(max(iw*{fx:.3f}-{W // 2}\\,0)\\,iw-{W}):0[lay]"
        return g, {"hook_y": int(H * 0.15), "caption_y": int(H * 0.70)}
    if layout == "game_only":
        g = f"[0:v]scale=-2:{H},crop={W}:{H}:(iw-{W})*{gx:.3f}:0[lay]"
        return g, {"hook_y": int(H * 0.15), "caption_y": int(H * 0.70)}
    # blur_fit (default): real frame in the middle, blurred zoomed copy behind it
    fg_h = _even(W * 9 / 16 * blur_zoom)
    g = (f"[0:v]split=2[bg][fg];"
         f"[bg]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},boxblur=24:3,eq=brightness=-0.12[bgb];"
         f"[fg]scale=-2:{fg_h},crop='min(iw,{W})':{fg_h}[fgs];"
         f"[bgb][fgs]overlay=(W-w)/2:(H-h)/2[lay]")
    top = (H - fg_h) // 2
    return g, {"hook_y": top // 2 + 40, "caption_y": top + fg_h + (H - top - fg_h) // 3}


def zoom_expr(zooms: list[dict]) -> str | None:
    """Scale factor over time: quick punch in (0.12s), hold (0.7s), ease out (0.4s)."""
    terms = []
    for z in zooms:
        a, s = float(z["at"]), float(z.get("strength", 0.15))
        terms.append(f"{s:.3f}*(between(t,{a:.2f},{a + 0.12:.2f})*(t-{a:.2f})/0.12"
                     f"+between(t,{a + 0.12:.2f},{a + 0.82:.2f})"
                     f"+between(t,{a + 0.82:.2f},{a + 1.22:.2f})*(1-(t-{a + 0.82:.2f})/0.4))")
    return "1+" + "+".join(terms) if terms else None


def render(src: Path, plan: dict, out: Path, campaign: dict | None = None, words=None, facecam: dict | None = None,
           caption_style: dict | None = None, encoder: str = "auto", fps: int = 30) -> Path:
    """Render `src` according to a validated plan (see studio.director.validate)."""
    campaign = campaign or {}
    start, end = plan["keep"]
    dur = end - start
    graph, spots = layout_graph(plan["layout"], facecam, plan.get("game_x", 0.5))
    chain = "[lay]"
    zx = zoom_expr([{"at": z["at"] - start, "strength": z.get("strength", 0.15)} for z in plan.get("zooms", [])])
    if zx:
        graph += (f";{chain}scale=w='trunc({W}*({zx})/2)*2':h='trunc({H}*({zx})/2)*2':eval=frame,"
                  f"crop={W}:{H}[zm]")
        chain = "[zm]"

    inputs = ["-ss", f"{start:.2f}", "-t", f"{dur:.2f}", "-i", str(Path(src).resolve())]
    n_in = 1
    logo = campaign.get("logo_path")
    if logo and Path(logo).exists() and plan.get("logo"):
        lw = _even(W * float(plan["logo"].get("scale", 0.18)))
        corner = plan["logo"].get("corner", "top_right")
        m = 48
        x = m if "left" in corner else f"W-w-{m}"
        y = 90 if "top" in corner else f"H-h-{int(H * 0.2)}"   # bottom logo sits above the app's buttons
        opacity = float(campaign.get("logo_opacity", 0.92))
        inputs += ["-loop", "1", "-i", str(Path(logo).resolve())]
        graph += (f";[{n_in}:v]scale={lw}:-2,format=rgba,colorchannelmixer=aa={opacity:.2f}[lg];"
                  f"{chain}[lg]overlay={x}:{y}:shortest=1[lgo]")
        chain = "[lgo]"
        n_in += 1

    sfx_inputs = []
    for s in plan.get("sfx", []):
        if s.get("sound") in SFX:
            sfx_inputs.append((n_in, max(0.0, float(s["at"]) - start), float(s.get("volume", 0.8)), s["sound"]))
            inputs += ["-i", str(SFX_DIR / f"{s['sound']}.wav")]
            n_in += 1

    enc = encode.pick(encoder)
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        style = dict(caption_style or {})
        ass = captions.build_ass(words or [], start, end, style, preset=plan.get("caption_style"),
                                 emphasis=plan.get("emphasis_words", []), hook=plan.get("hook"),
                                 caption_y=spots["caption_y"], hook_y=spots["hook_y"],
                                 font=campaign.get("font_name"))
        (tmp / "captions.ass").write_text(ass, encoding="utf-8")
        fonts = tmp / "fonts"
        shutil.copytree(captions.FONTS_DIR, fonts)
        if campaign.get("font_path") and Path(campaign["font_path"]).exists():
            shutil.copy(campaign["font_path"], fonts)
        # bare relative names: ffmpeg runs inside tmp, which avoids Windows drive-letter escaping
        graph += f";{chain}ass=captions.ass:fontsdir=fonts,fps={fps},setsar=1,format=yuv420p[v]"

        has_audio = _has_audio(src)
        if has_audio:
            # One steady gain to -14 LUFS (measured first), not loudnorm's dynamic mode: on a quiet
            # stream that pumped the background hiss up in every pause. Sounds go in *after* the
            # stream is levelled, under the voice, then a limiter catches peaks.
            gain = stream_gain(src, start, dur)
            mix = [f"[0:a]aresample=48000,highpass=f=60,volume={gain:.2f}dB[a0]"]
            labels = ["[a0]"]
            for idx, at, vol, _ in sfx_inputs:
                ms = int(at * 1000)
                mix.append(f"[{idx}:a]aresample=48000,aformat=channel_layouts=stereo,volume={min(vol, 0.5) * SFX_LEVEL:.3f},"
                           f"adelay={ms}|{ms}[s{idx}]")
                labels.append(f"[s{idx}]")
            if len(labels) > 1:
                mix.append(f"{''.join(labels)}amix=inputs={len(labels)}:duration=first:normalize=0[am]")
                src_a = "[am]"
            else:
                src_a = "[a0]"
            mix.append(f"{src_a}alimiter=limit=0.84:level=disabled,aresample=48000[a]")
            graph += ";" + ";".join(mix)

        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *inputs, "-filter_complex", graph,
               "-map", "[v]", "-t", f"{dur:.2f}"]
        if has_audio:
            cmd += ["-map", "[a]", "-c:a", "aac", "-b:a", "192k"]
        cmd += [*encode.video_args(enc), "-movflags", "+faststart", "out.mp4"]
        r = subprocess.run(cmd, cwd=tmp, capture_output=True, text=True)
        if r.returncode != 0 and enc != "libx264":
            log.warning("%s failed, retrying with libx264: %s", enc, r.stderr[-300:])
            i = cmd.index("-c:v")
            cmd[i:i + len(encode.video_args(enc))] = encode.video_args("libx264")
            r = subprocess.run(cmd, cwd=tmp, capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"ffmpeg failed: {r.stderr[-1200:]}")
        out.parent.mkdir(parents=True, exist_ok=True)
        final = tmp / "out.mp4"
        intro, outro = campaign.get("intro_path"), campaign.get("outro_path")
        if (intro and Path(intro).exists()) or (outro and Path(outro).exists()):
            final = _join(tmp, [intro, str(final), outro], enc, fps)
        shutil.move(str(final), out)
    return out


def _has_audio(path) -> bool:
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index",
                        "-of", "csv=p=0", str(path)], capture_output=True, text=True)
    return bool(r.stdout.strip())


def _duration(path) -> float:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                       capture_output=True, text=True)
    return float(r.stdout.strip() or 0)


def _join(tmp: Path, parts: list[str | None], enc: str, fps: int) -> Path:
    """Campaign intro/outro + main clip, all normalised to 1080x1920, fps, stereo 48k."""
    files = [p for p in parts if p and Path(p).exists()]
    inputs, chains = [], []
    for i, f in enumerate(files):
        inputs += ["-i", str(Path(f).resolve())]
        silent = not _has_audio(f)
        if silent:   # give silent clips a silent track of the same length, so concat lines up
            inputs += ["-f", "lavfi", "-t", f"{_duration(f):.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
        chains.append((i, silent))
    filt, labels, idx = [], [], 0
    for i, silent in chains:
        filt.append(f"[{idx}:v]scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,"
                    f"fps={fps},setsar=1,format=yuv420p[v{i}]")
        a_idx = idx + 1 if silent else idx
        filt.append(f"[{a_idx}:a]aresample=48000,aformat=channel_layouts=stereo[a{i}]")
        labels.append(f"[v{i}][a{i}]")
        idx += 2 if silent else 1
    filt.append(f"{''.join(labels)}concat=n={len(chains)}:v=1:a=1[v][a]")
    out = tmp / "joined.mp4"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *inputs, "-filter_complex", ";".join(filt),
                    "-map", "[v]", "-map", "[a]", *encode.video_args(enc), "-c:a", "aac", "-b:a", "192k",
                    "-movflags", "+faststart", str(out)], check=True, capture_output=True)
    return out
