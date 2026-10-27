"""Where updates come from: the public releases-only GitHub repository over plain HTTPS, or a local folder.

Every release carries a manifest asset ``eutonos-release.json``::

    {"profile": "eutonos.release.v1", "version": "1.4.0", "artifact_id": "eutonos-1.4.0-<12 hex>",
     "wheel": "eutonos-1.4.0-py3-none-any.whl", "sha256": "<64 hex>", "channels": ["stable"],
     "min_compatible": "1.2.0", "source_commit": "<40 hex, optional>", "plugin_version": "1.4.0",
     "interpreter": "cp314-win_amd64", "files": {"<wheel>": {"sha256": "<64 hex>", "size": 123}}}

``source_commit`` is optional: the git commit the release was built from. A local patch is only reported as included
in a release when this names a commit the patch's own commit is an ancestor of. ``plugin_version``, ``interpreter``
and ``files`` are optional too (``tools/release.py`` writes all three): the Claude plugin version stamped into the
kit, the CPython the bundled kit wheels need, and the sha256 and size of each published file.

Every manifest is signed: ``eutonos-release.json.sig`` holds the Ed25519 signature of the manifest's exact bytes
(``release_signing``). A source verifies the signature before it parses the manifest and marks the release it returns
with ``trust`` (``verified``, or ``unsigned-allowed`` for a local folder opened with the developer override). A release
whose signature is missing or wrong is never returned; it is listed in the source's ``rejected`` list so the caller can
say why nothing was found. The network source has no override.

Each release also carries the exact ``manifest_bytes`` and ``signature_bytes`` that were verified, so a caller can keep
a copy that verifies again later (the update staging folder does).

Both sources answer the same two questions: ``releases()`` (each with its validated manifest) and
``fetch_wheel(release, folder)``. ``GitHubSource`` uses the standard library only (``urllib``): unauthenticated GETs with timeouts and size caps to
GitHub's release API and asset hosts, never a write and never a credential; the wheel's sha256 is checked against
the manifest before it is handed back.
Promotion between channels belongs to a later campaign; here only the manifest's ``channels`` list matters.
"""
from __future__ import annotations

import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import shutil
import sys
import sysconfig
import urllib.error
import urllib.parse
import urllib.request

from . import release_signing as sig
from . import tokenslang as ts

PROFILE = "eutonos.release.v1"
MANIFEST_NAME = "eutonos-release.json"
DEFAULT_REPO = "EUTONOS/releases"  # the public, releases-only repository (the code repository stays private)
CHANNELS = ("dev", "beta", "stable")
LIST_LIMIT = 20
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_ARTIFACT = re.compile(r"^eutonos-[0-9A-Za-z.+_-]+-[0-9a-f]{12}$")


def version_tuple(text) -> tuple:
    """The leading numeric parts of a version (``1.4.0rc1`` sorts as ``(1, 4, 0)``)."""
    parts = []
    for item in str(text).replace("-", ".").replace("+", ".").split("."):
        if not item.isdigit():
            break
        parts.append(int(item))
    return tuple(parts)


