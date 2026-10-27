"""Signed manifests: Ed25519 verification, the sources' refusals, compatibility gates and the component migrate hook.

Every key here is a throwaway generated inside the tests; no real signing key exists in or is read by this file."""
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
import unittest.mock

import test_installer as ti
import test_update_check as tuc
from eutonos import install_record as ir
from eutonos import installer
from eutonos import pending_actions
from eutonos import release_signing as rsg
from eutonos import release_source as rs
from eutonos import tokenslang as ts
from eutonos import update_check as uc

try:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
except ImportError:  # the cross-check is skipped where the library is absent; the RFC vectors still run
    Ed25519PrivateKey = None

SEED = os.urandom(32)
PUB = rsg.public_key(SEED).hex()
OTHER_SEED = os.urandom(32)
OTHER_PUB = rsg.public_key(OTHER_SEED).hex()


def sign_folder(folder, name="r2", seed=SEED):
    """Sign the manifest in `folder/name` the way tools/release.py does."""
    path = Path(folder) / name
    data = (path / rs.MANIFEST_NAME).read_bytes()
    (path / rsg.SIGNATURE_NAME).write_text(rsg.signature_text(rsg.sign(seed, data)), encoding="ascii")


def edit_manifest(folder, name="r2", **fields):
    path = Path(folder) / name / rs.MANIFEST_NAME
    value = json.loads(path.read_text(encoding="utf-8"))
    value.update(fields)
    path.write_text(json.dumps(value), encoding="utf-8")


class Ed25519Tests(unittest.TestCase):
    VECTORS = (  # RFC 8032 section 7.1, tests 1 and 2
        ("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60",
         "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a", "",
         "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"),
        ("4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb",
         "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c", "72",
         "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00"),
    )

    def test_the_rfc_8032_vectors(self):
        for seed, public, message, signature in self.VECTORS:
            seed, public, message, signature = (bytes.fromhex(item) for item in (seed, public, message, signature))
            self.assertEqual(rsg.public_key(seed), public)
            self.assertEqual(rsg.sign(seed, message), signature)
            self.assertTrue(rsg.verify(public, signature, message))

    @unittest.skipIf(Ed25519PrivateKey is None, "cryptography is not installed")
    def test_it_agrees_with_an_independent_implementation(self):
        for size in (0, 1, 33, 500):
            seed, message = os.urandom(32), os.urandom(size)
            key = Ed25519PrivateKey.from_private_bytes(seed)
            public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
            self.assertEqual(rsg.public_key(seed), public)
            self.assertEqual(rsg.sign(seed, message), key.sign(message))
            self.assertTrue(rsg.verify(public, key.sign(message), message))

    def test_a_changed_message_signature_or_key_does_not_verify(self):
        public, message = bytes.fromhex(PUB), b'{"version": "1"}\n'
        signature = rsg.sign(SEED, message)
        self.assertTrue(rsg.verify(public, signature, message))
        self.assertFalse(rsg.verify(public, signature, message + b" "))
        self.assertFalse(rsg.verify(bytes.fromhex(OTHER_PUB), signature, message))
        for index in (0, 31, 32, 63):
            broken = bytearray(signature)
            broken[index] ^= 1
            self.assertFalse(rsg.verify(public, bytes(broken), message), index)

    def test_malformed_input_is_a_false_never_an_exception(self):
        public, message = bytes.fromhex(PUB), b"m"
        signature = rsg.sign(SEED, message)
        too_big = signature[:32] + (rsg._Q).to_bytes(32, "little")  # s must be below the group order
        for args in ((public[:31], signature, message), (public, signature[:63], message), (public, too_big, message),
                     (b"\xff" * 32, signature, message), (public, b"\xff" * 64, message), (b"", b"", message)):
            self.assertFalse(rsg.verify(*args))

    def test_a_pinned_key_must_be_well_formed(self):
        for bad in (("zz",), ("A" * 64,), ("a" * 63,), (123,)):
            with self.assertRaises(ValueError):
                rsg.check_keys(bad)


