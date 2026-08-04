"""Identity: user-agent strings, Web Bot Auth signing, well-known directory (section 3).

Two modes, both required.

`stealth` (default) presents a realistic browser identity: current Chrome UA,
browser-ordered headers, and curl_cffi TLS impersonation at tier 2.5.

`declared` self-identifies honestly and signs its requests with Web Bot Auth:
an Ed25519 key, HTTP Message Signatures (RFC 9421) over the request, and a
`Signature-Agent` header pointing at a directory this server publishes at
`/.well-known/http-message-signatures-directory`.

Registration with Cloudflare's bot directory is a later manual step and is not
part of this build -- but the signing plumbing and the well-known endpoint are,
so registering later is a form submission rather than a rebuild.

robots.txt is not consulted in either mode. See DECISIONS.md.
"""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import logging
import time
from typing import Any
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from . import config

logger = logging.getLogger(__name__)

# A current, ordinary desktop Chrome identity. Kept in one place because a UA
# that drifts out of step with the Chromium patchright actually drives is
# itself a detection signal.
CHROME_MAJOR = "141"
STEALTH_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    f"(KHTML, like Gecko) Chrome/{CHROME_MAJOR}.0.0.0 Safari/537.36"
)

DECLARED_UA = (
    f"RenderFetch/1.0 (+{config.CONTACT_URL}; MCP reader on behalf of one operator) "
    "python-httpx"
)


def _installed(module: str) -> bool:
    """Is this codec package available for httpx to decode with?"""
    return importlib.util.find_spec(module) is not None


def _supported_encodings() -> str:
    """Build Accept-Encoding from codecs we can actually decode.

    Chrome sends "gzip, deflate, br, zstd", so stealth mode wants to as well.
    But advertising an encoding httpx cannot decode does not degrade
    gracefully -- the response body comes back as undecoded compressed bytes,
    extraction yields nothing, and every page silently falls through to the
    expensive tiers. So the header is derived from what is installed rather
    than asserted, and a missing optional codec costs realism, not correctness.
    """
    encodings = ["gzip", "deflate"]
    if _installed("brotli") or _installed("brotlicffi"):
        encodings.append("br")
    else:
        logger.warning("brotli not installed; not advertising 'br' (less browser-like)")
    if _installed("zstandard"):
        encodings.append("zstd")
    return ", ".join(encodings)


ACCEPT_ENCODING = _supported_encodings()

# Header order matters as much as header content; this is Chrome's order for a
# top-level navigation.
_STEALTH_HEADERS: tuple[tuple[str, str], ...] = (
    ("sec-ch-ua", f'"Chromium";v="{CHROME_MAJOR}", "Not?A_Brand";v="24", "Google Chrome";v="{CHROME_MAJOR}"'),
    ("sec-ch-ua-mobile", "?0"),
    ("sec-ch-ua-platform", '"Windows"'),
    ("upgrade-insecure-requests", "1"),
    ("user-agent", STEALTH_UA),
    ("accept", "text/markdown,text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"),
    ("sec-fetch-site", "none"),
    ("sec-fetch-mode", "navigate"),
    ("sec-fetch-user", "?1"),
    ("sec-fetch-dest", "document"),
    ("accept-encoding", ACCEPT_ENCODING),
    ("accept-language", "en-US,en;q=0.9"),
)

_DECLARED_HEADERS: tuple[tuple[str, str], ...] = (
    ("user-agent", DECLARED_UA),
    ("accept", "text/markdown, text/html;q=0.9, */*;q=0.8"),
    ("accept-language", "en-US,en;q=0.9"),
    ("accept-encoding", ACCEPT_ENCODING),
    ("from", config.CONTACT_URL),
)


def resolve_mode(override: str | None = None) -> str:
    """Per-call override wins over the IDENTITY_MODE secret."""
    if override:
        mode = override.strip().lower()
        if mode in ("stealth", "declared"):
            return mode
        logger.warning("ignoring unknown identity_mode override %r", override)
    return config.IDENTITY_MODE


def base_headers(mode: str) -> dict[str, str]:
    pairs = _DECLARED_HEADERS if mode == "declared" else _STEALTH_HEADERS
    return dict(pairs)


# --- Web Bot Auth -----------------------------------------------------------

_key_cache: ed25519.Ed25519PrivateKey | None = None
_key_generated_this_boot = False


