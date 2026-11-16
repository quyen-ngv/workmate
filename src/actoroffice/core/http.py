"""Shared HTTP helpers: one TLS trust for everything the office calls.

On a network that inspects TLS (a company proxy that re-signs every connection with its own CA) the roots shipped with
Python are not enough. The office trusts, in this order: the CA bundle named in the settings (`ca_bundle`), else the
operating system's certificate store (through `truststore`), else the roots shipped with Python. Verification is
never switched off.
"""

from __future__ import annotations

import functools
import logging
import os
import ssl
import sys
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

_ca_bundle: str | None = None


def configure(ca_bundle: str | None) -> None:
    """Use this CA bundle (a PEM file) for every connection the office makes. None: the operating system's store."""
    global _ca_bundle
    wanted = str(Path(ca_bundle).expanduser()) if ca_bundle else None
    if wanted != _ca_bundle:
        _ca_bundle = wanted
        ssl_context.cache_clear()


def verify(transport: object = None) -> ssl.SSLContext | bool:
    """The `verify=` of an httpx client: the office's context, or nothing to load when a test transport is given."""
    return True if transport is not None else ssl_context()


def ca_bundle() -> str | None:
    return _ca_bundle


@functools.cache
def ssl_context() -> ssl.SSLContext:
    """The TLS context of the office (CERT_REQUIRED, hostname check), built once per process: loading the roots costs
    ~0.3-0.6s, and each client without a transport would load them again."""
    if _ca_bundle:
        return ssl.create_default_context(cafile=_ca_bundle)
    try:
        import truststore

        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except Exception:  # not installed, or a platform it does not know: the roots shipped with Python
        log.info("truststore is not usable here, using the default roots")
        return httpx.create_ssl_context()


def git_tls_config() -> list[tuple[str, str]]:
    """git settings that make it trust what the office trusts: the configured bundle, else on Windows the system
    store (schannel) instead of the bundle shipped with Git."""
    if _ca_bundle:
        return [("http.sslCAInfo", _ca_bundle)]
    if sys.platform == "win32":
        return [("http.sslBackend", "schannel")]
    return []


def node_ca_file(data_dir: Path) -> str | None:
    """A PEM file for `NODE_EXTRA_CA_CERTS`, so the AI's own CLI (Kiro, Claude: Node programs, which do not read the
    operating system's store) trusts the company's CA too. The configured bundle, else on Windows the system's root
    and intermediate stores written to <data_dir>/ca-system.pem. None when the caller already set the variable or
    there is nothing to add."""
    if os.environ.get("NODE_EXTRA_CA_CERTS"):
        return None
    if _ca_bundle:
        return _ca_bundle
    if sys.platform != "win32":
        return None
    return _windows_store_pem(str(data_dir))


@functools.cache
def _windows_store_pem(data_dir: str) -> str | None:
    pems: list[str] = []
    for store in ("ROOT", "CA"):
        try:
            entries = ssl.enum_certificates(store)  # type: ignore[attr-defined]
        except OSError:
            continue
        for cert, encoding, _trust in entries:
            if encoding == "x509_asn":
                pems.append(ssl.DER_cert_to_PEM_cert(cert))
    if not pems:
        return None
    path = Path(data_dir) / "ca-system.pem"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(dict.fromkeys(pems)), encoding="ascii")
    except OSError:
        log.warning("could not write %s", path)
        return None
    return str(path)
