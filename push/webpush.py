"""Web Push, signed and encrypted here rather than by a library.

Web Push is two independent things wearing one name. **VAPID** (RFC 8292) is how
the push service learns who is asking it to deliver: an ES256 JWT signed with a
key pair the gateway generates once and keeps. **aes128gcm** (RFC 8188, applied
by RFC 8291) is how the browser learns that the payload was not read on the way:
a fresh ECDH to the subscription's own public key, per message.

Neither needs a dependency. `pywebpush` would pull in `py_vapid`, `http_ece` and
`pyelliptic`-era transitives for perhaps two hundred lines of arithmetic, and the
Hermes runtime already ships `cryptography`, which is the only part that is
genuinely hard to get right. So this module is written against `cryptography`
directly and the plugin simply declares no Web Push capability on a gateway
where that import fails.

Nothing here is a secret the plugin was given: the VAPID private key is minted
on first use and never leaves the gateway, and the subscription keys are the
browser's own public material.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import struct
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 15

# RFC 8188's record size. One record is plenty: a notification that says who and
# what kind is a few hundred bytes, and multi-record framing would be code with
# no caller.
RECORD_SIZE = 4096


def available() -> bool:
    """Whether this runtime can sign and encrypt at all."""
    try:
        import cryptography  # noqa: F401

        return True
    except Exception:
        return False


def b64(data: bytes) -> str:
    """base64url without padding, which is the only encoding Web Push uses."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def unb64(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


# --- VAPID ------------------------------------------------------------------


def load_or_create_key(path: Path):
    """The gateway's VAPID private key, minted on first use.

    The key identifies this gateway to every push service its users' browsers
    happen to use, and its public half is what every browser subscribes with
    (the advert's `webPush.publicKey`). So it is written 0600 and it is never
    rotated automatically: a new key is a gateway every existing subscription
    stops matching, and each push service then refuses this sender (a 403)
    until the device subscribes again. That is a cost with no benefit unless
    the key leaked, and an operator who decides it did removes the file.

    Minting is exclusive. The key is created at load, and a gateway and a
    `hermes plugins validate` probe can load at the same moment; whichever
    creates the file first wins and the other reads that key back, so neither
    replaces a key somebody may already have subscribed with.
    """
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    if path.exists():
        return _load_existing(path)

    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        # 0600 from the first byte, rather than written and then narrowed.
        descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return _load_existing(path)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(pem)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        # A half-written file would be "unreadable" on every later load, and
        # that is refused rather than replaced. Nothing has subscribed with a
        # key that was never finished, so this one file may go.
        path.unlink(missing_ok=True)
        raise
    os.chmod(path, 0o600)
    return key


def _load_existing(path: Path):
    """A key file that is already there: read it, or refuse. Never replace it."""
    from cryptography.hazmat.primitives import serialization

    try:
        return serialization.load_pem_private_key(path.read_bytes(), password=None)
    except Exception as exc:
        logger.warning("hermie: VAPID key at %s is unreadable (%s); refusing to overwrite it", path, exc)
        raise


def public_key_bytes(key) -> bytes:
    """The uncompressed P-256 point, which is what both VAPID and ECDH carry."""
    from cryptography.hazmat.primitives import serialization

    return key.public_key().public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )


def public_key_b64(key) -> str:
    """The public half as a browser's `applicationServerKey` takes it: 87 characters."""
    return b64(public_key_bytes(key))