def _load_or_generate_key() -> ed25519.Ed25519PrivateKey:
    """Return the Ed25519 signing key, generating one at first boot.

    A generated key is ephemeral: it changes on every cold start, so the
    published directory means nothing until the operator provisions a stable
    key via the SIGNING_KEY_PEM Secret. The key material itself is never
    logged -- logs are not a private-key store.
    """
    global _key_cache, _key_generated_this_boot
    if _key_cache is not None:
        return _key_cache

    pem = config.SIGNING_KEY_PEM.strip()
    if pem:
        try:
            loaded = serialization.load_pem_private_key(pem.encode(), password=None)
            if not isinstance(loaded, ed25519.Ed25519PrivateKey):
                raise TypeError(f"expected an Ed25519 key, got {type(loaded).__name__}")
            _key_cache = loaded
            return _key_cache
        except Exception as exc:
            logger.error("SIGNING_KEY_PEM could not be loaded (%s); generating an ephemeral key", exc)

    _key_cache = ed25519.Ed25519PrivateKey.generate()
    _key_generated_this_boot = True
    logger.warning(
        "no usable SIGNING_KEY_PEM; generated an ephemeral Ed25519 key that will "
        "change on the next cold start. Provision a stable key via the "
        "SIGNING_KEY_PEM Secret (generate locally, e.g.: "
        "python -c \"from cryptography.hazmat.primitives.asymmetric import ed25519; "
        "from cryptography.hazmat.primitives import serialization as s; "
        "print(ed25519.Ed25519PrivateKey.generate().private_bytes("
        "s.Encoding.PEM, s.PrivateFormat.PKCS8, s.NoEncryption()).decode())\")."
    )
    return _key_cache


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def public_jwk() -> dict[str, Any]:
    key = _load_or_generate_key()
    raw = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    jwk = {"kty": "OKP", "crv": "Ed25519", "x": _b64url(raw), "alg": "EdDSA", "use": "sig"}
    jwk["kid"] = key_thumbprint(jwk)
    return jwk


def key_thumbprint(jwk: dict[str, Any]) -> str:
    """RFC 7638 JWK thumbprint, which is the key id Web Bot Auth expects."""
    canonical = json.dumps(
        {"crv": jwk["crv"], "kty": jwk["kty"], "x": jwk["x"]},
        separators=(",", ":"),
        sort_keys=True,
    )
    return _b64url(hashlib.sha256(canonical.encode()).digest())


def signature_agent_url() -> str:
    if config.PUBLIC_ORIGIN:
        return f"{config.PUBLIC_ORIGIN}/.well-known/http-message-signatures-directory"
    return "/.well-known/http-message-signatures-directory"


def directory_document() -> dict[str, Any]:
    """The document served at the well-known endpoint (T13)."""
    return {"keys": [public_jwk()]}


def key_is_ephemeral() -> bool:
    _load_or_generate_key()
    return _key_generated_this_boot


def sign_request(method: str, url: str, headers: dict[str, str]) -> dict[str, str]:
    """Add RFC 9421 HTTP Message Signature headers for Web Bot Auth.

    Covers @method, @authority, @path and signature-agent, which is the
    component set Cloudflare's Web Bot Auth profile expects. Returns a new
    header mapping; the input is not mutated.
    """
    key = _load_or_generate_key()
    jwk = public_jwk()
    parts = urlsplit(url)
    authority = parts.netloc.lower()
    path = parts.path or "/"
    if parts.query:
        path = f"{path}?{parts.query}"

    agent = signature_agent_url()
    created = int(time.time())
    expires = created + 300
    keyid = jwk["kid"]

    covered = '("@method" "@authority" "@path" "signature-agent")'
    params = f'{covered};created={created};expires={expires};keyid="{keyid}";alg="ed25519";tag="web-bot-auth"'

    base = "\n".join(
        [
            f'"@method": {method.upper()}',
            f'"@authority": {authority}',
            f'"@path": {path}',
            f'"signature-agent": "{agent}"',
            f'"@signature-params": {params}',
        ]
    )
    sig = key.sign(base.encode())

    out = dict(headers)
    out["signature-agent"] = f'"{agent}"'
    out["signature-input"] = f"sig1={params}"
    out["signature"] = f"sig1=:{base64.b64encode(sig).decode()}:"
    return out


def headers_for(mode: str, method: str, url: str) -> dict[str, str]:
    """Full outbound header set for a request in the given identity mode."""
    headers = base_headers(mode)
    if mode == "declared":
        try:
            headers = sign_request(method, url, headers)
        except Exception as exc:  # signing must never take the fetch down
            logger.error("Web Bot Auth signing failed, sending unsigned: %s", exc)
    return headers
