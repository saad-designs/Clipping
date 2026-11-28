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
    overlay: bool = False                       # True: cam is the overlay's real border, not an estimate

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


def persistent_edges(frames: list[np.ndarray]) -> tuple[np.ndarray, np.ndarray]:
    """Edge strength that stays put across frames: (vertical-line map, horizontal-line map).

    A webcam overlay's border is in the same place in every frame; game edges move. The median
    over frames keeps the first and washes out the second.
    """
    import cv2
    vs, hs = [], []
    for f in frames:
        g = cv2.GaussianBlur(cv2.cvtColor(f, cv2.COLOR_BGR2GRAY).astype(np.float32), (3, 3), 0)
        vs.append(np.abs(cv2.Sobel(g, cv2.CV_32F, 1, 0, ksize=3)))
        hs.append(np.abs(cv2.Sobel(g, cv2.CV_32F, 0, 1, ksize=3)))
    return np.median(np.stack(vs), axis=0), np.median(np.stack(hs), axis=0)


LINE_ON = 0.5    # share of a candidate line's length that must be a clear edge to count as a border
STRIP = 12       # px each side of a candidate line when checking "game moves outside, room still inside"


def _continuity(edge_band: np.ndarray, thr: float) -> np.ndarray:
    """For each candidate line (axis 1), the share of its length (axis 0) that's a clear edge.
    A 3-px tolerance across the line absorbs scaling blur."""
    on = edge_band >= thr
    on = on | np.roll(on, 1, axis=1) | np.roll(on, -1, axis=1)
    return on.mean(axis=0)


def _border(cont: np.ndarray, motion: np.ndarray, lo: int, hi: int, frame_edge: int, level: float,
            fallback: float) -> float:
    """One overlay border between the face and a frame edge.

    Candidates are long unbroken straight lines in [lo, hi), strongest first. A candidate counts
    only if the picture moves more on its outside (game) than its inside (the streamer's room):
    that rules out a headboard or shelf inside the webcam. No candidate passes: if it's still
    all the way to the frame edge the overlay runs there, otherwise keep `fallback`.
    """
    n = len(cont)
    lo, hi = max(lo, STRIP), min(hi, n - STRIP)
    outside_first = frame_edge == 0                  # left/top: the game is at lower indices
    if hi > lo:
        seg = cont[lo:hi]
        peaks = [i for i in range(len(seg)) if seg[i] >= LINE_ON and seg[i] == seg[max(i - 3, 0):i + 4].max()]
        for i in sorted(peaks, key=lambda i: -seg[i]):
            p = lo + i
            before, after = motion[p - STRIP:p - 2].mean(), motion[p + 2:p + STRIP].mean()
            if (before - after if outside_first else after - before) >= level:
                return float(p)
    edge_side = motion[:max(lo, 1)] if outside_first else motion[min(hi, n - 1):]
    inside = motion[lo:hi] if hi > lo else motion
    if edge_side.size and edge_side.mean() <= inside.mean() + level / 2:   # still to the edge: overlay reaches it
        return float(frame_edge)
    return fallback


def motion_map(frames: list[np.ndarray]) -> np.ndarray:
    """How much each pixel changes across the sampled frames (temporal std, lightly blurred)."""
    import cv2
    stack = np.stack([cv2.cvtColor(f, cv2.COLOR_BGR2GRAY).astype(np.float32) for f in frames])
    return cv2.GaussianBlur(stack.std(axis=0), (9, 9), 0)


def find_overlay(frames: list[np.ndarray], face: tuple[float, float, float, float]) -> list[float] | None:
    """The real webcam overlay rectangle around a face (sample pixels in, fractions out).

    An overlay's border is a long straight edge that's in the same place in every frame; the
    webcam room has persistent edges too, but they're short and broken. So: for each side, the
    most unbroken line between the face and the frame edge, measured along the middle of the
    other axis; a side with no such line runs to the frame edge. None when the result doesn't
    look like a webcam overlay (the caller falls back to the estimate around the face).
    """
    if len(frames) < 6:
        return None
    vmap, hmap = persistent_edges(frames)
    motion = motion_map(frames)
    H, W = vmap.shape
    fx, fy, fw, fh = face
    thr = max(12.0, float(np.percentile(np.maximum(vmap, hmap), 92)))
    level = max(3.0, 0.2 * float(np.median(motion)))
    est = expand_to_cam(face)
    left, top = est[0] * W, est[1] * H
    right, bottom = left + est[2] * W, top + est[3] * H
    # measure lines over the face's neighbourhood only (inside the overlay whatever its size)
    r0, r1 = int(max(fy - 0.3 * fh, 0)), int(min(fy + 1.6 * fh, H))
    c0, c1 = int(max(fx - 0.6 * fw, 0)), int(min(fx + 1.6 * fw, W))
    for _ in range(2):
        vcont, vmot = _continuity(vmap[r0:r1], thr), motion[r0:r1].mean(axis=0)              # per column
        hcont, hmot = _continuity(hmap[:, c0:c1].T, thr), motion[:, c0:c1].mean(axis=1)       # per row
        left = _border(vcont, vmot, int(fx - 5 * fw), int(fx - 0.1 * fw), 0, level, left)
        right = _border(vcont, vmot, int(fx + 0.9 * fw), int(fx + 6 * fw), W, level, right)
        top = _border(hcont, hmot, int(fy - 4 * fh), int(fy - 0.1 * fh), 0, level, top)
        bottom = _border(hcont, hmot, int(fy + 1.2 * fh), int(fy + 6 * fh), H, level, bottom)
        # second pass: measure along the middle 60% of the box just found (longer = surer)
        r0, r1 = int(top + 0.2 * (bottom - top)), int(bottom - 0.2 * (bottom - top))
        c0, c1 = int(left + 0.2 * (right - left)), int(right - 0.2 * (right - left))
    w, h = right - left, bottom - top
    if not (fw * 1.5 <= w <= W * 0.6 and fh * 1.5 <= h <= H * 0.75 and 0.8 <= w / max(h, 1) <= 2.6):
        return None
    return [round(left / W, 4), round(top / H, 4), round(w / W, 4), round(h / H, 4)]


def detect(path, frames: list[np.ndarray] | None = None) -> FacecamResult:
    frames = frames if frames is not None else sample_frames(path, 16)
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
    overlay = None
    try:
        overlay = find_overlay(frames, (fx, fy, fw, fh))
    except Exception as e:   # never let the refinement break detection
        overlay = None
        note = f"overlay search failed: {e}"
    else:
        note = "overlay border found" if overlay else "overlay border not found: estimated from the face"
    return FacecamResult("webcam", cam=overlay or expand_to_cam((fx, fy, fw, fh)), face=face,
                         confidence=round(share, 2), frames=len(frames), notes=[note],
                         overlay=bool(overlay))
