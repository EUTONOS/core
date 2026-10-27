"""Read Git objects, never working-tree bodies, filters, hooks or remote sources."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import subprocess
from .hidden_process import no_window
from threading import Thread
import time
import unicodedata

from .tokenslang import ReadError, require, string


def safe_path(value: str) -> str:
    string(value)
    parts = value.split("/")
    require(len(parts) <= 32, "unsafe-path", "Path depth exceeds the read-adapter bound.")
    for part in parts:
        require(part not in {"", ".", ".."} and part.casefold() != ".git"
                and not part.endswith((".", " "))
                and not re.search(r'[\x00-\x1f\x7f\\:<>"|?*]', part)
                and not re.fullmatch(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", part),
                "unsafe-path", "Path is not a supported portable repository-relative locator.")
    return value


class GitView:
    """One trusted local repository, pinned once to a commit; no live overlay.

    This is bounded source retrieval, not a sandbox for a hostile Git installation
    or object database. Git is an explicit host prerequisite.
    """

    def __init__(self, root: str | Path, revision: str):
        self.root = Path(root).resolve(strict=True)
        self.reset_limits()
        self.env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
        self.env.update(GIT_OPTIONAL_LOCKS="0", GIT_NO_REPLACE_OBJECTS="1",
                        GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                        GIT_TERMINAL_PROMPT="0", GIT_NO_LAZY_FETCH="1")
        partial = self._git("config", "--local", "--get-regexp",
                            r"^(extensions\.partialclone|remote\..*\.promisor)$", allow_missing=True)
        require(not partial, "unsupported", "Partial/promisor repositories are unsupported; reads must not lazy-fetch.")
        top = self._git("rev-parse", "--show-toplevel").decode("utf-8").strip()
        require(Path(top).resolve() == self.root, "unsafe-root", "Select the repository root explicitly.")
        self.requested_revision = string(revision)
        require(not revision.startswith("-") and "\x00" not in revision,
                "invalid", "Invalid revision selector.")
        self.commit = self._git("rev-parse", "--verify", "--end-of-options",
                                revision + "^{commit}").decode("ascii").strip()
        require(bool(re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", self.commit)),
                "unavailable", "Git did not return an exact commit.")
        self.tree = self._git("rev-parse", self.commit + "^{tree}").decode("ascii").strip()
        self.excluded_roots: tuple[str, ...] = ()

    def reset_limits(self) -> None:
        self._commands = 0
        self._output_bytes = 0
        self._deadline = time.monotonic() + 30

    def _git(self, *args: str, limit: int = 1048576, allow_missing: bool = False) -> bytes:
        self._commands += 1
        require(self._commands <= 512 and time.monotonic() < self._deadline,
                "overflow", "Total Git operation/time budget exhausted.")
        limit = min(limit, 4194304 - self._output_bytes)
        require(limit > 0, "overflow", "Total Git output budget exhausted.")
        command = ["git", "-c", "safe.directory=" + str(self.root),
                   "-c", "core.fsmonitor=false", "-C", str(self.root), *args]
        try:
            with subprocess.Popen(command, env=self.env, stdin=subprocess.DEVNULL,
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, **no_window()) as process:
                output = []
                reader = Thread(target=lambda: output.append(process.stdout.read(limit + 1)), daemon=True)
                reader.start()
                reader.join(max(0.01, min(15, self._deadline - time.monotonic())))
                if reader.is_alive() or (output and len(output[0]) > limit):
                    process.kill()
                    process.wait()
                    reader.join()
                    raise ReadError("overflow" if output and len(output[0]) > limit else "unavailable",
                                    "Git source exceeds the adapter bound or timeout.")
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                    raise
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ReadError("unavailable", "The local Git read failed or timed out.") from exc
        require(process.returncode == 0 or (allow_missing and process.returncode == 1),
                "unavailable", "Git could not obtain the selected source.")
        self._output_bytes += len(output[0])
        return output[0]

    def _git_stream(self, *args: str):
        """Yield an unbounded Git result without weakening exact-command checks.

        This path is reserved for repository inventories and already-sized blob
        reads.  Consumers must fully drain it so the command status is checked.
        """
        self._commands += 1
        require(self._commands <= 512, "overflow", "Total Git operation budget exhausted.")
        command = ["git", "-c", "safe.directory=" + str(self.root),
                   "-c", "core.fsmonitor=false", "-C", str(self.root), *args]
        try:
            with subprocess.Popen(command, env=self.env, stdin=subprocess.DEVNULL,
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, **no_window()) as process:
                assert process.stdout is not None
                drained = False
                try:
                    while chunk := process.stdout.read(65536):
                        yield chunk
                    drained = True
                finally:
                    if not drained and process.poll() is None:
                        process.kill()
                    process.wait()
        except OSError as exc:
            raise ReadError("unavailable", "The local Git read failed.") from exc
        require(process.returncode == 0, "unavailable", "Git could not obtain the selected source.")

    def iter_nul(self, *args: str):
        """Yield NUL-delimited Git records without retaining the full listing."""
        pending = b""
        for chunk in self._git_stream(*args):
            parts = (pending + chunk).split(b"\0")
            pending = parts.pop()
            yield from parts
        require(not pending, "conflict", "Git returned an incomplete NUL-delimited record.")

    def read(self, path: str) -> tuple[bytes, dict]:
        path = safe_path(path)
        require(not any(path == p or path.startswith(p + "/") for p in self.excluded_roots),
                "denied", "Excluded nested source root.")
        tree = self.tree
        parts = path.split("/")
        for index, part in enumerate(parts):
            entries = []
            for raw in self._git("ls-tree", "-z", tree).split(b"\0"):
                if not raw:
                    continue
                meta, name = raw.split(b"\t", 1)
                mode, kind, oid = meta.decode("ascii").split()
                try:
                    name = name.decode("utf-8")
                except UnicodeError as exc:
                    raise ReadError("unsupported", "Non-UTF-8 Git path in selected directory.") from exc
                entries.append((name, mode, kind, oid))
            key = unicodedata.normalize("NFC", part).casefold()
            matches = [e for e in entries if unicodedata.normalize("NFC", e[0]).casefold() == key]
            require(len(matches) <= 1, "ambiguous", "Case or Unicode path collision.")
            require(len(matches) == 1 and matches[0][0] == part, "missing", "Exact source path not found.")
            _, mode, kind, oid = matches[0]
            require(mode not in {"120000", "160000"}, "unsupported", "Links and submodules are unsupported.")
            if index < len(parts) - 1:
                require(kind == "tree", "missing", "Source parent is not a directory.")
                tree = oid
            else:
                require(kind == "blob" and mode in {"100644", "100755"},
                        "unsupported", "Expected a regular source blob.")
                size = int(self._git("cat-file", "-s", oid).strip())
                chunks = []
                received = 0
                for chunk in self._git_stream("cat-file", "blob", oid):
                    received += len(chunk)
                    require(received <= size, "conflict", "Source size changed during retrieval.")
                    chunks.append(chunk)
                data = b"".join(chunks)
                require(len(data) == size, "conflict", "Source size changed during retrieval.")
                return data, {"path": path, "git_blob": oid, "sha256": hashlib.sha256(data).hexdigest(),
                              "bytes": size, "source_commit": self.commit}
        raise ReadError("missing", "No source selected.")
