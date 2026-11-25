"""ffmpeg filter graphs that turn a 16:9 stream into a 1080x1920 vertical video."""
from __future__ import annotations

W, H = 1080, 1920


def _rect(r) -> str:
    x, y, w, h = r
    return f"crop=iw*{w}:ih*{h}:iw*{x}:ih*{y}"


def build(layout: str, fps: int, blur_zoom: float = 1.25, facecam: dict | None = None) -> str:
    """Returns a filter_complex ending in the label [v]."""
    if layout == "crop":
        # centre crop: best when the streamer is centred (just chatting, IRL)
        body = f"[0:v]scale=-2:{H},crop={W}:{H}"
    elif layout == "facecam" and facecam:
        cam_h = int(H * float(facecam.get("cam_share", 0.38))) // 2 * 2
        game_h = H - cam_h
        game = facecam.get("game", [0.2, 0.0, 0.6, 1.0])
        body = (f"[0:v]split=2[a][b];"
                f"[a]{_rect(facecam['cam'])},scale={W}:{cam_h}:force_original_aspect_ratio=increase,crop={W}:{cam_h}[cam];"
                f"[b]{_rect(game)},scale={W}:{game_h}:force_original_aspect_ratio=increase,crop={W}:{game_h}[game];"
                f"[cam][game]vstack")
    else:
        # blur (default): the real frame in the middle, a blurred zoomed copy filling the rest
        fg_h = int(W * 9 / 16 * blur_zoom) // 2 * 2
        body = (f"[0:v]split=2[bg][fg];"
                f"[bg]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},boxblur=24:3,eq=brightness=-0.10[bgb];"
                f"[fg]scale=-2:{fg_h},crop='min(iw,{W})':{fg_h}[fgs];"
                f"[bgb][fgs]overlay=(W-w)/2:(H-h)/2")
    return f"{body},fps={fps},setsar=1,format=yuv420p[v]"
