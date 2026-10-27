"""Release signatures: Ed25519 (RFC 8032) over the exact bytes of ``eutonos-release.json``.

A release is trusted because its manifest carries a valid signature from a key this build already knows
(``PINNED_KEYS``, shipped in the package), never because of where it was downloaded from. The manifest names the
wheel's sha256, so signing the manifest signs the wheel too. The signature travels beside the manifest as
``eutonos-release.json.sig``: the 64 signature bytes as 128 lowercase hex digits and a newline.

This is a small pure-standard-library implementation of the RFC 8032 reference algorithm, so the installer needs no
third-party package to check an update. ``verify`` handles only public data. ``sign`` and ``public_key`` exist for the
release tool (``tools/release.py``) and the tests; they are not constant-time and the private key never belongs in this
repository or in a build.

``PINNED_KEYS`` holds the public half of the release signing key made with ``python tools/release.py keygen --out FILE``
(key id 4822e3a9, 2026-10-26). With no pinned key every network update is refused ("no-trusted-key"): the check fails
closed instead of trusting an unsigned download. A release folder may be used unsigned only with the explicit
``--allow-unsigned`` developer flag.
"""
from __future__ import annotations

import hashlib
import re

from . import tokenslang as ts

SIGNATURE_NAME = "eutonos-release.json.sig"

# Public keys (64 lowercase hex digits each) whose signatures this build accepts. A second entry lets a new key start
# signing before the old one is retired. Only public keys appear here; a private key never does.
PINNED_KEYS: tuple[str, ...] = (
    "3d20bd7dbc447406c1b63eccde8ff8b79659824cfd784b2cd91b2902faffae75",  # key id 4822e3a9, made 2026-10-26
)