def _sign_es256(key, message: bytes) -> bytes:
    """A JWS signature: raw r||s, not the DER sequence `cryptography` returns."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec, utils

    der = key.sign(message, ec.ECDSA(hashes.SHA256()))
    r, s = utils.decode_dss_signature(der)
    return r.to_bytes(32, "big") + s.to_bytes(32, "big")


def vapid_header(key, endpoint: str, contact: str, *, now: Optional[int] = None) -> str:
    """The `Authorization` value for one endpoint.

    The audience is the push service's origin and nothing else — a token minted
    for Mozilla's service is not valid at Apple's, which is the point.
    """
    parsed = urllib.parse.urlparse(endpoint)
    audience = f"{parsed.scheme}://{parsed.netloc}"
    issued = int(now if now is not None else time.time())

    header = b64(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
    claims = {"aud": audience, "exp": issued + 12 * 3600, "sub": contact or "mailto:admin@localhost"}
    body = b64(json.dumps(claims, separators=(",", ":")).encode())
    signing_input = f"{header}.{body}".encode("ascii")
    signature = b64(_sign_es256(key, signing_input))
    return f"vapid t={header}.{body}.{signature}, k={public_key_b64(key)}"


# --- aes128gcm (RFC 8291) ---------------------------------------------------


def _hkdf(salt: bytes, ikm: bytes, info: bytes, length: int) -> bytes:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    return HKDF(algorithm=hashes.SHA256(), length=length, salt=salt, info=info).derive(ikm)


def encrypt(payload: bytes, p256dh: str, auth: str, *, salt: Optional[bytes] = None, ephemeral=None) -> bytes:
    """One aes128gcm body for one subscription.

    `salt` and `ephemeral` are injectable so a test can assert against the RFC's
    own vectors; in production both are fresh per message, which is what makes
    the scheme worth anything.
    """
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    user_public_bytes = unb64(p256dh)
    auth_secret = unb64(auth)
    user_public = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), user_public_bytes)

    ephemeral = ephemeral or ec.generate_private_key(ec.SECP256R1())
    ephemeral_public_bytes = public_key_bytes(ephemeral)
    salt = salt or os.urandom(16)

    shared = ephemeral.exchange(ec.ECDH(), user_public)

    # RFC 8291 §3.3: the auth secret salts the first extraction, and the info
    # string binds the derived key to BOTH public keys, so a key derived for
    # one subscription cannot be replayed at another.
    key_info = b"WebPush: info\x00" + user_public_bytes + ephemeral_public_bytes
    ikm = _hkdf(auth_secret, shared, key_info, 32)

    content_key = _hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)

    # 0x02 is RFC 8188's "this is the last record" delimiter.
    ciphertext = AESGCM(content_key).encrypt(nonce, payload + b"\x02", None)

    header = salt + struct.pack("!L", RECORD_SIZE) + bytes([len(ephemeral_public_bytes)]) + ephemeral_public_bytes
    return header + ciphertext


# --- sending ----------------------------------------------------------------


@dataclass(frozen=True)
class Result:
    status: int
    error: str = ""

    @property
    def device_gone(self) -> bool:
        """404 and 410 are the push services' way of saying the subscription died."""
        return self.status in (404, 410)

    @property
    def key_mismatch(self) -> bool:
        """403: the push service will not take this sender's key for this subscription.

        The subscription was made with another VAPID key than the one this
        gateway signs with (another sender's, or this gateway's before its key
        file was replaced). Asking again changes nothing until the device
        subscribes with this gateway's key, so the row is retired like a dead one.
        """
        return self.status == 403

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


def _request(url: str, body: bytes, headers: Dict[str, str]) -> Tuple[int, str]:
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        return response.status, ""


def send(
    key,
    endpoint: str,
    p256dh: str,
    auth: str,
    payload: Dict[str, Any],
    *,
    contact: str = "",
    ttl: int = 3600,
    request=_request,
) -> Result:
    """Encrypt *payload* for one subscription and hand it to its push service."""
    try:
        body = encrypt(json.dumps(payload, separators=(",", ":")).encode("utf-8"), p256dh, auth)
        headers = {
            "Content-Encoding": "aes128gcm",
            "Content-Type": "application/octet-stream",
            "Content-Length": str(len(body)),
            "TTL": str(ttl),
            # `high` wakes a sleeping device; anything Hermie sends is something
            # a person asked to be told about, so there is no low-urgency case.
            "Urgency": "high",
            "Authorization": vapid_header(key, endpoint, contact),
        }
        status, _ = request(endpoint, body, headers)
        return Result(status=status)
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:200]
        except Exception:
            pass
        return Result(status=exc.code, error=detail)
    except Exception as exc:
        logger.warning("hermie: web push to %s failed: %s", urllib.parse.urlparse(endpoint).netloc, exc)
        return Result(status=0, error=str(exc))