def validate_manifest(value) -> dict:
    """The manifest as a plain dict, or a ``ReadError`` naming the first thing wrong."""
    ts.require(isinstance(value, dict) and value.get("profile") == PROFILE, "invalid",
               f"A release manifest must have the profile {PROFILE}.")
    for name in ("version", "artifact_id", "wheel", "sha256"):
        ts.require(isinstance(value.get(name), str) and value[name].strip(), "invalid", f"The release manifest has no {name}.")
    ts.require(_HEX64.match(value["sha256"]) is not None, "invalid", "The release sha256 is not 64 lowercase hex digits.")
    ts.require(_ARTIFACT.match(value["artifact_id"]) is not None, "invalid", "The release artifact_id has the wrong shape.")
    wheel = value["wheel"]
    ts.require(wheel == Path(wheel).name and wheel.endswith(".whl") and ".." not in wheel and "/" not in wheel
               and "\\" not in wheel, "invalid", "The release wheel must be a plain .whl file name.")
    channels = value.get("channels")
    ts.require(isinstance(channels, list) and channels and all(item in CHANNELS for item in channels), "invalid",
               "The release channels must be a non-empty list of dev, beta or stable.")
    floor = value.get("min_compatible")
    ts.require(floor is None or (isinstance(floor, str) and floor.strip()), "invalid", "min_compatible must be a version or null.")
    checked = {"profile": PROFILE, "version": value["version"], "artifact_id": value["artifact_id"], "wheel": wheel,
               "sha256": value["sha256"], "channels": list(channels), "min_compatible": floor}
    if "source_commit" in value:
        commit = value["source_commit"]
        ts.require(commit is None or (isinstance(commit, str) and _COMMIT.match(commit) is not None), "invalid",
                   "The release source_commit must be 40 or 64 lowercase hex digits or null.")
        checked["source_commit"] = commit
    for name in ("plugin_version", "interpreter"):
        if name in value:
            ts.require(value[name] is None or (isinstance(value[name], str) and value[name].strip()), "invalid",
                       f"The release {name} must be text or null.")
            checked[name] = value[name]
    if "files" in value:
        files = value["files"]
        ts.require(isinstance(files, dict) and all(
            isinstance(key, str) and key == Path(key).name and isinstance(item, dict) and _HEX64.match(str(item.get("sha256"))) is not None
            and isinstance(item.get("size"), int) and not isinstance(item.get("size"), bool) and item["size"] >= 0
            for key, item in files.items()), "invalid", "The release files must map plain file names to a sha256 and a size.")
        ts.require(wheel not in files or files[wheel]["sha256"] == value["sha256"], "invalid",
                   "The release sha256 and the wheel's entry in files disagree.")
        checked["files"] = {key: {"sha256": item["sha256"], "size": item["size"]} for key, item in files.items()}
    return checked


def newest(releases: list[dict], channel: str) -> dict | None:
    """The newest release whose manifest names `channel` (highest version, then latest publication)."""
    ts.require(channel in CHANNELS, "invalid", f"Unknown channel: {channel}")
    chosen = [item for item in releases if channel in item["manifest"]["channels"]]
    chosen.sort(key=lambda item: (version_tuple(item["manifest"]["version"]), item.get("published") or ""), reverse=True)
    return chosen[0] if chosen else None


class FolderSource:
    """A local folder: each release is a sub-folder holding ``eutonos-release.json``, its ``.sig`` and the wheel.
    `allow_unsigned` is the explicit developer override (a release without a signature, or a build that pins no key,
    is accepted and marked ``unsigned-allowed``); `keys` replaces the pinned keys (tests)."""

    kind = "folder"

    def __init__(self, folder, *, allow_unsigned: bool = False, keys=None):
        self.folder = Path(folder)
        self.allow_unsigned = allow_unsigned
        self.keys = keys
        self.rejected: list[dict] = []

    def releases(self) -> list[dict]:
        found = []
        self.rejected = []
        if not self.folder.is_dir():
            return found
        for sub in sorted(item for item in self.folder.iterdir() if item.is_dir()):
            path = sub / MANIFEST_NAME
            if not path.is_file():
                continue
            try:
                data = path.read_bytes()
                signature = (sub / sig.SIGNATURE_NAME).read_bytes() if (sub / sig.SIGNATURE_NAME).is_file() else None
            except OSError:
                continue
            try:
                trust = sig.check_release(data, signature, allow_unsigned=self.allow_unsigned, keys=self.keys)
            except ts.ReadError as exc:
                self.rejected.append({"id": sub.name, "reason": exc.code, "detail": str(exc)})
                continue
            try:
                manifest = validate_manifest(json.loads(data.decode("utf-8")))
            except (ValueError, ts.ReadError):
                continue  # a damaged release is skipped, never trusted
            found.append({"id": sub.name, "manifest": manifest, "published": None, "trust": trust,
                          "manifest_bytes": data, "signature_bytes": signature})
        return found

    def fetch_wheel(self, release: dict, destination: Path) -> Path:
        source = self.folder / release["id"] / release["manifest"]["wheel"]
        ts.require(source.is_file(), "missing", f"The release folder has no {release['manifest']['wheel']}.")
        destination = Path(destination)
        destination.mkdir(parents=True, exist_ok=True)
        target = destination / release["manifest"]["wheel"]
        shutil.copyfile(source, target)
        return target


