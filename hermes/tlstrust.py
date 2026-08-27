"""Give Python a set of root certificates it can actually verify against.

The symptom is always the same and always confusing:

    [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed:
    unable to get local issuer certificate

It is almost never a problem with the site. On a macOS python.org build,
OpenSSL is told to look in `.../Python.framework/Versions/X/etc/openssl/cert.pem`,
a file that only exists after somebody runs the `Install Certificates.command`
that ships beside the interpreter. Nobody runs it. So the default context loads
**zero** roots, and every TLS connection fails: web pages an agent tries to
read, IMAP and SMTP, and every cloud AI backend.

The machine is not short of trust material — it has the system keychain, and
usually two or three OpenSSL bundles. The interpreter simply is not pointed at
any of them. So: find one, verify it actually loads, cache it, and use it
everywhere Hermes speaks TLS.

What this deliberately does not do is turn verification off. An agent console
that reads web pages and mail is exactly the wrong place to stop checking who
it is talking to, and "fixing" a certificate error that way converts a loud
failure into a silent one.
"""
from __future__ import annotations

import os
import ssl
import subprocess
import sys
import threading
from pathlib import Path

from . import config

_lock = threading.Lock()
_cached: ssl.SSLContext | None = None
_source = ""

CACHE = "ca-bundle.pem"

# Where a working bundle usually lives, most authoritative first. macOS ships
# /etc/ssl/cert.pem; the Linux entries cover Debian/Ubuntu, RHEL/Fedora, SUSE
# and Alpine; the last two are Homebrew's OpenSSL on Apple silicon and Intel.
BUNDLE_PATHS = (
    "/etc/ssl/cert.pem",
    "/etc/ssl/certs/ca-certificates.crt",
    "/etc/pki/tls/certs/ca-bundle.crt",
    "/etc/ssl/ca-bundle.pem",
    "/etc/ssl/certs/ca-bundle.crt",
    "/usr/local/share/certs/ca-root-nss.crt",
    "/opt/homebrew/etc/openssl@3/cert.pem",
    "/usr/local/etc/openssl@3/cert.pem",
)

# The keychain the system's own roots live in.
MAC_ROOTS = "/System/Library/Keychains/SystemRootCertificates.keychain"


class NoTrustStore(RuntimeError):
    """Raised when no usable set of roots could be found anywhere."""


def _loads(ctx: ssl.SSLContext, *, cafile=None, cadata=None) -> int:
    """Load into a context and report how many roots actually took."""
    try:
        ctx.load_verify_locations(cafile=cafile, cadata=cadata)
    except (ssl.SSLError, OSError, ValueError):
        return 0
    return len(ctx.get_ca_certs())


def _interpreter_bundle() -> str:
    """The bundle the python.org installer ships but does not wire up.

    Located by path rather than by importing it — Hermes has no third-party
    imports and this must not become the first one.
    """
    for base in {Path(sys.prefix), Path(sys.base_prefix)}:
        for lib in base.glob("lib/python*/site-packages/certifi/cacert.pem"):
            return str(lib)
    return ""


def _keychain_roots() -> str:
    """Export the system roots from the macOS keychain as PEM text."""
    if sys.platform != "darwin":
        return ""
    try:
        out = subprocess.run(["/usr/bin/security", "find-certificate", "-a", "-p", MAC_ROOTS],
                             capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout if out.returncode == 0 and "BEGIN CERTIFICATE" in out.stdout else ""


def _cache_path() -> Path:
    return config.HOME / CACHE


def _build() -> tuple:
    """Find roots. Returns (context, description of where they came from)."""
    # 1. The healthy case: the interpreter is already configured, whether by the
    #    OS, by SSL_CERT_FILE, or by somebody having run the installer command.
    ctx = ssl.create_default_context()
    if ctx.get_ca_certs():
        return ctx, "the interpreter's own trust store"

    # 2. A bundle this machine has already been told to use.
    for var in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"):
        path = os.environ.get(var)
        if path and Path(path).is_file():
            ctx = ssl.create_default_context()
            if _loads(ctx, cafile=path):
                return ctx, f"{path} (from ${var})"

    # 3. Something found on a previous run.
    cached = _cache_path()
    if cached.is_file():
        ctx = ssl.create_default_context()
        if _loads(ctx, cafile=str(cached)):
            return ctx, f"{cached}"

    # 4. Go looking, and write down whatever works.
    for path in BUNDLE_PATHS:
        if Path(path).is_file():
            ctx = ssl.create_default_context()
            if _loads(ctx, cafile=path):
                return ctx, path

    bundle = _interpreter_bundle()
    if bundle:
        ctx = ssl.create_default_context()
        if _loads(ctx, cafile=bundle):
            return ctx, f"{bundle} (shipped with this Python, never wired up)"

    pem = _keychain_roots()
    if pem:
        ctx = ssl.create_default_context()
        if _loads(ctx, cadata=pem):
            try:
                config.ensure_dirs()
                cached.write_text(pem)
                cached.chmod(0o600)
            except OSError:
                pass    # caching is an optimisation, not a requirement
            return ctx, "the macOS system keychain"

    raise NoTrustStore(
        "No root certificates could be found on this machine, so Hermes cannot "
        "verify who it is talking to. Nothing that needs TLS will work — web "
        "pages, email, and every cloud AI backend.\n"
        + remedy()
    )


def remedy() -> str:
    """What the operator should actually do about it."""
    if sys.platform == "darwin":
        return ("Fix it by running the certificate installer that shipped with your Python:\n"
                f"    open \"/Applications/Python {sys.version_info.major}."
                f"{sys.version_info.minor}/Install Certificates.command\"\n"
                "or point Hermes at a bundle yourself:\n"
                "    export SSL_CERT_FILE=/etc/ssl/cert.pem")
    return ("Install your distribution's root certificates:\n"
            "    Debian/Ubuntu:  sudo apt install ca-certificates\n"
            "    RHEL/Fedora:    sudo dnf install ca-certificates\n"
            "    Alpine:         apk add ca-certificates\n"
            "or point Hermes at a bundle yourself:\n"
            "    export SSL_CERT_FILE=/path/to/ca-bundle.pem")


def context() -> ssl.SSLContext:
    """A verifying SSL context with roots in it. Built once, then reused."""
    global _cached, _source
    with _lock:
        if _cached is None:
            _cached, _source = _build()
        return _cached


def describe() -> dict:
    """Where the roots came from, for `hermes doctor`. Never raises."""
    try:
        ctx = context()
        return {"ok": True, "source": _source, "certs": len(ctx.get_ca_certs()),
                "detail": f"{len(ctx.get_ca_certs())} roots from {_source}"}
    except NoTrustStore as e:
        return {"ok": False, "source": "", "certs": 0, "detail": str(e).splitlines()[0],
                "remedy": remedy()}


def friendly(error: Exception) -> str:
    """Turn a raw verification failure into something worth reading."""
    text = str(error)
    if "CERTIFICATE_VERIFY_FAILED" not in text and "SSLCertVerificationError" not in text:
        return text
    return (f"{text}\n\nThis is a certificate-trust problem on this machine, not a problem "
            f"with the site.\n{remedy()}")


def reset() -> None:
    """Forget the cached context. For tests, and after changing SSL_CERT_FILE."""
    global _cached, _source
    with _lock:
        _cached, _source = None, ""
