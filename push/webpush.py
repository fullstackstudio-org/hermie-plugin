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
import tempfile
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


class UnreadableKey(Exception):
    """A key file is there and is not a key. It is refused, never replaced."""


def load_or_create_key(path: Path, *, create: bool = True):
    """The gateway's VAPID private key, minted on first use.

    The key identifies this gateway to every push service its users' browsers
    happen to use, and its public half is what every browser subscribes with
    (the advert's `webPush.publicKey`). So it is written 0600 and it is never
    rotated automatically: a new key is a gateway every existing subscription
    stops matching, and each push service then refuses this sender until the
    device subscribes again. That is a cost with no benefit unless the key
    leaked, and an operator who decides it did removes the file.

    Minting is exclusive and whole. The PEM is written to a 0600 temporary file
    beside the key, flushed to disk, and hard-linked into place, which fails if
    a key is already there: a gateway and a `hermes plugins validate` probe
    loading at the same moment end up with one key, and no reader ever sees a
    half-written file it would then refuse for good. Where the file system has
    no hard links, the file is created with `O_EXCL` instead.

    With `create=False` a missing file answers ``None`` and nothing is written.
    An unreadable file raises :class:`UnreadableKey` either way; the caller
    decides how loudly to say so.
    """
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    if path.exists():
        return _load_existing(path)
    if not create:
        return None

    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    # mkstemp creates the file 0600, so the key is never readable by others.
    descriptor, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=".vapid-", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(pem)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, str(path))
        except FileExistsError:
            return _load_existing(path)
        except OSError:
            return _create_exclusive(path, pem, key)
    finally:
        try:
            os.unlink(temporary)
        except OSError:
            pass
    return key


def _create_exclusive(path: Path, pem: bytes, key):
    """The same promise without a hard link: created only if absent, removed if unfinished."""
    try:
        descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return _load_existing(path)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(pem)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return key


def _load_existing(path: Path):
    """A key file that is already there: read it, or refuse. Never replace it."""
    from cryptography.hazmat.primitives import serialization

    try:
        return serialization.load_pem_private_key(path.read_bytes(), password=None)
    except Exception as exc:
        raise UnreadableKey(f"{path} is not a usable key ({type(exc).__name__}); refusing to overwrite it") from exc


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


# How the push services word "this subscription was made with another key" in
# a 403 body, lowercased. Apple names a reason (`VapidPkHashMismatch`); FCM
# writes a sentence, in an older and a newer wording. Mozilla's autopush answers
# a mismatched key with 401 rather than 403, so it is not read here.
KEY_MISMATCH_MARKERS = (
    "vapidpkhashmismatch",
    "does not correspond to the sender id used to subscribe",
    "do not correspond to the credentials used to create the subscription",
)

# How much of an error body is kept: enough to find a marker in, little enough
# to put into a log line.
ERROR_BYTES = 1000


@dataclass(frozen=True)
class Result:
    status: int
    error: str = ""

    @property
    def device_gone(self) -> bool:
        """404 and 410 are the push services' way of saying the subscription died."""
        return self.status in (404, 410)

    @property
    def forbidden(self) -> bool:
        """403: the push service refused this sender. Not by itself a key mismatch."""
        return self.status == 403

    @property
    def key_mismatch(self) -> bool:
        """A 403 whose body says the subscription was made with another VAPID key.

        Asking again changes nothing until the device subscribes with this
        gateway's key, so the row is retired like a dead one. A 403 that does not
        say so may be about something else entirely — Apple's `BadJwtToken` for
        a `sub` it will not take reads the same status — and is not this.
        """
        if not self.forbidden:
            return False
        text = self.error.lower()
        return any(marker in text for marker in KEY_MISMATCH_MARKERS)

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
            detail = exc.read(ERROR_BYTES).decode("utf-8", "replace")
        except Exception:
            pass
        return Result(status=exc.code, error=detail)
    except Exception as exc:
        logger.warning("hermie: web push to %s failed: %s", urllib.parse.urlparse(endpoint).netloc, exc)
        return Result(status=0, error=str(exc))
