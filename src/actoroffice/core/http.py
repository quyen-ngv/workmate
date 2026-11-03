"""Shared HTTP helpers."""

from __future__ import annotations

import functools
import ssl

import httpx


@functools.cache
def ssl_context() -> ssl.SSLContext:
    """httpx's default TLS context (CERT_REQUIRED, hostname check, certifi or SSL_CERT_FILE/DIR roots), built once
    per process: loading the CA bundle costs ~0.3-0.6s, and each client without a transport would load it again."""
    return httpx.create_ssl_context()
