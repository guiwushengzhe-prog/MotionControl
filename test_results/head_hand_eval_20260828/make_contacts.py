"""Create timestamped visual evidence sheets for the two supplied videos."""

from __future__ import annotations

from pathlib import Path

import cv2
from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parent
OUT = ROOT / "video_review"
OUT.mkdir(parents=True, exist_ok=True)
VIDEOS = [
    Path(r"D:\xwechat_files\wxid_wczned99ujlf22_545c\msg\video\2026-08\bf5ad4437d893c1bbb493ff90ab5d368.mp4"),
    Path(r"D:\xwechat_files\wxid_wczned99ujlf22_545c\msg\video\2026-08\c7fbc865832d892ec4aaa362fda5800d.mp4"),
]


def sheet(video: Path) -> None:
    cap = cv2.VideoCapture(str(video))
    fps = float(cap.get(cv2.CAP_PROP_FPS) or 30.0)
    frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    duration = frames / max(fps, 1e-6)
    times = [float(t) for t in range(0, int(duration) + 1, 4)]
    thumbs = []
    font = ImageFont.load_default()
    for t in times:
        cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000.0)
        ok, frame = cap.read()
        if not ok:
            continue
        frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        image = Image.fromarray(frame).resize((180, 320))
        canvas = Image.new("RGB", (180, 344), "black")
        canvas.paste(image, (0, 24))
        draw = ImageDraw.Draw(canvas)
        draw.text((5, 5), f"t={t:.1f}s", fill="white", font=font)
        thumbs.append(canvas)
    cap.release()
    cols = 5
    rows = (len(thumbs) + cols - 1) // cols
    out = Image.new("RGB", (cols * 180, rows * 344), "#202020")
    for index, thumb in enumerate(thumbs):
        out.paste(thumb, ((index % cols) * 180, (index // cols) * 344))
    out.save(OUT / f"{video.stem}_timestamp_contact.jpg", quality=92)


for video_path in VIDEOS:
    sheet(video_path)
print(OUT)