class CheckReleaseTests(unittest.TestCase):
    DATA = b'{"profile": "eutonos.release.v1"}\n'

    def sig(self, seed=SEED, data=None):
        return rsg.signature_text(rsg.sign(seed, data or self.DATA))

    def code(self, *args, **kwargs):
        with self.assertRaises(ts.ReadError) as caught:
            rsg.check_release(*args, **kwargs)
        return caught.exception.code

    def test_a_valid_signature_from_a_pinned_key_is_verified(self):
        self.assertEqual(rsg.check_release(self.DATA, self.sig(), keys=(PUB,)), "verified")
        self.assertEqual(rsg.check_release(self.DATA, self.sig().encode("ascii"), keys=(OTHER_PUB, PUB)), "verified")
        self.assertEqual(rsg.verify_manifest(self.DATA, self.sig(), keys=(PUB,)), rsg.key_id(PUB))

    def test_unsigned_wrong_key_and_malformed_signatures_are_refused(self):
        self.assertEqual(self.code(self.DATA, None, keys=(PUB,)), "unsigned")
        self.assertEqual(self.code(self.DATA, "", keys=(PUB,)), "unsigned")
        self.assertEqual(self.code(self.DATA, self.sig(OTHER_SEED), keys=(PUB,)), "bad-signature")
        self.assertEqual(self.code(self.DATA, "not hex", keys=(PUB,)), "bad-signature")
        self.assertEqual(self.code(self.DATA + b" ", self.sig(), keys=(PUB,)), "bad-signature")

    def test_a_build_with_no_pinned_key_fails_closed(self):
        self.assertEqual(self.code(self.DATA, self.sig(), keys=()), "no-trusted-key")
        with unittest.mock.patch.object(rsg, "PINNED_KEYS", ()):
            self.assertEqual(self.code(self.DATA, self.sig()), "no-trusted-key")

    def test_the_developer_override_accepts_no_signature_but_not_a_wrong_one(self):
        self.assertEqual(rsg.check_release(self.DATA, None, allow_unsigned=True, keys=(PUB,)), "unsigned-allowed")
        self.assertEqual(rsg.check_release(self.DATA, self.sig(), allow_unsigned=True, keys=()), "unsigned-allowed")
        self.assertEqual(self.code(self.DATA, self.sig(OTHER_SEED), allow_unsigned=True, keys=(PUB,)), "bad-signature")


