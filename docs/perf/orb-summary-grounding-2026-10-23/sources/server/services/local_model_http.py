"""Reuse verified trust material without sharing per-request client state.

HTTPX constructs an SSL context even for plain loopback HTTP. Re-reading the
CA bundle for every concurrent model call blocks the event loop. An immutable
application-owned context preserves the existing certifi verification policy;
clients, cookies, headers, requests and stream cleanup remain request-local.
"""

from __future__ import annotations

import ssl
import threading

import certifi

_context: ssl.SSLContext | None = None
_context_lock = threading.Lock()


def local_model_ssl_context() -> ssl.SSLContext:
    """Same trust roots as HTTPX verify=True, trust_env=False; never mutate it."""
    global _context
    if _context is None:
        with _context_lock:
            if _context is None:
                _context = ssl.create_default_context(cafile=certifi.where())
    return _context
