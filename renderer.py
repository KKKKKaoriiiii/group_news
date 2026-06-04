"""群新闻渲染器：Markdown 新闻文本 → HTML → 报纸风格图片。

流水线：
1. 解析 LLM 输出的结构化 Markdown 文本
2. 注入报纸风格 HTML 模板
3. Playwright Chromium 无头渲染
4. 整页截图，按 A4 比例分割为多张 PNG 图片
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from pathlib import Path
from string import Template

# A4 宽度参考值（210mm，150 DPI 下的像素宽度）
_A4_WIDTH_PX = 1240


_TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"


@dataclass
class NewsBlock:
    """新闻文本解析后的结构化块。"""

    type: str  # "h1", "h2", "para", "divider", "quote"
    text: str = ""


def _load_template(name: str) -> Template:
    """加载 HTML 模板文件。"""
    path = _TEMPLATE_DIR / name
    return Template(path.read_text(encoding="utf-8"))


def parse_news_text(text: str) -> list[NewsBlock]:
    """将 LLM 输出的 Markdown 新闻文本解析为结构化块列表。

    支持的格式：
        # 主标题（仅一个，放在最前面）
        ## 板块标题
        --- 分隔线
        > 引用/点评
        空行分隔的正文段落

    Args:
        text: LLM 生成的原始新闻文本。

    Returns:
        解析后的 NewsBlock 列表。
    """
    blocks: list[NewsBlock] = []
    lines = text.strip().split("\n")
    current_para: list[str] = []

    def flush_para() -> None:
        if current_para:
            blocks.append(NewsBlock("para", "\n".join(current_para)))
            current_para.clear()

    for line in lines:
        stripped = line.strip()
        if not stripped:
            flush_para()
            continue

        if stripped.startswith("# ") and not stripped.startswith("## "):
            flush_para()
            blocks.append(NewsBlock("h1", stripped[2:].strip()))
        elif stripped.startswith("## "):
            flush_para()
            blocks.append(NewsBlock("h2", stripped[3:].strip()))
        elif stripped in ("---", "--- ", "***", "*** "):
            flush_para()
            blocks.append(NewsBlock("divider"))
        elif stripped.startswith("> "):
            flush_para()
            blocks.append(NewsBlock("quote", stripped[2:].strip()))
        else:
            current_para.append(stripped)

    flush_para()
    return blocks


def _block_to_html(block: NewsBlock) -> str:
    """将单个新闻块转为 HTML 片段。"""
    esc = _escape_html

    if block.type == "h1":
        return f'<h1 class="main-title">{esc(block.text)}</h1>'
    if block.type == "h2":
        return (
            '<div class="section-title-bar">'
            '<span class="section-icon">&#9632;</span>'
            f'<h2>{esc(block.text)}</h2>'
            "</div>"
        )
    if block.type == "divider":
        return '<div class="fancy-divider"><span>&#9830;</span></div>'
    if block.type == "quote":
        return (
            '<blockquote class="editor-note">'
            '<span class="quote-mark">&ldquo;</span>'
            f'<p>{esc(block.text)}</p>'
            "</blockquote>"
        )
    if block.type == "para":
        return f"<p>{esc(block.text)}</p>"

    return ""


def _escape_html(text: str) -> str:
    """转义 HTML 特殊字符。"""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def build_news_html(
    blocks: list[NewsBlock],
    *,
    date_str: str = "",
    page_num: int = 1,
    total_pages: int = 1,
) -> str:
    """将结构化新闻块渲染为完整 HTML 页面。

    Args:
        blocks: 解析后的新闻块列表。
        date_str: 日期字符串。
        page_num: 当前页码。
        total_pages: 总页数。

    Returns:
        完整的 HTML 字符串。
    """
    content_html = "\n".join(_block_to_html(b) for b in blocks)

    title_text = "群 新 闻"
    if date_str:
        title_text = f"群 新 闻 · {_escape_html(date_str)}"

    page_info = f"纯属虚构 · 娱乐整蛊 · 第 {page_num}/{total_pages} 页"

    template = _load_template("news.html")
    return template.safe_substitute(
        title=title_text,
        date=date_str,
        content=content_html,
        page_info=page_info,
    )


def html_to_images(html_str: str, *, dpi: int = 150) -> list[bytes]:
    """将 HTML 字符串渲染为 PNG 长图。

    使用 Playwright 无头 Chromium 渲染全页截图，直接输出为单张长图。

    Args:
        html_str: 完整 HTML 字符串。
        dpi: 输出图片 DPI。

    Returns:
        包含单张 PNG 图片 bytes 的列表。

    Raises:
        ImportError: 缺少 playwright 依赖。
        RuntimeError: 渲染过程失败。
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise ImportError(
            "缺少 playwright 依赖，请执行: uv add playwright && uv run playwright install chromium"
        ) from None

    scale_factor = dpi / 96.0
    viewport_width = int(_A4_WIDTH_PX / scale_factor)

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            context = browser.new_context(
                viewport={"width": viewport_width, "height": 800},
                device_scale_factor=scale_factor,
            )
            page = context.new_page()
            page.set_content(html_str, wait_until="networkidle")

            content_height = page.evaluate("() => document.body.scrollHeight")
            page.set_viewport_size({"width": viewport_width, "height": content_height})

            screenshot_bytes = page.screenshot(full_page=True)
            browser.close()
    except Exception as exc:
        raise RuntimeError(f"Playwright 渲染失败: {exc}") from exc

    return [screenshot_bytes]


def render_news_to_images(
    text: str,
    *,
    date_str: str = "",
    dpi: int = 150,
) -> list[bytes]:
    """端到端：Markdown 新闻文本 → PNG 图片列表。

    Args:
        text: LLM 生成的原始新闻文本。
        date_str: 日期字符串。
        dpi: 输出图片 DPI。

    Returns:
        每页 PNG 图片的 bytes 列表。
    """
    blocks = parse_news_text(text)
    if not blocks:
        return []

    total = max(1, len(blocks) // 6 + 1)
    html = build_news_html(blocks, date_str=date_str, page_num=1, total_pages=total)
    return html_to_images(html, dpi=dpi)


def images_to_base64_list(images: list[bytes]) -> list[str]:
    """将 PNG bytes 列表转为 base64 字符串列表（供 send_image 使用）。"""
    return [base64.b64encode(img).decode("utf-8") for img in images]