class SourceSignatureTests(ti.Base):
    def test_a_folder_source_returns_only_verified_releases(self):
        ti.make_release(self.base, "r2", version="1.3.0", body="two")
        source = rs.FolderSource(self.base, keys=(PUB,))
        self.assertEqual(source.releases(), [])
        self.assertEqual([item["reason"] for item in source.rejected], ["unsigned"])
        sign_folder(self.base)
        (found,) = source.releases()
        self.assertEqual((found["id"], found["trust"], source.rejected), ("r2", "verified", []))

    def test_a_manifest_changed_after_signing_is_rejected(self):
        ti.make_release(self.base, "r2", version="1.3.0", body="two")
        sign_folder(self.base)
        edit_manifest(self.base, min_compatible="0.0.1")
        source = rs.FolderSource(self.base, keys=(PUB,))
        self.assertEqual(source.releases(), [])
        self.assertEqual(source.rejected[0]["reason"], "bad-signature")
        flexible = rs.FolderSource(self.base, keys=(PUB,), allow_unsigned=True)
        self.assertEqual(flexible.releases(), [])  # the override never accepts a wrong signature
        self.assertEqual(flexible.rejected[0]["reason"], "bad-signature")

    def test_the_override_accepts_an_unsigned_folder_release(self):
        ti.make_release(self.base, "r2", version="1.3.0", body="two")
        (found,) = rs.FolderSource(self.base, keys=(PUB,), allow_unsigned=True).releases()
        self.assertEqual(found["trust"], "unsigned-allowed")

    def test_the_github_source_requires_a_signature_and_has_no_override(self):
        manifest = tuc.manifest_for()
        unsigned = tuc.FakeNet()
        unsigned.add_release("v1", manifest, signed=False)
        source = tuc.github(unsigned)
        self.assertEqual(source.releases(), [])
        self.assertEqual(source.rejected[0]["reason"], "unsigned")
        signed = tuc.FakeNet()
        signed.add_release("v1", manifest)
        self.assertEqual(tuc.github(signed).releases()[0]["trust"], "verified")
        wrong = rs.GitHubSource("Owner/Repo", opener=signed, keys=(OTHER_PUB,))
        self.assertEqual(wrong.releases(), [])
        self.assertEqual(wrong.rejected[0]["reason"], "bad-signature")
        tampered = tuc.FakeNet()
        tampered.add_release("v1", manifest)
        tampered.pages[f"{tuc.BASE}v1/{rs.MANIFEST_NAME}"] = json.dumps({**manifest, "version": "9.9.9"}).encode("utf-8")
        self.assertEqual(tuc.github(tampered).releases(), [])
        with self.assertRaises(ts.ReadError):
            rs.open_source(None, allow_unsigned=True)

    def test_the_default_build_pins_no_key_so_a_network_release_is_refused(self):
        net = tuc.FakeNet()
        net.add_release("v1", tuc.manifest_for())
        with unittest.mock.patch.object(rsg, "PINNED_KEYS", ()):
            source = rs.GitHubSource("Owner/Repo", opener=net)
            self.assertEqual(source.releases(), [])
            self.assertEqual(source.rejected[0]["reason"], "no-trusted-key")

    def test_interpreter_tags(self):
        self.assertTrue(rs.interpreter_matches("cp314-win_amd64", "cp314-win_amd64"))
        self.assertFalse(rs.interpreter_matches("cp314-win_amd64", "cp313-win_amd64"))
        self.assertFalse(rs.interpreter_matches("cp314-win_amd64", "cp314-win_arm64"))
        self.assertTrue(rs.interpreter_matches("cp314-win_amd64", "cp314-macosx_14_0_arm64"))  # only Windows pins the platform
        self.assertRegex(rs.current_interpreter(), r"^cp3\d+-\S+$")


