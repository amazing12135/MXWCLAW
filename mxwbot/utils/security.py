"""Security utilities: path validation and command injection detection.

Covers:
  - Path traversal detection (15+ variant patterns including URL-encoded and
    double-written separators)
  - Command injection pattern matching (10+ patterns: chaining, substitution,
    pipes, redirects, backticks)
  - Sensitive text scrubbing
  - Token encryption / decryption (AES-GCM with PBKDF2 key derivation)
  - SSRF URL validation
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import re
import uuid
from pathlib import Path

logger = logging.getLogger("mxwbot.utils.security")

# ---------------------------------------------------------------------------
# Path traversal detection
# ---------------------------------------------------------------------------

# Common traversal patterns (normalised before matching)
_TRAVERSAL_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"(?:^|[\\/])\.\.(?:[\\/]|$)"),                               # ../ or ..\
    re.compile(r"\.\.[\\/]\.\.[\\/]"),                                         # ../../ (repeated)
    re.compile(r"\.{3,}[\\/]\.{3,}"),                                          # ....//.... (dot-stuffed traversal)
    re.compile(r"\.{3,}[\\/]"),                                                # .../  (obfuscated traversal attempt)

    re.compile(r"%2e%2e[%2f%5c]"),                                             # URL-encoded ../
    re.compile(r"%252e%252e[%252f%255c]"),                                     # Double-encoded ../
    re.compile(r"\.\.\s*;\s*[\\/]"),                                           # ..;/  (semicolon trick)
    re.compile(r"file://"),                                                    # file:// URI
    re.compile(r"^[\\/]"),                                                     # Absolute path
    re.compile(r"^[A-Za-z]:[\\/]"),                                            # Windows absolute
    re.compile(r"\\\\\?\?\\"),                                                 # NT device path
    re.compile(r"\\\\\.\\"),                                                   # NT namespace
]

# Characters that should never appear in a user-supplied relative path
_FORBIDDEN_PATH_CHARS = {"\x00"}


def is_path_safe(user_path: str, allowed_dir: str | Path) -> bool:
    """Check whether *user_path* resolves inside *allowed_dir* after
    normalisation.

    Returns True if the path is safe (no traversal detected).
    """
    if not user_path or any(c in user_path for c in _FORBIDDEN_PATH_CHARS):
        return False

    # Decode URL-encoded sequences once
    import urllib.parse

    decoded_once = urllib.parse.unquote(user_path)
    decoded_twice = urllib.parse.unquote(decoded_once)

    for variant in (user_path, decoded_once, decoded_twice):
        for pattern in _TRAVERSAL_PATTERNS:
            if pattern.search(variant):
                return False

    # Resolve the full path and check it's inside allowed_dir
    try:
        base = Path(allowed_dir).resolve()
        target = (base / user_path).resolve()
        # Ensure we don't follow symlinks outside base
        target = target.resolve()
        return str(target).startswith(str(base) + os.sep) or target == base
    except (OSError, ValueError):
        return False


# ---------------------------------------------------------------------------
# Command injection detection
# ---------------------------------------------------------------------------

# Patterns that suggest command injection when found in user-controlled input
_CMD_INJECTION_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"[;|`$]"),                           "shell metacharacter"),
    (re.compile(r"&(?!\s*>\s*&?\d)"),                  "background cmd (& not redirect 2>&1)"),
    (re.compile(r"\$\(.*\)"),                           "$(...) command substitution"),
    (re.compile(r"`[^`]+`"),                            "backtick command substitution"),
    (re.compile(r"\|\s*\w+"),                           "pipe to command"),
    (re.compile(r"&&\s*\w+"),                          "command chaining (&&)"),
    (re.compile(r"\n.*(?:rm|curl|wget|shutdown|reboot|chmod|chown)"), "malicious command"),
    (re.compile(r">\s*(?:/dev/(?!null|zero|random|urandom|stdout|stderr|stdin|fd)|/etc|/proc|C:\\)"), "redirect to sensitive path"),
    (re.compile(r"<\s*(?:/etc|/proc|C:\\)"),            "input redirect from sensitive path"),
    (re.compile(r"\\x[0-9a-fA-F]{2}"),                  "hex-encoded shell"),
    (re.compile(r"\b(?:eval|exec|system|subprocess|os\.system)\s*\("), "code exec function"),
]


def detect_command_injection(command: str) -> list[str]:
    """Check a command string for injection indicators.

    Returns a list of human-readable warnings (empty = safe).
    """
    hits: list[str] = []
    for pattern, label in _CMD_INJECTION_PATTERNS:
        if pattern.search(command):
            hits.append(label)
    return hits


def is_command_safe(command: str) -> bool:
    """Return True if no injection patterns are detected."""
    return len(detect_command_injection(command)) == 0


# ---------------------------------------------------------------------------
# Secret scrubbing
# ---------------------------------------------------------------------------

def sanitize_secrets(text: str, secrets: list[str]) -> str:
    """Replace every occurrence of each secret in *text* with `***`."""
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text


# ---------------------------------------------------------------------------
# Token encryption (AES-GCM with PBKDF2 key derivation)
# ---------------------------------------------------------------------------

_MACHINE_KEY: bytes | None = None
_SALT = b"mxwbot-token-v1"


def _derive_machine_key() -> bytes:
    """Derive a 32-byte AES key from machine-specific identity.

    Uses ``uuid.getnode()`` (MAC address) as the machine identity and
    PBKDF2-HMAC-SHA256 with 100 000 iterations.  The result is cached
    in the module-level ``_MACHINE_KEY`` variable.
    """
    global _MACHINE_KEY
    if _MACHINE_KEY is not None:
        return _MACHINE_KEY

    machine_id = str(uuid.getnode()).encode()
    _MACHINE_KEY = hashlib.pbkdf2_hmac(
        "sha256", machine_id, _SALT, 100_000, dklen=32,
    )
    return _MACHINE_KEY


def encrypt_token(plaintext: str) -> str:
    """Encrypt *plaintext* with AES-256-GCM and return a base64 token.

    If ``cryptography`` is not installed, falls back to base64
    obfuscation (NOT cryptographically secure) and emits a warning.
    """
    if not plaintext:
        return ""

    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        key = _derive_machine_key()
        nonce = os.urandom(12)
        aesgcm = AESGCM(key)
        ciphertext = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)
        return base64.urlsafe_b64encode(nonce + ciphertext).decode("ascii")
    except ImportError:
        logger.warning(
            "cryptography not installed — token stored with base64 "
            "obfuscation only (install cryptography>=41.0 for AES-GCM)"
        )
        return base64.urlsafe_b64encode(plaintext.encode("utf-8")).decode("ascii")


def decrypt_token(encrypted: str) -> str:
    """Decrypt a token produced by ``encrypt_token()``.

    Automatically detects whether the token was AES-GCM encrypted or
    merely base64-obfuscated (based on payload length).
    """
    if not encrypted:
        return ""

    try:
        raw = base64.urlsafe_b64decode(encrypted.encode("ascii"))
    except Exception:
        return ""

    # AES-GCM: nonce(12) + ciphertext(>=16) => len >= 28
    if len(raw) >= 28:
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM

            key = _derive_machine_key()
            nonce, ciphertext = raw[:12], raw[12:]
            aesgcm = AESGCM(key)
            return aesgcm.decrypt(nonce, ciphertext, None).decode("utf-8")
        except ImportError:
            pass
        except Exception:
            logger.warning("Token decryption failed (key may have changed)")
            return ""

    # Fallback: plain base64 obfuscation
    try:
        return raw.decode("utf-8")
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# SSRF URL validation
# ---------------------------------------------------------------------------

import ipaddress
import socket
from urllib.parse import urlparse

# Private / reserved IP ranges that must not be reachable
_PRIVATE_IP_RANGES = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
]


def _is_private_host(hostname: str) -> bool:
    """Return True if *hostname* resolves to any private / reserved IP."""
    if not hostname:
        return False

    # Quick literal check before DNS
    try:
        addr = ipaddress.ip_address(hostname)
        return any(addr in rng for rng in _PRIVATE_IP_RANGES)
    except ValueError:
        pass  # Not an IP literal — resolve via DNS

    try:
        infos = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except socket.gaierror:
        return True  # Cannot resolve — treat as unsafe

    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
            if any(ip in rng for rng in _PRIVATE_IP_RANGES):
                return True
        except ValueError:
            continue
    return False


def validate_url(target: str) -> str:
    """Validate *target* is a safe URL for outbound HTTP requests.

    - Only ``http`` and ``https`` schemes are allowed.
    - The host must not resolve to a private / reserved IP.
    - Returns a normalised URL string.

    Raises ``ValueError`` when the URL is unsafe.
    """
    if not target:
        raise ValueError("empty URL")

    parsed = urlparse(target)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"disallowed URL scheme: {parsed.scheme}")

    hostname = parsed.hostname
    if not hostname:
        raise ValueError("URL has no hostname")

    if _is_private_host(hostname):
        raise ValueError(f"URL resolves to private address: {hostname}")

    return parsed.geturl()
