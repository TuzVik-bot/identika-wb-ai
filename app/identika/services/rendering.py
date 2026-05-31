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


def _svg_open(w: int, h: int) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
        f'width="100%" height="100%" viewBox="0 0 {w} {h}" preserveAspectRatio="xMidYMid meet">'
    )


def _image_tag(href: str, x: int, y: int, width: int, height: int, *, fit: str = "meet") -> str:
    escaped_href = html.escape(href, quote=True)
    return (
        f'<image href="{escaped_href}" xlink:href="{escaped_href}" '
        f'x="{x}" y="{y}" width="{width}" height="{height}" '
        f'preserveAspectRatio="xMidYMid {fit}"/>'
    )


def _text_header_parts(slide: SlideSpec, *, accent: str) -> list[str]:
    title_lines = _wrap(slide.title, 20)
    subtitle_lines = _wrap(slide.subtitle, 32)
    parts: list[str] = []
    y = 90
    parts.append(
        f'<text x="70" y="{y}" font-family="Arial, DejaVu Sans, sans-serif" '
        f'font-size="26" fill="#7b8794">Слайд {slide.index:02d}</text>'
    )
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
    return parts


def _text_bullet_parts(slide: SlideSpec, *, accent: str) -> list[str]:
    parts: list[str] = []
    y = 950
    for bullet in slide.bullets[:5]:
        clean = html.escape(str(bullet))
        parts.append(f'<circle cx="86" cy="{y-10}" r="8" fill="{accent}"/>')
        parts.append(
            f'<text x="110" y="{y}" font-family="Arial, DejaVu Sans, sans-serif" '
            f'font-size="28" fill="#102a43">{clean}</text>'
        )
        y += 42
    return parts


def _frame_border(w: int, h: int) -> str:
    return f'<rect x="36" y="36" width="{w-72}" height="{h-72}" rx="22" fill="none" stroke="#d8e2ef" stroke-width="3"/>'


def _text_overlay_parts(slide: SlideSpec, *, accent: str) -> list[str]:
    parts = _text_header_parts(slide, accent=accent)
    parts.extend(_text_bullet_parts(slide, accent=accent))
    parts.append(_frame_border(slide.width, slide.height))
    return parts


def _render_full_background(slide: SlideSpec, background_href: str) -> bytes:
    w, h = slide.width, slide.height
    accent = "#2f6fed" if slide.role == "hero" else "#243b53"
    parts = [
        _svg_open(w, h),
        _image_tag(background_href, 0, 0, w, h, fit="meet"),
        '<defs><linearGradient id="topScrim" x1="0" y1="0" x2="0" y2="1">'
        '<stop offset="0%" stop-color="#ffffff" stop-opacity="0.92"/>'
        '<stop offset="55%" stop-color="#ffffff" stop-opacity="0.55"/>'
        '<stop offset="100%" stop-color="#ffffff" stop-opacity="0"/>'
        "</linearGradient></defs>",
        f'<rect x="0" y="0" width="{w}" height="520" fill="url(#topScrim)"/>',
    ]
    if slide.bullets:
        parts.extend(
            [
                '<defs><linearGradient id="bottomScrim" x1="0" y1="0" x2="0" y2="1">'
                '<stop offset="0%" stop-color="#ffffff" stop-opacity="0"/>'
                '<stop offset="35%" stop-color="#ffffff" stop-opacity="0.72"/>'
                '<stop offset="100%" stop-color="#ffffff" stop-opacity="0.95"/>'
                "</linearGradient></defs>",
                f'<rect x="0" y="820" width="{w}" height="380" fill="url(#bottomScrim)"/>',
            ]
        )
    parts.extend(_text_overlay_parts(slide, accent=accent))
    parts.append("</svg>")
    return "\n".join(parts).encode("utf-8")


def _render_white_background(slide: SlideSpec, source_href: str) -> bytes:
    w, h = slide.width, slide.height
    margin_x = 80
    margin_y = 120
    img_w = w - margin_x * 2
    img_h = h - margin_y * 2
    parts = [
        _svg_open(w, h),
        f'<rect width="{w}" height="{h}" fill="#ffffff"/>',
        _image_tag(source_href, margin_x, margin_y, img_w, img_h, fit="meet"),
        f'<rect x="36" y="36" width="{w-72}" height="{h-72}" rx="22" fill="none" stroke="#e2e8f0" stroke-width="3"/>',
        "</svg>",
    ]
    return "\n".join(parts).encode("utf-8")


def _render_product_composite(slide: SlideSpec, source_href: str) -> bytes:
    w, h = slide.width, slide.height
    bg = "#f4f7fb"
    accent = "#2f6fed" if slide.role == "hero" else "#243b53"
    product_y = 520 if slide.role != "hero" else 570
    parts = [
        _svg_open(w, h),
        f'<rect width="{w}" height="{h}" fill="{bg}"/>',
        _frame_border(w, h),
    ]
    parts.extend(_text_header_parts(slide, accent=accent))
    parts.extend(
        [
            f'<rect x="150" y="{product_y}" width="600" height="360" rx="34" fill="#ffffff" stroke="#bcccdc" stroke-width="3"/>',
            _image_tag(source_href, 170, product_y + 20, 560, 320, fit="meet"),
        ]
    )
    parts.extend(_text_bullet_parts(slide, accent=accent))
    parts.append("</svg>")
    return "\n".join(parts).encode("utf-8")


def _render_placeholder(slide: SlideSpec) -> bytes:
    w, h = slide.width, slide.height
    bg = "#ffffff" if slide.role == "white_background" else "#f4f7fb"
    accent = "#2f6fed" if slide.role == "hero" else "#243b53"
    product_y = 520 if slide.role != "hero" else 570
    parts = [
        _svg_open(w, h),
        f'<rect width="{w}" height="{h}" fill="{bg}"/>',
        f'<rect x="36" y="36" width="{w-72}" height="{h-72}" rx="22" fill="none" stroke="#d8e2ef" stroke-width="3"/>',
    ]
    parts.extend(_text_overlay_parts(slide, accent=accent))
    parts.extend(
        [
            f'<rect x="150" y="{product_y}" width="600" height="360" rx="34" fill="#ffffff" stroke="#bcccdc" stroke-width="3"/>',
            f'<ellipse cx="450" cy="{product_y + 260}" rx="210" ry="44" fill="#d9e2ec"/>',
            f'<rect x="315" y="{product_y + 70}" width="270" height="210" rx="36" fill="#e6f0ff" stroke="#2f6fed" stroke-width="5"/>',
            f'<text x="450" y="{product_y + 190}" text-anchor="middle" font-family="Arial, DejaVu Sans, sans-serif" font-size="34" font-weight="700" fill="#102a43">ТОВАР</text>',
        ]
    )
    parts.append("</svg>")
    return "\n".join(parts).encode("utf-8")


def render_slide_svg(
    slide: SlideSpec,
    *,
    source_image_href: str | None = None,
    background_image_href: str | None = None,
    source_image_data_uri: str | None = None,
    background_image_data_uri: str | None = None,
) -> bytes:
    source_image_href = source_image_href or source_image_data_uri
    background_image_href = background_image_href or background_image_data_uri

    if slide.role == "white_background":
        product_href = source_image_href or background_image_href
        if product_href:
            return _render_white_background(slide, product_href)
        return _render_placeholder(slide)

    if background_image_href:
        return _render_full_background(slide, background_image_href)
    if source_image_href:
        return _render_product_composite(slide, source_image_href)
    return _render_placeholder(slide)


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