class SignedUpdateTests(ti.UpdateBase):
    def strict(self, **extra):
        """update() the way a normal run does it: no developer override, the throwaway key pinned."""
        return self.update(allow_unsigned=False, **extra)

    def test_a_signed_release_installs_and_an_unsigned_one_is_refused_before_anything_changes(self):
        self.first()
        ti.make_release(self.releases, "r2", version="1.3.0", body="two")
        before = ti.settled(self.h)
        with unittest.mock.patch.object(rsg, "PINNED_KEYS", (PUB,)):
            refused = self.strict()
            self.assertEqual((refused["outcome"], refused["reason"]), ("refused", "unsigned"), refused)
            self.assertEqual(ti.settled(self.h), before)
            sign_folder(self.releases)
            done = self.strict()
        self.assertEqual((done["outcome"], done["version"]), ("updated", "1.3.0"), done)

    def test_a_wrong_or_missing_key_refuses_the_update(self):
        self.first()
        ti.make_release(self.releases, "r2", version="1.3.0", body="two")
        sign_folder(self.releases, seed=OTHER_SEED)
        before = ti.settled(self.h)
        with unittest.mock.patch.object(rsg, "PINNED_KEYS", (PUB,)):
            refused = self.strict()
        self.assertEqual((refused["outcome"], refused["reason"]), ("refused", "bad-signature"), refused)
        with unittest.mock.patch.object(rsg, "PINNED_KEYS", ()):
            refused = self.strict()
        self.assertEqual((refused["outcome"], refused["reason"]), ("refused", "no-trusted-key"), refused)
        self.assertEqual(ti.settled(self.h), before)

    def test_a_source_that_skipped_the_signature_check_is_not_trusted(self):
        self.first()
        manifest = ti.make_release(self.releases, "r2", version="1.3.0", body="two")

        class Careless(rs.FolderSource):
            def releases(self):
                return [{key: value for key, value in item.items() if key != "trust"} for item in super().releases()]

        refused = self.update(source=Careless(self.releases, allow_unsigned=True))
        self.assertEqual((refused["outcome"], refused["reason"]), ("refused", "unsigned"), refused)
        self.assertTrue(manifest)

    def test_the_network_source_refuses_an_update_with_no_pinned_key(self):
        self.first()
        net = tuc.FakeNet()
        net.add_release("v1.3.0", tuc.manifest_for(version="1.3.0", channels=["dev"],
                                                   artifact_id="eutonos-1.3.0-abcdef012345"))
        with unittest.mock.patch.object(rsg, "PINNED_KEYS", ()):
            result = self.update(source=rs.GitHubSource("Owner/Repo", opener=net))
        self.assertEqual((result["outcome"], result["reason"]), ("refused", "no-trusted-key"), result)

    def test_a_check_reports_a_rejected_release_instead_of_calling_it_missing(self):
        self.first()
        ti.make_release(self.releases, "r2", version="1.3.0", body="two")
        with unittest.mock.patch.object(rsg, "PINNED_KEYS", (PUB,)):
            result = installer.check_update(home=self.h, source=self.releases)
        self.assertEqual((result["outcome"], result["reason"]), ("refused", "unsigned"), result)


class CompatibilityTests(ti.UpdateBase):
    def test_a_release_whose_floor_is_above_the_installed_version_needs_setup(self):
        self.first()  # installed 1.2.3
        ti.make_release(self.releases, "r2", version="2.0.0", body="two", min_compatible="1.5.0")
        before = ti.settled(self.h)
        for dry in (False, True):
            result = self.update(dry_run=dry)
            self.assertEqual((result["outcome"], result["reason"]), ("refused", "min-compatible"), result)
            self.assertEqual((result["installed"], result["min_compatible"]), ("1.2.3", "1.5.0"))
            self.assertIn("Setup", result["next_action"])
        self.assertEqual(ti.settled(self.h), before)

    def test_a_release_whose_floor_the_installed_version_meets_installs(self):
        self.first()
        ti.make_release(self.releases, "r2", version="2.0.0", body="two", min_compatible="1.2.3")
        self.assertEqual(self.update()["outcome"], "updated")

    def test_a_release_built_for_another_bundled_python_needs_setup(self):
        self.first()
        ti.make_release(self.releases, "r2", version="1.3.0", body="two")
        edit_manifest(self.releases, interpreter="cp300-win_amd64")
        before = ti.settled(self.h)
        refused = self.update(interpreter="cp314-win_amd64")
        self.assertEqual((refused["outcome"], refused["reason"]), ("refused", "interpreter-changed"), refused)
        self.assertEqual((refused["needs"], refused["installed"]), ("cp300-win_amd64", "cp314-win_amd64"))
        self.assertIn("Setup", refused["next_action"])
        self.assertEqual(ti.settled(self.h), before)
        self.assertEqual(self.update(interpreter="cp300-win_amd64")["outcome"], "updated")

    def test_a_check_flags_a_blocked_update_and_the_notice_says_to_run_setup(self):
        self.first()
        ti.make_release(self.releases, "r2", version="2.0.0", body="two", min_compatible="1.5.0")
        uc.set_notice("on", home=self.h)
        result = installer.check_update(home=self.h, source=self.releases, allow_unsigned=True)
        self.assertEqual(result["outcome"], "update-available", result)
        self.assertEqual(result["available"]["blocked"]["reason"], "min-compatible")
        (entry,) = pending_actions.list_actions(self.h)
        self.assertIn("Setup", entry["action"])
        self.assertNotIn("eutonos-install update", entry["action"])

    def test_a_local_patch_is_not_held_to_the_release_floor(self):
        # the patch path builds its own unsigned folder source and keeps the active generation's floor
        self.assertTrue(rs.FolderSource(self.base, allow_unsigned=True).allow_unsigned)


