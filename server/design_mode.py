#!/usr/bin/env python3
"""
Design Mode Module for Taktstock Orchestrator
---------------------------------------------
Enables visual capture and DOM/CSS context extraction for frontend frameworks (Astro, Appsmith, React, etc.):
- Full-page or element-specific screenshot capture (PNG)
- Extracts HTML / DOM structure and computed CSS styles of target elements
- Injects visual context and component documentation into prompts for multimodal models (Gemini/agy, Claude, GPT-5)
- Supports Playwright (Chromium headless) with graceful fallback via urllib/HTML parser if Playwright is not installed
"""

import os
import json
import logging
from pathlib import Path
from typing import Dict, Any, List, Optional
from datetime import datetime
import urllib.request
import urllib.parse
import urllib.error

from url_validator import validate_safe_url, SafeRedirectHandler

logger = logging.getLogger("TaktstockDesignMode")

class DesignCapture:
    def __init__(self, output_dir: Optional[Path] = None):
        default_home = os.environ.get("TAKTSTOCK_HOME") or os.environ.get("UFFICIO_HOME")
        base_dir = Path(default_home) if default_home else (Path.home() / "taktstock")
        self.output_dir = Path(output_dir) if output_dir else (base_dir / "design_captures")
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def capture_url(
        self,
        url: str,
        selector: Optional[str] = None,
        viewport_width: int = 1280,
        viewport_height: int = 800,
        wait_seconds: float = 2.0
    ) -> Dict[str, Any]:
        """
        Captures screenshot and DOM/CSS metadata from a URL.
        Strictly validates URL against SSRF attacks prior to any network request.
        Uses Playwright if available, otherwise falls back to urllib.
        """
        # Preventive SSRF validation
        is_safe, ssrf_err = validate_safe_url(url)
        if not is_safe:
            logger.warning(f"Design Capture blocked for security reasons (SSRF): {url} - {ssrf_err}")
            return {
                "url": url,
                "title": "Access Blocked (SSRF Protection)",
                "selector": selector,
                "screenshot_path": None,
                "dom_snippet": "",
                "computed_styles": {},
                "driver": "blocked",
                "error": f"URL blocked for security reasons (SSRF): {ssrf_err}",
                "success": False
            }

        timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
        clean_url_name = url.replace("https://", "").replace("http://", "").replace("/", "_").replace(":", "_")
        screenshot_path = self.output_dir / f"screen_{timestamp_str}_{clean_url_name[:30]}.png"

        # Prova ad utilizzare Playwright
        try:
            from playwright.sync_api import sync_playwright
            logger.info(f"Design Mode (Playwright): Navigazione su {url}...")
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page(viewport={"width": viewport_width, "height": viewport_height})

                # Protezione SSRF a livello di routing / redirect per tutte le richieste
                def handle_route(route):
                    req_url = route.request.url
                    safe, _ = validate_safe_url(req_url)
                    if not safe:
                        logger.warning(f"Playwright route bloccata per motivi SSRF: {req_url}")
                        route.abort()
                    else:
                        route.continue_()

                page.route("**/*", handle_route)
                page.goto(url, wait_until="networkidle", timeout=30000)
                page.wait_for_timeout(int(wait_seconds * 1000))

                dom_snippet = ""
                computed_styles = {}

                if selector:
                    element = page.query_selector(selector)
                    if element:
                        element.screenshot(path=str(screenshot_path))
                        dom_snippet = element.inner_html()[:2000]
                        computed_styles = element.evaluate("""
                            el => {
                                const cs = window.getComputedStyle(el);
                                return {
                                    display: cs.display,
                                    position: cs.position,
                                    width: cs.width,
                                    height: cs.height,
                                    margin: cs.margin,
                                    padding: cs.padding,
                                    color: cs.color,
                                    backgroundColor: cs.backgroundColor,
                                    fontSize: cs.fontSize,
                                    fontFamily: cs.fontFamily,
                                    flexDirection: cs.flexDirection,
                                    justifyContent: cs.justifyContent,
                                    alignItems: cs.alignItems
                                };
                            }
                        """)
                    else:
                        page.screenshot(path=str(screenshot_path), full_page=True)
                        dom_snippet = page.content()[:3000]
                else:
                    page.screenshot(path=str(screenshot_path), full_page=True)
                    dom_snippet = page.content()[:3000]

                title = page.title()
                browser.close()

                logger.info(f"Screenshot salvato in {screenshot_path}")
                return {
                    "url": url,
                    "title": title,
                    "selector": selector,
                    "screenshot_path": str(screenshot_path),
                    "dom_snippet": dom_snippet,
                    "computed_styles": computed_styles,
                    "driver": "playwright",
                    "success": True
                }

        except ImportError:
            logger.warning(
                "Playwright non installato. Per screenshot reali esegui: pip install playwright && playwright install chromium"
            )
            # Fallback HTTP standard (recupera solo HTML testuale)
            return self._fallback_fetch_html(url, selector)
        except Exception as e:
            logger.error(f"Error during Playwright capture: {e}")
            return self._fallback_fetch_html(url, selector, error_msg=str(e))

    def _fallback_fetch_html(self, url: str, selector: Optional[str] = None, error_msg: Optional[str] = None) -> Dict[str, Any]:
        """Lightweight fallback when Playwright or browser is unavailable."""
        is_safe, ssrf_err = validate_safe_url(url)
        if not is_safe:
            return {
                "url": url,
                "title": "Access Blocked (SSRF Protection)",
                "selector": selector,
                "screenshot_path": None,
                "dom_snippet": "",
                "computed_styles": {},
                "driver": "blocked",
                "error": f"URL blocked for security reasons (SSRF): {ssrf_err}",
                "success": False
            }

        dom_snippet = ""
        fetch_err = None
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Taktstock-DesignMode/1.0"})
            opener = urllib.request.build_opener(SafeRedirectHandler())
            with opener.open(req, timeout=10) as resp:
                html_bytes = resp.read()
                dom_snippet = html_bytes.decode("utf-8", errors="ignore")[:3000]
        except Exception as e:
            fetch_err = str(e)
            dom_snippet = f"Unable to read HTML via HTTP: {e}"

        success = bool(dom_snippet and not dom_snippet.startswith("Unable to read"))
        res = {
            "url": url,
            "title": f"Captured from {url}",
            "selector": selector,
            "screenshot_path": None,
            "dom_snippet": dom_snippet,
            "computed_styles": {},
            "driver": "urllib_fallback",
            "warning": error_msg or ("Playwright not active (simulated screenshot)" if not fetch_err else None),
            "success": success
        }
        if fetch_err:
            res["error"] = fetch_err
        return res

    @staticmethod
    def format_prompt_context(capture_data: Dict[str, Any]) -> str:
        """Formats extracted visual data for inclusion in agent prompts."""
        if not capture_data or not capture_data.get("success"):
            return ""

        styles_str = ""
        if capture_data.get("computed_styles"):
            styles_str = "\n".join([f"  - {k}: {v}" for k, v in capture_data["computed_styles"].items()])

        res = [
            "\n════════════════════════════════════════════════════════════",
            "🎨 VISUAL CONTEXT & DESIGN MODE (Frontend)",
            f"Target URL: {capture_data.get('url')}",
            f"Page Title: {capture_data.get('title')}",
        ]
        if capture_data.get("selector"):
            res.append(f"Target Element Selector: `{capture_data.get('selector')}`")
        if capture_data.get("screenshot_path"):
            res.append(f"Screenshot Path: {capture_data.get('screenshot_path')}")
        if styles_str:
            res.append(f"Computed CSS Styles:\n{styles_str}")
        if capture_data.get("dom_snippet"):
            res.append(f"DOM/HTML Snippet:\n```html\n{capture_data.get('dom_snippet')[:1500]}\n```")
        res.append("════════════════════════════════════════════════════════════\n")
        return "\n".join(res)
