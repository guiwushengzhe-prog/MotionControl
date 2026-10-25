"""生成与网页矢量标志一致的多尺寸 Windows 图标；仅构建时需要 Pillow。"""
from pathlib import Path
from PIL import Image, ImageDraw


def build(target):
    scale = 4
    image = Image.new("RGBA", (256 * scale, 256 * scale))
    draw = ImageDraw.Draw(image)

    def box(values):
        return tuple(round(v * scale) for v in values)

    draw.rounded_rectangle(box((8, 8, 248, 248)), radius=58 * scale, fill="#202225")
    for bounds, start, end in [((45, 33, 217, 223), 157, 203), ((39, 33, 211, 223), -23, 23)]:
        draw.arc(box(bounds), start, end, fill="#1685f8", width=12 * scale)
    draw.ellipse(box((111, 48, 145, 82)), fill="#f4f5f6")
    for points in [((81, 106), (128, 122), (175, 106)), ((128, 96), (128, 153)),
                   ((99, 196), (128, 150), (157, 196))]:
        draw.line([box(p) for p in points], fill="#f4f5f6", width=14 * scale, joint="curve")
        for x, y in points:
            draw.ellipse(box((x - 7, y - 7, x + 7, y + 7)), fill="#f4f5f6")
    image.resize((256, 256), Image.Resampling.LANCZOS).save(target, sizes=[(s, s) for s in (16, 24, 32, 48, 64, 128, 256)])


if __name__ == "__main__":
    build(Path(__file__).resolve().parents[2] / "web" / "assets" / "motioncontrol.ico")