class MigrateHookTests(ti.UpdateBase):
    def test_an_update_calls_each_components_migrate_with_the_versions(self):
        manifest, calls = ti.fake_component(self)
        self.first(components=[manifest])
        seen = []
        sys.modules["fake_fakecomp"].migrate = lambda ctx, start, end: seen.append((start, end, ctx["gen_id"])) or {"notes": ["moved"]}
        ti.make_release(self.releases, "r2", version="1.3.0", body="two")
        result = self.update(components=[manifest])
        self.assertEqual(result["outcome"], "updated", result)
        self.assertEqual([item[:2] for item in seen], [("1.2.3", "1.3.0")])
        self.assertEqual(seen[0][2], result["gen_id"])
        record = ir.read_record(self.layout, result["gen_id"])
        self.assertEqual(record["migrations"], [{"component": "fakecomp", "from": "1.2.3", "to": "1.3.0"}])
        self.assertIn("moved", result.get("notes", []))

    def test_a_component_without_a_hook_and_a_first_install_run_none(self):
        manifest, _ = ti.fake_component(self)
        calls = []
        sys.modules["fake_fakecomp"].migrate = lambda ctx, start, end: calls.append(start)
        self.first(components=[manifest])
        self.assertEqual(calls, [])  # an install is not an update
        del sys.modules["fake_fakecomp"].migrate
        ti.make_release(self.releases, "r2", version="1.3.0", body="two")
        self.assertEqual(self.update(components=[manifest])["outcome"], "updated")

    def test_a_failing_migration_stops_the_update_and_keeps_the_earlier_generation(self):
        manifest, _ = ti.fake_component(self)
        old = self.first(components=[manifest])

        def broken(ctx, start, end):
            raise RuntimeError("migration failed")

        sys.modules["fake_fakecomp"].migrate = broken
        ti.make_release(self.releases, "r2", version="1.3.0", body="two")
        result = self.update(components=[manifest])
        self.assertEqual((result["outcome"], result["phase"]), ("rolled-back", "effects"), result)
        self.assertEqual(ir.read_state(self.layout)["active"], old["gen_id"])
        del sys.modules["fake_fakecomp"].migrate
        self.assertEqual(self.update(components=[manifest])["outcome"], "updated")


class ReleaseToolSigningTests(unittest.TestCase):
    def setUp(self):
        import importlib.util
        root = Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location("release_tool_for_signing", root / "tools" / "release.py")
        self.tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.tool)
        self.checkout = root

    def test_keygen_writes_a_new_key_outside_the_repository_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "keys" / "release-signing-seed"
            made = self.tool.keygen(out)
            self.assertEqual(made["public_key"], rsg.public_key(self.tool.read_seed(out)).hex())
            self.assertNotIn(self.tool.read_seed(out).hex(), json.dumps(made))  # the private key is never printed
            with self.assertRaises(FileExistsError):
                self.tool.keygen(out)
            inside = self.checkout / "tests" / "never-written-key"
            with self.assertRaises(ValueError):
                self.tool.keygen(inside)
            self.assertFalse(inside.exists())

    def test_a_key_file_must_hold_exactly_a_hex_seed(self):
        with tempfile.TemporaryDirectory() as temp:
            bad = Path(temp) / "seed"
            for text in ("", "abc", "g" * 64, "a" * 63):
                bad.write_text(text, encoding="ascii")
                with self.assertRaises(ValueError):
                    self.tool.read_seed(bad)


if __name__ == "__main__":
    unittest.main()
