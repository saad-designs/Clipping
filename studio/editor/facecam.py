"""Find the streamer's webcam in a clip, so it can go on top of the vertical video.

Samples frames, runs OpenCV's face detector, and keeps the face position that shows up
in the same place in most frames (game characters move around; a webcam overlay doesn't).

Result kinds:
  webcam      small face in a fixed spot -> "facecam on top, game below" layout
  center_face big face near the middle (just chatting / IRL) -> vertical crop on the face
  none        no steady face -> game-only layouts
"""
from __future__ import annotations

import subprocess
from dataclasses import asdict, dataclass, field

import numpy as np

SAMPLE_W, SAMPLE_H = 960, 540


@dataclass
class FacecamResult:
    kind: str                                   # webcam | center_face | none
    cam: list[float] | None = None              # [x, y, w, h] as fractions of the frame
    face: list[float] | None = None
    confidence: float = 0.0                     # share of sampled frames with the face there
    frames: int = 0
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def sample_frames(path, count: int = 12, duration: float | None = None) -> list[np.ndarray]:
    """`count` evenly spaced frames as BGR arrays (960x540)."""
    if duration is None:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                             capture_output=True, text=True)
        duration = float(out.stdout.strip() or 0) or 10.0
    fps = count / max(duration, 1.0)
    raw = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(path), "-vf",
                          f"fps={fps:.4f},scale={SAMPLE_W}:{SAMPLE_H}", "-f", "rawvideo", "-pix_fmt", "bgr24", "-"],
                         capture_output=True).stdout
    size = SAMPLE_W * SAMPLE_H * 3
    return [np.frombuffer(raw[i:i + size], np.uint8).reshape(SAMPLE_H, SAMPLE_W, 3)
            for i in range(0, len(raw) - size + 1, size)][:count]


def _skin_ratio(frame: np.ndarray, box: tuple[int, int, int, int]) -> float:
    """Share of skin-coloured pixels in a box: rejects HUD icons and logos that look face-like."""
    import cv2
    x, y, w, h = box
    patch = frame[max(y, 0):y + h, max(x, 0):x + w]
    if patch.size == 0:
        return 0.0
    ycc = cv2.cvtColor(patch, cv2.COLOR_BGR2YCrCb)
    cr, cb = ycc[..., 1], ycc[..., 2]
    skin = (cr > 133) & (cr < 180) & (cb > 75) & (cb < 135)
    return float(skin.mean())


def _detect(frames: list[np.ndarray]) -> list[list[tuple[int, int, int, int]]]:
    """Faces per frame, in 960x540 sample coordinates.

    Webcam faces are small, so frames are upscaled 2x and contrast-boosted (CLAHE) before
    detection. Profile faces are searched both ways, since streamers often look at a
    second monitor.
    """
    import cv2
    front = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    profile = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_profileface.xml")
    clahe = cv2.createCLAHE(2.0, (8, 8))
    W2, H2 = SAMPLE_W * 2, SAMPLE_H * 2
    out = []
    for f in frames:
        g = clahe.apply(cv2.resize(cv2.cvtColor(f, cv2.COLOR_BGR2GRAY), (W2, H2)))
        boxes = list(front.detectMultiScale(g, 1.08, 5, minSize=(40, 40)))
        boxes += list(profile.detectMultiScale(g, 1.08, 5, minSize=(40, 40)))
        boxes += [(W2 - x - w, y, w, h) for (x, y, w, h) in profile.detectMultiScale(cv2.flip(g, 1), 1.08, 5, minSize=(40, 40))]
        found = []
        for (x, y, w, h) in boxes:
            b = (int(x / 2), int(y / 2), int(w / 2), int(h / 2))
            if _skin_ratio(f, b) >= 0.2:
                found.append(b)
        out.append(found)
    return out


def _cluster(per_frame: list[list[tuple]], tol: float = 0.06) -> tuple[list[tuple], int]:
    """Group detections whose centres are within `tol` of the frame; return the biggest group."""
    groups: list[list[tuple]] = []
    for boxes in per_frame:
        for (x, y, w, h) in boxes:
            cx, cy = (x + w / 2) / SAMPLE_W, (y + h / 2) / SAMPLE_H
            for g in groups:
                gx, gy = g[0][4], g[0][5]
                if abs(cx - gx) < tol and abs(cy - gy) < tol:
                    g.append((x, y, w, h, cx, cy))
                    break
            else:
                groups.append([(x, y, w, h, cx, cy)])
    if not groups:
        return [], 0
    best = max(groups, key=len)
    return best, len(best)


def expand_to_cam(face: tuple[float, float, float, float]) -> list[float]:
    """Face box (pixels in the 960x540 sample) -> likely webcam overlay rectangle (fractions).

    Webcam overlays are roughly 16:9 to 4:3 with the face filling ~35-40% of the width,
    a little above centre. Clamped to the frame.
    """
    x, y, w, h = face
    cam_w = min(w * 2.8, SAMPLE_W * 0.42)
    cam_h = min(cam_w * 0.75, SAMPLE_H * 0.55)
    cx, cy = x + w / 2, y + h / 2 + h * 0.25
    left = min(max(cx - cam_w / 2, 0), SAMPLE_W - cam_w)
    top = min(max(cy - cam_h / 2, 0), SAMPLE_H - cam_h)
    return [round(left / SAMPLE_W, 4), round(top / SAMPLE_H, 4), round(cam_w / SAMPLE_W, 4), round(cam_h / SAMPLE_H, 4)]


def detect(path, frames: list[np.ndarray] | None = None) -> FacecamResult:
    frames = frames if frames is not None else sample_frames(path)
    if not frames:
        return FacecamResult("none", notes=["no frames"])
    try:
        per_frame = _detect(frames)
    except Exception as e:   # e.g. an OpenCV build without the face detector files
        return FacecamResult("none", frames=len(frames), notes=[f"face detection unavailable: {e}"])
    group, _ = _cluster(per_frame)
    # count frames, not boxes: frontal + profile can both fire on the same face in one frame
    hits = sum(1 for boxes in per_frame if any(
        abs((x + w / 2) / SAMPLE_W - group[0][4]) < 0.06 and abs((y + h / 2) / SAMPLE_H - group[0][5]) < 0.06
        for (x, y, w, h) in boxes)) if group else 0
    share = hits / len(frames)
    if share < 0.4:
        return FacecamResult("none", confidence=round(share, 2), frames=len(frames),
                             notes=["no face in a steady position"])
    fx = float(np.median([g[0] for g in group])); fy = float(np.median([g[1] for g in group]))
    fw = float(np.median([g[2] for g in group])); fh = float(np.median([g[3] for g in group]))
    face = [round(fx / SAMPLE_W, 4), round(fy / SAMPLE_H, 4), round(fw / SAMPLE_W, 4), round(fh / SAMPLE_H, 4)]
    cx = (fx + fw / 2) / SAMPLE_W
    if fw / SAMPLE_W > 0.16 and 0.25 < cx < 0.75:
        return FacecamResult("center_face", face=face, confidence=round(share, 2), frames=len(frames))
    return FacecamResult("webcam", cam=expand_to_cam((fx, fy, fw, fh)), face=face,
                         confidence=round(share, 2), frames=len(frames))
