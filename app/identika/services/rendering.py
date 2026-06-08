from __future__ import annotations

import base64
import binascii
import html
import io
import json
import textwrap
import zipfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps, UnidentifiedImageError

from identika.models import GenerationResult, ProductContext, QualityMode, RichBlock, SlideSpec
from identika.services.category_templates import CategoryTemplate

RICH_IMAGE_WIDTH = 1440
RICH_IMAGE_HEIGHT = 900
RICH_IMAGE_MEDIA_TYPE = "image/png"
RICH_IMAGE_EXTENSION = "png"
SLIDE_IMAGE_MEDIA_TYPE = "image/png"
SLIDE_IMAGE_EXTENSION = "png"


def _wrap(text: str, width: int) -> list[str]:
    lines: list[str] = []
    for part in str(text or "").splitlines() or [""]:
        lines.extend(textwrap.wrap(part, width=width) or [""])
    return lines


def image_to_data_uri(path: Path, media_type: str) -> str:
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{media_type};base64,{encoded}"


def _font(size: int, *, bold: bool = False) -> ImageFont.ImageFont:
    candidates = [
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf"
        if bold
        else "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
        if bold
        else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for candidate in candidates:
        if Path(candidate).exists():
            return ImageFont.truetype(candidate, size=size)
    return ImageFont.load_default(size=size)


def _wrap_for_width(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.ImageFont,
    max_width: int,
) -> list[str]:
    lines: list[str] = []
    for paragraph in str(text or "").splitlines() or [""]:
        words = paragraph.split()
        if not words:
            lines.append("")
            continue
        current = words[0]
        for word in words[1:]:
            candidate = f"{current} {word}"
            if draw.textbbox((0, 0), candidate, font=font)[2] <= max_width:
                current = candidate
            else:
                lines.append(current)
                current = word
        lines.append(current)
    return lines


def _image_from_data_uri(href: str | None) -> Image.Image | None:
    if not href or not href.startswith("data:image/") or ";base64," not in href:
        return None
    try:
        encoded = href.split(";base64,", 1)[1]
        image = Image.open(io.BytesIO(base64.b64decode(encoded)))
        return ImageOps.exif_transpose(image).convert("RGBA")
    except (binascii.Error, OSError, UnidentifiedImageError, ValueError):
        return None


def _paste_contained(canvas: Image.Image, source: Image.Image, box: tuple[int, int, int, int]) -> None:
    x1, y1, x2, y2 = box
    target_w = x2 - x1
    target_h = y2 - y1
    image = source.copy()
    image.thumbnail((target_w, target_h), Image.Resampling.LANCZOS)
    px = x1 + (target_w - image.width) // 2
    py = y1 + (target_h - image.height) // 2
    canvas.alpha_composite(image, (px, py))


def _is_squareish(image: Image.Image) -> bool:
    ratio = image.width / image.height if image.height else 1
    return 0.85 <= ratio <= 1.15


def _adapt_square_product_to_vertical(source: Image.Image, width: int, height: int) -> Image.Image:
    adapted = Image.new("RGBA", (width, height), "#ffffff")
    background = source.copy()
    ratio = max(width / background.width, height / background.height)
    background = background.resize(
        (max(1, int(background.width * ratio)), max(1, int(background.height * ratio))),
        Image.Resampling.LANCZOS,
    )
    left = max(0, (background.width - width) // 2)
    top = max(0, (background.height - height) // 2)
    background = background.crop((left, top, left + width, top + height))
    background = background.filter(ImageFilter.GaussianBlur(34))
    veil = Image.new("RGBA", (width, height), (255, 255, 255, 186))
    adapted.alpha_composite(background)
    adapted.alpha_composite(veil)
    _paste_contained(adapted, source, (80, 170, width - 80, height - 170))
    return adapted


def _paste_covered(canvas: Image.Image, source: Image.Image, box: tuple[int, int, int, int]) -> None:
    x1, y1, x2, y2 = box
    target_w = x2 - x1
    target_h = y2 - y1
    image = source.copy()
    ratio = max(target_w / image.width, target_h / image.height)
    resized = image.resize(
        (max(1, int(image.width * ratio)), max(1, int(image.height * ratio))),
        Image.Resampling.LANCZOS,
    )
    left = max(0, (resized.width - target_w) // 2)
    top = max(0, (resized.height - target_h) // 2)
    cropped = resized.crop((left, top, left + target_w, top + target_h))
    canvas.alpha_composite(cropped, (x1, y1))


def _draw_wrapped_text(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    text: str,
    font: ImageFont.ImageFont,
    max_width: int,
    *,
    fill: str,
    line_gap: int = 8,
    max_lines: int = 3,
) -> int:
    x, y = xy
    line_height = draw.textbbox((0, 0), "Ag", font=font)[3] + line_gap
    for line in _wrap_for_width(draw, text, font, max_width)[:max_lines]:
        draw.text((x, y), line, font=font, fill=fill)
        y += line_height
    return y


def _draw_slide_text(draw: ImageDraw.ImageDraw, slide: SlideSpec, *, accent: str) -> None:
    title_font = _font(52, bold=True)
    subtitle_font = _font(30)
    meta_font = _font(26)
    bullet_font = _font(28)
    draw.text((70, 90), f"Слайд {slide.index:02d}", font=meta_font, fill="#7b8794")
    y = _draw_wrapped_text(
        draw,
        (70, 150),
        slide.title,
        title_font,
        760,
        fill=accent,
        line_gap=8,
        max_lines=3,
    )
    _draw_wrapped_text(
        draw,
        (70, y + 8),
        slide.subtitle,
        subtitle_font,
        760,
        fill="#334e68",
        line_gap=7,
        max_lines=3,
    )
    y = 950
    for bullet in slide.bullets[:5]:
        draw.ellipse((78, y - 18, 94, y - 2), fill=accent)
        draw.text((110, y - 28), str(bullet), font=bullet_font, fill="#102a43")
        y += 42


def _draw_template_text(
    draw: ImageDraw.ImageDraw,
    slide: SlideSpec,
    *,
    accent: str,
    title_position: str,
) -> None:
    if title_position == "left":
        title_font = _font(46, bold=True)
        subtitle_font = _font(26)
        meta_font = _font(22)
        draw.text((64, 88), f"Слайд {slide.index:02d}", font=meta_font, fill="#7b8794")
        y = _draw_wrapped_text(
            draw,
            (64, 145),
            slide.title,
            title_font,
            320,
            fill=accent,
            line_gap=8,
            max_lines=4,
        )
        _draw_wrapped_text(
            draw,
            (64, y + 12),
            slide.subtitle,
            subtitle_font,
            320,
            fill="#334e68",
            line_gap=6,
            max_lines=4,
        )
        return
    if title_position == "bottom":
        title_font = _font(46, bold=True)
        subtitle_font = _font(28)
        y = _draw_wrapped_text(
            draw,
            (70, 910),
            slide.title,
            title_font,
            760,
            fill=accent,
            line_gap=8,
            max_lines=2,
        )
        _draw_wrapped_text(
            draw,
            (70, y + 10),
            slide.subtitle,
            subtitle_font,
            760,
            fill="#334e68",
            line_gap=7,
            max_lines=2,
        )
        return
    _draw_slide_text(draw, slide, accent=accent)


def _draw_slide_frame(draw: ImageDraw.ImageDraw, width: int, height: int) -> None:
    draw.rounded_rectangle((36, 36, width - 36, height - 36), radius=22, outline="#d8e2ef", width=3)


def _draw_template_frame(draw: ImageDraw.ImageDraw, width: int, height: int, *, style: str, accent: str) -> None:
    if style == "none":
        return
    if style == "accent":
        draw.rounded_rectangle((34, 34, width - 34, height - 34), radius=24, outline=accent, width=5)
        draw.rounded_rectangle((54, 54, width - 54, height - 54), radius=18, outline="#d8e2ef", width=2)
        return
    _draw_slide_frame(draw, width, height)


def render_slide_image(
    slide: SlideSpec,
    *,
    source_image_href: str | None = None,
    background_image_href: str | None = None,
    category_template: CategoryTemplate | None = None,
    width: int | None = None,
    height: int | None = None,
) -> bytes:
    width = width or slide.width
    height = height or slide.height
    canvas = Image.new("RGBA", (width, height), "#ffffff")
    draw = ImageDraw.Draw(canvas)
    accent = category_template.accent_color if category_template else ("#2f6fed" if slide.role == "hero" else "#243b53")
    frame_style = category_template.frame_style if category_template else "thin"
    title_position = category_template.title_position if category_template else "top"
    expand_square = bool(category_template and category_template.photo_treatment == "expand_square")
    source = _image_from_data_uri(source_image_href)
    background = _image_from_data_uri(background_image_href)

    if slide.image_cleared:
        source = None
        background = None

    if slide.role == "white_background":
        draw.rectangle((0, 0, width, height), fill="#ffffff")
        product = source or background
        if product and slide.index == 10:
            title_font = _font(48, bold=True)
            bullet_font = _font(26)
            draw.text((70, 72), "Комплект поставки", font=title_font, fill="#1e293b")
            draw.rectangle((70, 150, width - 70, 152), fill="#dbeafe")
            _paste_contained(canvas, product, (70, 190, 450, 690))
            draw.rounded_rectangle((480, 190, 830, 690), radius=28, fill="#f8fafc", outline="#e2e8f0", width=2)
            bullets = [item for item in slide.bullets if item.strip()][:4] or [
                "Основной товар",
                "Аксессуары",
                "Инструкция",
                "Упаковка",
            ]
            y = 250
            for idx, bullet in enumerate(bullets, start=1):
                draw.ellipse((506, y - 24, 534, y + 4), fill="#2563eb")
                draw.text((516, y - 23), str(idx), font=_font(16, bold=True), fill="#ffffff")
                draw.text((550, y - 28), bullet, font=bullet_font, fill="#0f172a")
                y += 88
        elif product and expand_square and _is_squareish(product):
            canvas = _adapt_square_product_to_vertical(product, width, height)
            draw = ImageDraw.Draw(canvas)
        elif product:
            _paste_contained(canvas, product, (80, 120, width - 80, height - 120))
        else:
            _draw_template_text(draw, slide, accent=accent, title_position=title_position)
            _draw_missing_photo(draw, width, 520, "Загрузите фото товара")
        _draw_template_frame(draw, width, height, style=frame_style, accent=accent)
    else:
        if background:
            _paste_covered(canvas, background, (0, 0, width, height))
            overlay = Image.new("RGBA", (width, height), (255, 255, 255, 0))
            overlay_draw = ImageDraw.Draw(overlay)
            overlay_draw.rectangle((0, 0, width, 520), fill=(255, 255, 255, 220))
            overlay_draw.rectangle((0, 820, width, height), fill=(255, 255, 255, 210))
            canvas.alpha_composite(overlay)
            draw = ImageDraw.Draw(canvas)
            _draw_template_text(draw, slide, accent=accent, title_position=title_position)
            _draw_template_frame(draw, width, height, style=frame_style, accent=accent)
        elif source:
            draw.rectangle((0, 0, width, height), fill="#f4f7fb")
            _draw_template_frame(draw, width, height, style=frame_style, accent=accent)
            _draw_template_text(draw, slide, accent=accent, title_position=title_position)
            if expand_square and _is_squareish(source):
                product_card = _adapt_square_product_to_vertical(source, 520, 560)
                _paste_contained(canvas, product_card, (330, 520, 800, 920))
            else:
                draw.rounded_rectangle((150, 540, 750, 900), radius=34, fill="#ffffff", outline="#bcccdc", width=3)
                _paste_contained(canvas, source, (170, 560, 730, 880))
        else:
            draw.rectangle((0, 0, width, height), fill="#f4f7fb")
            _draw_template_frame(draw, width, height, style=frame_style, accent=accent)
            _draw_template_text(draw, slide, accent=accent, title_position=title_position)
            _draw_missing_photo(draw, width, 540, "Загрузите фото товара")

    out = io.BytesIO()
    canvas.convert("RGB").save(out, format="PNG", optimize=True)
    return out.getvalue()


def _draw_missing_photo(draw: ImageDraw.ImageDraw, width: int, y: int, title: str) -> None:
    title_font = _font(30, bold=True)
    text_font = _font(22)
    draw.rounded_rectangle((150, y, width - 150, y + 360), radius=34, fill="#ffffff", outline="#bcccdc", width=3)
    draw.text((width // 2 - 165, y + 150), title, font=title_font, fill="#102a43")
    draw.text((width // 2 - 145, y + 195), "Пример с разных ракурсов", font=text_font, fill="#627d98")


def render_rich_block_image(
    block: RichBlock,
    product: ProductContext,
    *,
    source_image_href: str | None = None,
    width: int = RICH_IMAGE_WIDTH,
    height: int = RICH_IMAGE_HEIGHT,
) -> bytes:
    canvas = Image.new("RGBA", (width, height), "#f8fafc")
    draw = ImageDraw.Draw(canvas)
    title_font = _font(62, bold=True)
    text_font = _font(34)
    meta_font = _font(24)
    small_font = _font(22)
    accent = "#2563eb"
    ink = "#0f172a"
    muted = "#64748b"

    draw.rounded_rectangle(
        (36, 36, width - 36, height - 36),
        radius=34,
        fill="#ffffff",
        outline="#dbeafe",
        width=3,
    )
    draw.rounded_rectangle((880, 100, 1340, 790), radius=36, fill="#f1f5f9", outline="#e2e8f0", width=2)
    draw.text((86, 94), f"Rich блок {block.index:02d}", font=meta_font, fill=accent)
    draw.text((86, height - 92), product.title[:96], font=small_font, fill=muted)

    y = 170
    for line in _wrap_for_width(draw, block.title or f"Блок {block.index}", title_font, 710)[:3]:
        draw.text((86, y), line, font=title_font, fill=ink)
        y += 72

    y += 18
    for line in _wrap_for_width(draw, block.text, text_font, 710)[:9]:
        draw.text((90, y), line, font=text_font, fill="#334155")
        y += 48

    product_image = _image_from_data_uri(source_image_href)
    if product_image:
        _paste_contained(canvas, product_image, (925, 155, 1295, 705))
    else:
        draw.rounded_rectangle((965, 270, 1255, 590), radius=28, fill="#ffffff", outline="#cbd5e1", width=3)
        draw.text((1024, 404), "Фото товара", font=text_font, fill=muted)
        draw.text((1012, 456), "1440 x 900", font=meta_font, fill=muted)

    out = io.BytesIO()
    canvas.convert("RGB").save(out, format="PNG", optimize=True)
    return out.getvalue()


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


def _render_kit_contents_infographic(slide: SlideSpec, source_href: str) -> bytes:
    w, h = slide.width, slide.height
    bullets = [item for item in slide.bullets if item.strip()][:4]
    if not bullets:
        bullets = ["Основной товар", "Аксессуары", "Инструкция", "Упаковка"]
    parts = [
        _svg_open(w, h),
        f'<rect width="{w}" height="{h}" fill="#ffffff"/>',
        f'<rect x="36" y="36" width="{w-72}" height="{h-72}" rx="22" fill="none" stroke="#d8e2ef" stroke-width="3"/>',
        '<text x="70" y="108" font-family="Arial, DejaVu Sans, sans-serif" font-size="48" '
        'font-weight="700" fill="#1e293b">Комплект поставки</text>',
        f'<rect x="70" y="150" width="{w-140}" height="2" fill="#dbeafe"/>',
        _image_tag(source_href, 70, 190, 380, 500, fit="meet"),
        '<rect x="480" y="190" width="350" height="500" rx="28" fill="#f8fafc" stroke="#e2e8f0" stroke-width="2"/>',
    ]
    y = 250
    for idx, bullet in enumerate(bullets, start=1):
        parts.append(
            f'<circle cx="520" cy="{y-10}" r="14" fill="#2563eb"/>'
            f'<text x="520" y="{y-4}" text-anchor="middle" font-family="Arial, DejaVu Sans, sans-serif" '
            f'font-size="16" font-weight="700" fill="#ffffff">{idx}</text>'
        )
        parts.append(
            f'<text x="550" y="{y}" font-family="Arial, DejaVu Sans, sans-serif" '
            f'font-size="26" fill="#0f172a">{html.escape(bullet)}</text>'
        )
        y += 88
    parts.append("</svg>")
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


def _render_missing_photo_state(slide: SlideSpec) -> bytes:
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
            f'<rect x="150" y="{product_y}" width="600" height="360" rx="34" fill="#ffffff" stroke="#bcccdc" stroke-width="3" stroke-dasharray="12 10"/>',
            f'<text x="450" y="{product_y + 165}" text-anchor="middle" font-family="Arial, DejaVu Sans, sans-serif" font-size="30" font-weight="700" fill="#102a43">Загрузите фото товара</text>',
            f'<text x="450" y="{product_y + 210}" text-anchor="middle" font-family="Arial, DejaVu Sans, sans-serif" font-size="22" fill="#627d98">Пример с разных ракурсов</text>',
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
    if slide.image_cleared:
        return _render_missing_photo_state(slide)

    source_image_href = source_image_href or source_image_data_uri
    background_image_href = background_image_href or background_image_data_uri

    if slide.role == "white_background":
        product_href = source_image_href or background_image_href
        if product_href:
            if slide.index == 10:
                return _render_kit_contents_infographic(slide, product_href)
            return _render_white_background(slide, product_href)
        return _render_missing_photo_state(slide)

    if background_image_href:
        return _render_full_background(slide, background_image_href)
    if source_image_href:
        return _render_product_composite(slide, source_image_href)
    return _render_missing_photo_state(slide)


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
        visual = ""
        if block.asset_id:
            visual = (
                "<figure class='rich-block__visual'>"
                f"<img src='/v1/assets/{block.asset_id}' alt='Rich block {block.index}' loading='lazy'/>"
                "</figure>"
            )
        blocks.append(
            "<section class='rich-block'>"
            f"{visual}"
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
    .rich-block {{ border: 1px solid #d9e2ec; border-radius: 8px; padding: 18px; margin: 14px 0; background:#fff; }}
    .rich-block__visual {{ margin: 0 0 12px; border:1px solid #e2e8f0; border-radius: 8px; overflow: hidden; }}
    .rich-block__visual img {{ display:block; width:100%; height:auto; background:#fff; }}
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


def _make_portable_rich_html(html_bytes: bytes, result: GenerationResult) -> bytes:
    """Rewrite server-relative /v1/assets/{id} URLs to ./block_NN.png relative paths for ZIP portability."""
    text = html_bytes.decode("utf-8")
    for block in result.rich.blocks:
        if block.asset_id:
            text = text.replace(
                f"/v1/assets/{block.asset_id}",
                f"./block_{block.index:02d}.{RICH_IMAGE_EXTENSION}",
            )
    return text.encode("utf-8")


def build_rich_zip(result: GenerationResult, asset_blobs: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        if result.rich.pdf_asset_id and result.rich.pdf_asset_id in asset_blobs:
            zf.writestr("rich/preview.pdf", asset_blobs[result.rich.pdf_asset_id])
        if result.rich.cover_asset_id and result.rich.cover_asset_id in asset_blobs:
            zf.writestr("rich/cover.svg", asset_blobs[result.rich.cover_asset_id])
        for block in result.rich.blocks:
            if block.asset_id and block.asset_id in asset_blobs:
                zf.writestr(
                    f"rich/block_{block.index:02d}.{RICH_IMAGE_EXTENSION}",
                    asset_blobs[block.asset_id],
                )
    return buf.getvalue()


def build_export_zip(
    result: GenerationResult,
    asset_blobs: dict[str, bytes],
    *,
    quality_mode: QualityMode = "preview",
) -> bytes:
    buf = io.BytesIO()
    manifest = result.model_dump(mode="json")
    manifest.pop("export_asset_id", None)
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        zf.writestr(
            "export_profile.json",
            json.dumps(
                {
                    "quality_mode": quality_mode,
                    "target": "wildberries",
                    "note": (
                        "preview mode before approve"
                        if quality_mode == "preview"
                        else "finalized export profile after approve"
                    ),
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
        for slide in result.slides:
            if slide.asset_id and slide.asset_id in asset_blobs:
                zf.writestr(
                    f"slides/slide_{slide.index:02d}.{SLIDE_IMAGE_EXTENSION}",
                    asset_blobs[slide.asset_id],
                )
                if quality_mode == "final":
                    zf.writestr(
                        f"slides_hq/slide_{slide.index:02d}.{SLIDE_IMAGE_EXTENSION}",
                        asset_blobs[slide.asset_id],
                    )
        for block in result.rich.blocks:
            if block.asset_id and block.asset_id in asset_blobs:
                zf.writestr(
                    f"rich/block_{block.index:02d}.{RICH_IMAGE_EXTENSION}",
                    asset_blobs[block.asset_id],
                )
        if result.rich.cover_asset_id and result.rich.cover_asset_id in asset_blobs:
            zf.writestr("rich/cover.svg", asset_blobs[result.rich.cover_asset_id])
        if result.rich.pdf_asset_id and result.rich.pdf_asset_id in asset_blobs:
            zf.writestr("rich/preview.pdf", asset_blobs[result.rich.pdf_asset_id])
    return buf.getvalue()