# ---------------------------------------------------------------------------------------- GitHub (stdlib HTTPS)
API = "https://api.github.com"
USER_AGENT = "eutonos-update-check"
HTTP_TIMEOUT = 20.0               # seconds, per request (connect and each read)
MANIFEST_LIMIT = 256 * 1024       # a manifest is a few KB; anything bigger is not one
SIGNATURE_LIMIT = 4 * 1024
LISTING_LIMIT = 4 * 1024 * 1024   # the releases listing JSON
WHEEL_LIMIT = 512 * 1024 * 1024   # an absolute ceiling; the manifest's own size for the wheel is the tighter cap
_REPO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}/[A-Za-z0-9._-]{1,100}$")
_HOSTS = ("api.github.com", "github.com")
_HOST_SUFFIXES = (".githubusercontent.com",)  # GitHub's own release-asset hosts (redirect targets)


def check_repo(repo) -> str:
    """`repo` as ``owner/name`` or a ``ReadError``; nothing else may reach a URL."""
    ts.require(isinstance(repo, str) and _REPO.match(repo) is not None, "invalid",
               "The releases repository must be written owner/name.")
    return repo


def _allowed_url(url) -> bool:
    if not isinstance(url, str):
        return False
    parts = urllib.parse.urlsplit(url)
    host = (parts.hostname or "").lower()
    return (parts.scheme == "https" and not parts.username and parts.port in (None, 443)
            and (host in _HOSTS or host.endswith(_HOST_SUFFIXES)))


