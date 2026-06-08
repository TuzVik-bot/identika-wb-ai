from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CSS = ROOT / "app" / "identika" / "static" / "app.css"
TEMPLATES = ROOT / "app" / "identika" / "templates"


def _css_block(css: str, query: str) -> str:
    marker = f"@media ({query})"
    start = css.find(marker)
    assert start >= 0, f"missing media query: {marker}"
    depth = 0
    end = start
    for index, char in enumerate(css[start:], start=start):
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                end = index + 1
                break
    assert end > start, f"malformed media query: {marker}"
    return css[start:end]


def test_desktop_and_mobile_layout_contracts_are_kept() -> None:
    css = CSS.read_text(encoding="utf-8")

    desktop_contracts = [
        ".dashboard-grid",
        ".create-workspace",
        ".job-layout--wide",
        ".job-tabs",
        ".rich-visual-grid",
        ".templates-grid",
        ".template-card",
        ".template-copy-btn",
    ]
    for selector in desktop_contracts:
        assert selector in css

    tablet = _css_block(css, "max-width: 900px")
    phone = _css_block(css, "max-width: 640px")
    compact = _css_block(css, "max-width: 520px")

    assert ".templates-grid" in tablet
    assert "grid-template-columns: 1fr" in tablet
    assert ".template-card" in phone
    assert ".template-card__editor form:first-of-type" in phone
    assert ".template-copy-btn" in phone
    assert ".job-tabs .tab-btn" in compact


def test_key_pages_keep_visual_regression_markers() -> None:
    base = (TEMPLATES / "base.html").read_text(encoding="utf-8")
    create = (TEMPLATES / "create.html").read_text(encoding="utf-8")
    job = (TEMPLATES / "job.html").read_text(encoding="utf-8")
    templates = (TEMPLATES / "templates.html").read_text(encoding="utf-8")

    assert "topbar__burger" in base
    assert "topbar-open" in base
    assert "source-upload-form" in create
    assert "category-template-select" in create
    assert "category-template-id-field" in create
    assert "job-layout--wide" in job
    assert "data-tab=\"slides\"" in job
    assert "data-tab=\"rich\"" in job
    assert "data-tab=\"export\"" in job
    assert "rich-iframe-preview" in job
    assert "template-copy-btn" in templates
    assert "template-card__editor" in templates
    assert "/templates/{{ template.id }}/delete" in templates
