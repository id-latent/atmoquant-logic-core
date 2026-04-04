# ==============================================================================
# utils/headers.py = Random User-Agent Headers
# ==============================================================================
"""
AQL Headers Utility
Rotate user-agent headers untuk setiap request
agar tidak terdeteksi sebagai automated bot.
"""
from __future__ import annotations

import random

# Pool user-agent dari browser populer
_USER_AGENTS = [
    # Chrome Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36",

    # Chrome Mac
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/123.0.0.0 Safari/537.36",

    # Chrome Linux
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36",

    # Firefox Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) "
    "Gecko/20100101 Firefox/124.0",

    # Safari Mac
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.4 Safari/605.1.15",

    # Chrome Android
    "Mozilla/5.0 (Linux; Android 13; Pixel 7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Mobile Safari/537.36",
]


def random_headers(include_content_type: bool = False) -> dict:
    """
    Generate random browser-like headers.

    Args:
        include_content_type: Set True untuk POST requests (JSON body).

    Returns:
        Dict headers siap dipakai di httpx request.
    """
    headers = {
        "User-Agent":      random.choice(_USER_AGENTS),
        "Accept":          "application/json",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection":      "keep-alive",
        "Cache-Control":   "no-cache",
    }

    if include_content_type:
        headers["Content-Type"] = "application/json"

    return headers


def random_get_headers() -> dict:
    """Headers untuk GET request."""
    return random_headers(include_content_type=False)


def random_post_headers() -> dict:
    """Headers untuk POST request (dengan Content-Type)."""
    return random_headers(include_content_type=True)
