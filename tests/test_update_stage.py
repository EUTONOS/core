"""The update staging folder: a verified release is downloaded into a scratch folder, checked again and renamed into
``updates/staged/<artifact_id>/``; nothing unverified, partial or tampered is ever left looking complete. A throwaway
key signs the test releases and no network is used."""
import os
from pathlib import Path
import unittest
import unittest.mock

import test_installer as ti
from eutonos import release_signing as rsg
from eutonos import release_source as rs
from eutonos import tokenslang as ts
from eutonos import update_stage

SEED = os.urandom(32)  # made for this run; no real key is ever used or stored
PUB = rsg.public_key(SEED).hex()


def sign_release(folder, name):
    """Add the signature file beside a release made by ``ti.make_release`` (signed over the manifest's exact bytes)."""
    where = Path(folder) / name
    data = (where / rs.MANIFEST_NAME).read_bytes()
    (where / rsg.SIGNATURE_NAME).write_text(rsg.signature_text(rsg.sign(SEED, data)), encoding="ascii")


def signed_source(releases, **extra):
    return rs.FolderSource(releases, allow_unsigned=False, keys=(PUB,), **extra)


class StageTests(ti.UpdateBase):
    def release(self, name="r2", version="1.3.0", body="two", signed=True):
        manifest = ti.make_release(self.releases, name, version=version, body=body)
        if signed:
            sign_release(self.releases, name)
        return manifest

    def newest(self, source):
        return rs.newest(source.releases(), "dev")

    def test_a_verified_release_is_staged_as_a_release_folder_and_installs_from_there(self):
        manifest = self.release()
        source = signed_source(self.releases)
        result = update_stage.stage(source, self.newest(source), self.h)
        self.assertEqual(result["outcome"], "staged", result)
        folder = update_stage.staged_root(self.h) / manifest["artifact_id"]
        self.assertEqual(sorted(path.name for path in folder.iterdir()),
                         sorted([manifest["wheel"], rs.MANIFEST_NAME, rsg.SIGNATURE_NAME]))
        self.assertTrue(update_stage.staged_ready(self.h, manifest["artifact_id"], keys=(PUB,)))
        (item,) = update_stage.releases(self.h, keys=(PUB,))
        self.assertEqual((item["trust"], item["manifest"]["version"]), ("verified", "1.3.0"))
        self.assertEqual(list(update_stage.updates_root(self.h).glob(update_stage.SCRATCH_PREFIX + "*")), [])
        again = update_stage.stage(source, self.newest(source), self.h)  # nothing is downloaded twice
        self.assertEqual((again["outcome"], again.get("already")), ("staged", True))

    def test_an_unsigned_or_unverified_release_is_never_staged(self):
        self.release(signed=False)
        source = rs.FolderSource(self.releases, allow_unsigned=True)  # the developer override marks it unsigned-allowed
        release = self.newest(source)
        self.assertEqual(release["trust"], "unsigned-allowed")
        result = update_stage.stage(source, release, self.h)
        self.assertEqual((result["outcome"], result["reason"]), ("refused", "not-verified"))
        self.assertFalse(update_stage.staged_root(self.h).exists() and any(update_stage.staged_root(self.h).iterdir()))

    def test_a_failed_download_leaves_nothing_that_looks_complete(self):
        self.release()

        class Offline(rs.FolderSource):
            def fetch_wheel(self, release, destination):
                Path(destination).mkdir(parents=True, exist_ok=True)
                (Path(destination) / release["manifest"]["wheel"]).write_bytes(b"half")  # a partial file, then the failure
                raise ts.ReadError("unavailable", "The connection dropped.")

        source = Offline(self.releases, allow_unsigned=False, keys=(PUB,))
        result = update_stage.stage(source, self.newest(source), self.h)
        self.assertEqual((result["outcome"], result["reason"]), ("unavailable", "unavailable"))
        self.assertEqual(update_stage.releases(self.h, keys=(PUB,)), [])
        self.assertEqual(list(update_stage.updates_root(self.h).glob(update_stage.SCRATCH_PREFIX + "*")), [])
        staged = update_stage.staged_root(self.h)
        self.assertFalse(staged.exists() and any(staged.iterdir()))

    def test_a_download_that_does_not_match_the_manifest_is_refused(self):
        self.release()

        class Swapped(rs.FolderSource):
            def fetch_wheel(self, release, destination):
                Path(destination).mkdir(parents=True, exist_ok=True)
                target = Path(destination) / release["manifest"]["wheel"]
                target.write_bytes(b"not the wheel")
                return target

        source = Swapped(self.releases, allow_unsigned=False, keys=(PUB,))
        result = update_stage.stage(source, self.newest(source), self.h)
        self.assertEqual((result["outcome"], result["reason"]), ("refused", "sha256-mismatch"))
        self.assertEqual(update_stage.releases(self.h, keys=(PUB,)), [])

    def test_only_the_newest_staged_release_is_kept(self):
        first = self.release("r2", "1.3.0", "two")
        second = self.release("r3", "1.4.0", "three")
        source = signed_source(self.releases)
        old = [item for item in source.releases() if item["manifest"]["version"] == "1.3.0"][0]
        new = [item for item in source.releases() if item["manifest"]["version"] == "1.4.0"][0]
        self.assertEqual(update_stage.stage(source, old, self.h)["outcome"], "staged")
        self.assertEqual(update_stage.stage(source, new, self.h)["outcome"], "staged")
        names = sorted(path.name for path in update_stage.staged_root(self.h).iterdir())
        self.assertEqual(names, [second["artifact_id"]])
        self.assertFalse(update_stage.staged_ready(self.h, first["artifact_id"], keys=(PUB,)))
        self.assertEqual(update_stage.prune(self.h, keep=None), [second["artifact_id"]])
        self.assertEqual(list(update_stage.staged_root(self.h).iterdir()), [])

    def test_a_staged_copy_changed_afterwards_no_longer_verifies(self):
        manifest = self.release()
        source = signed_source(self.releases)
        update_stage.stage(source, self.newest(source), self.h)
        target = update_stage.staged_root(self.h) / manifest["artifact_id"] / rs.MANIFEST_NAME
        target.write_bytes(target.read_bytes().replace(b"1.3.0", b"9.9.9"))
        self.assertFalse(update_stage.staged_ready(self.h, manifest["artifact_id"], keys=(PUB,)))
        self.assertEqual(update_stage.releases(self.h, keys=(PUB,)), [])

    def test_the_staged_folder_is_refused_without_a_pinned_key(self):
        manifest = self.release()
        source = signed_source(self.releases)
        update_stage.stage(source, self.newest(source), self.h)
        # no keys given and PINNED_KEYS is empty: nothing in the folder can be trusted
        with unittest.mock.patch.object(rsg, "PINNED_KEYS", ()):
            self.assertFalse(update_stage.staged_ready(self.h, manifest["artifact_id"]))

    def test_an_update_installs_from_the_staged_folder_and_checks_the_signature_again(self):
        self.first()
        manifest = self.release()
        source = signed_source(self.releases)
        update_stage.stage(source, self.newest(source), self.h)
        # tampered after staging: the install refuses it
        staged = update_stage.staged_root(self.h) / manifest["artifact_id"]
        wheel = staged / manifest["wheel"]
        good = wheel.read_bytes()
        wheel.write_bytes(good + b"x")
        before = ti.settled(self.h)
        result = self.update(source=update_stage.staged_source(self.h, keys=(PUB,)), allow_unsigned=False)
        self.assertEqual(result["outcome"], "refused", result)
        self.assertEqual(ti.settled(self.h), before)
        wheel.write_bytes(good)
        result = self.update(source=update_stage.staged_source(self.h, keys=(PUB,)), allow_unsigned=False)
        self.assertEqual((result["outcome"], result["version"]), ("updated", "1.3.0"), result)


if __name__ == "__main__":
    unittest.main()
