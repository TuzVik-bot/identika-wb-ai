from __future__ import annotations

import base64
import html
import io
import json
import textwrap
import zipfile
from pathlib import Path

from identika.models import GenerationResult, SlideSpec


def _wrap(text: str, width: int) -> list[str]:
    lines: list[str] = []
    for part in str(text or "").splitlines() or [""]:
        lines.extend(textwrap.wrap(part, width=width) or [""])
    return lines


def image_to_data_uri(path: Path, media_type: str) -> str:
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{media_type};base64,{encoded}"


def render_slide_svg(
    slide: SlideSpec,
    *,
    source_image_data_uri: str | None = None,
    background_image_data_uri: str | None = None,
) -> bytes:
    bg = "#ffffff" if slide.role == "white_background" else "#f4f7fb"
    accent = "#2f6fed" if slide.role == "hero" else "#243b53"
    h = slide.height
    w = slide.width
    title_lines = _wrap(slide.title, 20)
    subtitle_lines = _wrap(slide.subtitle, 32)
    bullet_lines = slide.bullets[:5]
    y = 90
    image_uri = background_image_data_uri or source_image_data_uri
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" width="{w}" height="{h}" viewBox="0 0 {w} {h}">',
        f'<rect width="{w}" height="{h}" fill="{bg}"/>',
        f'<rect x="36" y="36" width="{w-72}" height="{h-72}" rx="22" fill="none" stroke="#d8e2ef" stroke-width="3"/>',
        f'<text x="70" y="{y}" font-family="Arial, DejaVu Sans, sans-serif" font-size="26" fill="#7b8794">Слайд {slide.index:02d}</text>',
    ]
    y += 72
    for line in title_lines[:3]:
        parts.append(
            f'<text x="70" y="{y}" font-family="Arial, DejaVu Sans, sans-serif" '
            f'font-size="52" font-weight="700" fill="{accent}">{html.escape(line)}</text>'
        )
        y += 60
    for line in subtitle_lines[:3]:
        parts.append(
            f'<text x="70" y="{y}" font-family="Arial, DejaVu Sans, sans-serif" '
            f'font-size="30" fill="#334e68">{html.escape(line)}</text>'
        )
        y += 42
    product_y = 520 if slide.role != "hero" else 570
    if image_uri:
        parts.extend(
            [
                f'<rect x="150" y="{product_y}" width="600" height="360" rx="34" fill="#ffffff" stroke="#bcccdc" stroke-width="3"/>',
                f'<image href="{html.escape(image_uri, quote=True)}" x="170" y="{product_y + 20}" width="560" height="320" preserveAspectRatio="xMidYMid meet"/>',
            ]
        )
    else:
        parts.extend(
            [
                f'<rect x="150" y="{product_y}" width="600" height="360" rx="34" fill="#ffffff" stroke="#bcccdc" stroke-width="3"/>',
                f'<ellipse cx="450" cy="{product_y + 260}" rx="210" ry="44" fill="#d9e2ec"/>',
                f'<rect x="315" y="{product_y + 70}" width="270" height="210" rx="36" fill="#e6f0ff" stroke="#2f6fed" stroke-width="5"/>',
                f'<text x="450" y="{product_y + 190}" text-anchor="middle" font-family="Arial, DejaVu Sans, sans-serif" font-size="34" font-weight="700" fill="#102a43">ТОВАР</text>',
            ]
        )
    y = 950
    for bullet in bullet_lines:
        clean = html.escape(str(bullet))
        parts.append(f'<circle cx="86" cy="{y-10}" r="8" fill="{accent}"/>')
        parts.append(
            f'<text x="110" y="{y}" font-family="Arial, DejaVu Sans, sans-serif" '
            f'font-size="28" fill="#102a43">{clean}</text>'
        )
        y += 42
    parts.append("</svg>")
    return "\n".join(parts).encode("utf-8")


def render_pdf_preview(result: GenerationResult) -> bytes:
    # Minimal valid PDF with text preview. Image-heavy rich layout is exported as SVG assets.
    lines = [f"Rich-content preview: {result.product.title}", ""]
    for slide in result.slides:
        lines.append(f"{slide.index}. {slide.title}")
        if slide.subtitle:
            lines.append(f"   {slide.subtitle}")
        for bullet in slide.bullets:
            lines.append(f"   - {bullet}")
        lines.append("")
    text = "\n".join(lines).replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    chunks = []
    y = 780
    for line in text.splitlines()[:55]:
        chunks.append(f"BT /F1 10 Tf 50 {y} Td ({line}) Tj ET")
        y -= 14
    stream = "\n".join(chunks).encode("utf-8")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
    ]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = [0]
    for idx, obj in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(f"{idx} 0 obj\n".encode())
        out.write(obj)
        out.write(b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objects)+1}\n0000000000 65535 f \n".encode())
    for off in offsets[1:]:
        out.write(f"{off:010d} 00000 n \n".encode())
    out.write(f"trailer << /Size {len(objects)+1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return out.getvalue()


def render_rich_html_preview(result: GenerationResult) -> bytes:
    blocks = []
    for block in result.rich.blocks:
        blocks.append(
            "<section class='rich-block'>"
            f"<h2>{html.escape(block.title)}</h2>"
            f"<p>{html.escape(block.text)}</p>"
            "</section>"
        )
    doc = f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Rich-контент · {html.escape(result.product.title)}</title>
  <style>
    body {{ margin: 0; font-family: Arial, sans-serif; color: #102a43; background: #f5f7fa; }}
    main {{ max-width: 860px; margin: 0 auto; padding: 28px; background: #fff; }}
    header {{ border-bottom: 1px solid #d9e2ec; padding-bottom: 18px; margin-bottom: 18px; }}
    h1 {{ font-size: 30px; margin: 0 0 8px; }}
    .meta {{ color: #627d98; }}
    .rich-block {{ border: 1px solid #d9e2ec; border-radius: 8px; padding: 18px; margin: 14px 0; }}
    .rich-block h2 {{ margin: 0 0 8px; color: #2f6fed; font-size: 22px; }}
    .rich-block p {{ margin: 0; line-height: 1.55; white-space: pre-wrap; }}
  </style>
</head>
<body>
  <main>
    <header>
      <h1>Rich-контент: {html.escape(result.product.title)}</h1>
      <div class="meta">Магазин: {html.escape(result.product.store_slug)} · слайдов: {len(result.slides)}</div>
    </header>
    {''.join(blocks)}
  </main>
</body>
</html>"""
    return doc.encode("utf-8")


def build_export_zip(result: GenerationResult, asset_blobs: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    manifest = result.model_dump(mode="json")
    manifest.pop("export_asset_id", None)
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        for slide in result.slides:
            if slide.asset_id and slide.asset_id in asset_blobs:
                zf.writestr(f"slides/slide_{slide.index:02d}.svg", asset_blobs[slide.asset_id])
        for block in result.rich.blocks:
            if block.asset_id and block.asset_id in asset_blobs:
                zf.writestr(f"rich/block_{block.index:02d}.svg", asset_blobs[block.asset_id])
        if result.rich.cover_asset_id and result.rich.cover_asset_id in asset_blobs:
            zf.writestr("rich/cover.svg", asset_blobs[result.rich.cover_asset_id])
        if result.rich.pdf_asset_id and result.rich.pdf_asset_id in asset_blobs:
            zf.writestr("rich/preview.pdf", asset_blobs[result.rich.pdf_asset_id])
        if result.rich.html_asset_id and result.rich.html_asset_id in asset_blobs:
            zf.writestr("rich/preview.html", asset_blobs[result.rich.html_asset_id])
    return buf.getvalue()