_P = 2 ** 255 - 19
_Q = 2 ** 252 + 27742317777372353535851937790883648493
_D = -121665 * pow(121666, _P - 2, _P) % _P
_SQRT_M1 = pow(2, (_P - 1) // 4, _P)
_HEX_KEY = re.compile(r"^[0-9a-f]{64}$")
_HEX_SIG = re.compile(r"^[0-9a-f]{128}$")


def _add(a, b):
    x1, y1, z1, t1 = a
    x2, y2, z2, t2 = b
    pa = (y1 - x1) * (y2 - x2) % _P
    pb = (y1 + x1) * (y2 + x2) % _P
    pc = 2 * t1 * t2 * _D % _P
    pd = 2 * z1 * z2 % _P
    e, f, g, h = pb - pa, pd - pc, pd + pc, pb + pa
    return (e * f % _P, g * h % _P, f * g % _P, e * h % _P)


def _mul(scalar, point):
    result = (0, 1, 1, 0)
    while scalar > 0:
        if scalar & 1:
            result = _add(result, point)
        point = _add(point, point)
        scalar >>= 1
    return result


def _equal(a, b):
    return (a[0] * b[2] - b[0] * a[2]) % _P == 0 and (a[1] * b[2] - b[1] * a[2]) % _P == 0


def _recover_x(y, sign):
    if y >= _P:
        return None
    x2 = (y * y - 1) * pow(_D * y * y + 1, _P - 2, _P) % _P
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P != 0:
        x = x * _SQRT_M1 % _P
    if (x * x - x2) % _P != 0:
        return None
    if (x & 1) != sign:
        x = _P - x
    return x


_GY = 4 * pow(5, _P - 2, _P) % _P
_GX = _recover_x(_GY, 0)
_G = (_GX, _GY, 1, _GX * _GY % _P)


def _compress(point):
    zinv = pow(point[2], _P - 2, _P)
    x, y = point[0] * zinv % _P, point[1] * zinv % _P
    return int.to_bytes(y | ((x & 1) << 255), 32, "little")


def _decompress(data):
    if len(data) != 32:
        return None
    y = int.from_bytes(data, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    x = _recover_x(y, sign)
    if x is None:
        return None
    return (x, y, 1, x * y % _P)


def _hash_q(data):
    return int.from_bytes(hashlib.sha512(data).digest(), "little") % _Q


def _expand(seed):
    if len(seed) != 32:
        raise ValueError("An Ed25519 private key seed is 32 bytes.")
    digest = hashlib.sha512(seed).digest()
    scalar = int.from_bytes(digest[:32], "little")
    scalar &= (1 << 254) - 8
    scalar |= 1 << 254
    return scalar, digest[32:]


def public_key(seed: bytes) -> bytes:
    """The 32-byte public key for a 32-byte private seed."""
    return _compress(_mul(_expand(seed)[0], _G))


def sign(seed: bytes, message: bytes) -> bytes:
    """The 64-byte signature of `message` (release tool and tests only)."""
    scalar, prefix = _expand(seed)
    public = _compress(_mul(scalar, _G))
    r = _hash_q(prefix + message)
    point_r = _compress(_mul(r, _G))
    h = _hash_q(point_r + public + message)
    return point_r + int.to_bytes((r + h * scalar) % _Q, 32, "little")


def verify(public: bytes, signature: bytes, message: bytes) -> bool:
    """True only for a valid signature of `message` under the 32-byte `public` key. Never raises on bad input."""
    if len(public) != 32 or len(signature) != 64:
        return False
    point_a = _decompress(public)
    point_r = _decompress(signature[:32])
    if point_a is None or point_r is None:
        return False
    s = int.from_bytes(signature[32:], "little")
    if s >= _Q:
        return False
    h = _hash_q(signature[:32] + public + message)
    return _equal(_mul(s, _G), _add(point_r, _mul(h, point_a)))


def key_id(public_hex: str) -> str:
    """A short, stable name for a public key (the first 8 hex digits of its sha256) for messages and records."""
    return hashlib.sha256(bytes.fromhex(public_hex)).hexdigest()[:8]


def signature_text(signature: bytes) -> str:
    """The ``.sig`` file's text for a 64-byte signature."""
    return signature.hex() + "\n"


def check_keys(keys) -> tuple[str, ...]:
    """`keys` as a tuple of valid public-key hex strings, or ``ValueError`` (a bad pinned key is a build bug)."""
    found = tuple(keys)
    for key in found:
        if not isinstance(key, str) or _HEX_KEY.match(key) is None:
            raise ValueError("A pinned release key must be 64 lowercase hex digits.")
    return found


def verify_manifest(manifest_bytes: bytes, signature_file, keys=None) -> str:
    """The key id that signed `manifest_bytes`, or a ``ReadError``: ``unsigned`` (no signature file),
    ``no-trusted-key`` (this build pins no key, so nothing can be checked) or ``bad-signature``. `signature_file` is the
    ``.sig`` file's bytes (or text) or None."""
    trusted = check_keys(PINNED_KEYS if keys is None else keys)
    if signature_file is None or (isinstance(signature_file, (bytes, str)) and not signature_file.strip()):
        raise ts.ReadError("unsigned", "The release has no signature (eutonos-release.json.sig).")
    if not trusted:
        raise ts.ReadError("no-trusted-key", "This build of EUTONOS carries no release signing key, so a release "
                           "signature cannot be checked.")
    text = signature_file.decode("ascii", "replace") if isinstance(signature_file, bytes) else str(signature_file)
    text = text.strip().lower()
    if _HEX_SIG.match(text) is None:
        raise ts.ReadError("bad-signature", "The release signature is not 128 hex digits.")
    raw = bytes.fromhex(text)
    for key in trusted:
        if verify(bytes.fromhex(key), raw, manifest_bytes):
            return key_id(key)
    raise ts.ReadError("bad-signature", "The release signature does not match any key this build trusts.")


def check_release(manifest_bytes: bytes, signature_file, *, allow_unsigned: bool = False, keys=None) -> str:
    """``"verified"`` (signed by a pinned key) or, only with `allow_unsigned`, ``"unsigned-allowed"``; otherwise a
    ``ReadError``. `allow_unsigned` is the explicit developer override for a release folder: it accepts a release with
    no signature or when this build pins no key, but a signature that is present is still refused when it is wrong and
    a key is pinned."""
    trusted = check_keys(PINNED_KEYS if keys is None else keys)
    try:
        verify_manifest(manifest_bytes, signature_file, trusted)
        return "verified"
    except ts.ReadError as exc:
        if allow_unsigned and (exc.code == "unsigned" or (exc.code == "no-trusted-key")):
            return "unsigned-allowed"
        raise
