#!/usr/bin/env python3
"""
PDF rendering.

Primary: headless Chromium via Playwright, so the PDF is pixel-identical to
what the browser shows - SVG donut gauges, flex progress bars, gradients and
all. WeasyPrint cannot render SVG stroke-dasharray arcs or flex bars in paged
media, which left the score donut and category bars missing from PDFs.

Fallback: WeasyPrint, used automatically if Chromium is unavailable or fails,
so PDF generation degrades rather than breaking the audit.

print_background=True is essential - without it Chromium drops every
background colour, which would flatten the whole report.
"""

import os

_BROWSER_OK = None  # cached probe result


def browser_available():
    global _BROWSER_OK
    if _BROWSER_OK is None:
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                b = p.chromium.launch(args=["--no-sandbox",
                                            "--disable-dev-shm-usage"])
                b.close()
            _BROWSER_OK = True
        except Exception as e:
            print(f"  (chromium unavailable: {str(e)[:120]} - using WeasyPrint)")
            _BROWSER_OK = False
    return _BROWSER_OK


def _render_chromium(html_path, pdf_path):
    from playwright.sync_api import sync_playwright
    url = "file://" + os.path.abspath(html_path)
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox",
                                          "--disable-dev-shm-usage"])
        try:
            page = browser.new_page()
            page.goto(url, wait_until="networkidle", timeout=60000)
            page.emulate_media(media="print")
            page.pdf(path=pdf_path, format="A4", print_background=True,
                     margin={"top": "12mm", "bottom": "14mm",
                             "left": "10mm", "right": "10mm"},
                     prefer_css_page_size=False)
        finally:
            browser.close()


def _render_weasyprint(html_path, pdf_path):
    from weasyprint import HTML
    HTML(filename=html_path).write_pdf(pdf_path)


def html_to_pdf(html_path, pdf_path):
    """Render one HTML file to PDF. Returns the engine used, or None on failure."""
    if browser_available():
        try:
            _render_chromium(html_path, pdf_path)
            return "chromium"
        except Exception as e:
            print(f"  (chromium render failed: {str(e)[:150]} - falling back)")
    try:
        _render_weasyprint(html_path, pdf_path)
        return "weasyprint"
    except Exception as e:
        print(f"  PDF generation failed: {str(e)[:200]}")
        return None