class _GitHubRedirects(urllib.request.HTTPRedirectHandler):
    """A redirect may only lead to HTTPS on GitHub's own hosts."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _allowed_url(newurl):
            raise urllib.error.URLError("the release host redirected somewhere that is not GitHub over https")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def urlopen(url: str, headers: dict | None = None):
    """The one network call: an unauthenticated GET over HTTPS to a GitHub host, with a timeout. Returns the response
    (a context manager with ``read(n)``). The tests replace it; no credential is ever attached."""
    ts.require(_allowed_url(url), "unavailable", "The release host is not a GitHub https address.")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})}, method="GET")
    return urllib.request.build_opener(_GitHubRedirects).open(request, timeout=HTTP_TIMEOUT)


class GitHubSource:
    """GitHub Releases of the public releases-only repository `repo`, over the unauthenticated REST API and plain HTTPS
    downloads from the standard library. Nothing is written to GitHub and no credential is used. Every body is read
    under a size cap and the wheel's sha256 is checked against the manifest before ``fetch_wheel`` returns it."""

    kind = "github"

    def __init__(self, repo: str | None = None, *, opener=None, limit: int = LIST_LIMIT, keys=None):
        self.repo = check_repo(repo or os.environ.get("EUTONOS_RELEASE_REPO") or DEFAULT_REPO)
        self.opener = opener or urlopen
        self.limit = limit
        self.keys = keys  # replaces the pinned keys (tests)
        self.rejected: list[dict] = []

    def _get(self, url: str, cap: int, *, accept: str | None = None) -> bytes:
        try:
            with self.opener(url, {"Accept": accept} if accept else {}) as response:
                body = response.read(cap + 1)
        except ts.ReadError:
            raise
        except urllib.error.HTTPError as exc:
            hint = " (GitHub may be rate limiting; try again later)" if exc.code in (403, 429) else ""
            raise ts.ReadError("unavailable", f"GitHub answered {exc.code} for {urllib.parse.urlsplit(url).path}{hint}.") from exc
        except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as exc:
            raise ts.ReadError("unavailable", f"Could not reach GitHub: {type(exc).__name__}: {str(exc)[:120]}") from exc
        ts.require(len(body) <= cap, "unavailable", f"GitHub sent more than the {cap} bytes allowed for {url.rsplit('/', 1)[-1]}.")
        return body

    def releases(self) -> list[dict]:
        url = f"{API}/repos/{self.repo}/releases?per_page={int(self.limit)}"
        try:
            listing = json.loads(self._get(url, LISTING_LIMIT, accept="application/vnd.github+json").decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ts.ReadError("unavailable", "GitHub's release listing was not readable JSON.") from exc
        ts.require(isinstance(listing, list), "unavailable", "GitHub's release listing has an unexpected shape.")
        found = []
        self.rejected = []
        for row in listing:
            if not isinstance(row, dict) or row.get("draft") or not isinstance(row.get("tag_name"), str):
                continue
            assets = {item.get("name"): item for item in row.get("assets") or [] if isinstance(item, dict)}
            link = (assets.get(MANIFEST_NAME) or {}).get("browser_download_url")
            if not _allowed_url(link):
                continue  # a release without a manifest is not an EUTONOS release
            data = self._get(link, MANIFEST_LIMIT)
            signature_link = (assets.get(sig.SIGNATURE_NAME) or {}).get("browser_download_url")
            signature = self._get(signature_link, SIGNATURE_LIMIT) if _allowed_url(signature_link) else None
            try:
                trust = sig.check_release(data, signature, keys=self.keys)  # the network source has no override
            except ts.ReadError as exc:
                self.rejected.append({"id": row["tag_name"], "reason": exc.code, "detail": str(exc)})
                continue
            try:
                manifest = validate_manifest(json.loads(data.decode("utf-8")))
            except ts.ReadError as exc:
                if exc.code == "unavailable":
                    raise  # the network failed; a damaged release alone is skipped, never trusted
                continue
            except ValueError:
                continue
            wheel = assets.get(manifest["wheel"]) or {}
            found.append({"id": row["tag_name"], "manifest": manifest, "published": row.get("published_at"), "trust": trust,
                          "wheel_url": wheel.get("browser_download_url"), "wheel_size": wheel.get("size"),
                          "manifest_bytes": data, "signature_bytes": signature})
        return found

    def fetch_wheel(self, release: dict, destination: Path) -> Path:
        manifest = release["manifest"]
        url = release.get("wheel_url")
        ts.require(_allowed_url(url), "missing", f"The release has no downloadable {manifest['wheel']}.")
        declared = (manifest.get("files") or {}).get(manifest["wheel"], {}).get("size", release.get("wheel_size"))
        cap = declared if isinstance(declared, int) and not isinstance(declared, bool) and 0 <= declared <= WHEEL_LIMIT else WHEEL_LIMIT
        data = self._get(url, cap)
        digest = hashlib.sha256(data).hexdigest()
        ts.require(digest == manifest["sha256"], "sha256-mismatch",
                   f"The downloaded {manifest['wheel']} does not match the release manifest "
                   f"(expected {manifest['sha256'][:12]}, got {digest[:12]}).")
        destination = Path(destination)
        destination.mkdir(parents=True, exist_ok=True)
        target = destination / manifest["wheel"]
        target.write_bytes(data)
        return target


def current_interpreter() -> str:
    """The tag of the Python running this code, in the form a manifest's ``interpreter`` uses (``cp314-win_amd64``)."""
    platform = sysconfig.get_platform().replace("-", "_").replace(".", "_")
    return f"cp{sys.version_info.major}{sys.version_info.minor}-{platform}"


def interpreter_matches(wanted: str, current: str) -> bool:
    """Can a release built for interpreter `wanted` run on `current`? The CPython version must be the same; the platform
    part is compared only on Windows, where the kits' locked wheels are platform-specific."""
    want_python, _, want_platform = str(wanted).partition("-")
    have_python, _, have_platform = str(current).partition("-")
    if want_python != have_python:
        return False
    return not (want_platform.startswith("win") and have_platform.startswith("win")) or want_platform == have_platform


def open_source(spec=None, repo=None, allow_unsigned=False):
    """A source from a command-line value: a folder path (local) or nothing (the public GitHub releases repository,
    `repo` or the default)."""
    if spec is None or spec == "github":
        ts.require(not allow_unsigned, "invalid", "--allow-unsigned only applies to a release folder; the GitHub "
                                                   "source always requires a signature.")
        return GitHubSource(repo)
    if isinstance(spec, (str, Path)):
        return FolderSource(spec, allow_unsigned=allow_unsigned)
    return spec
