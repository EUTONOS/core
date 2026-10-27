"""The gh-free update check: GitHubSource over urllib (faked), `update --check` and the three update modes."""
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
import unittest.mock
import urllib.error

import test_installer as ti
from eutonos import install_record as ir
from eutonos import installer
from eutonos import pending_actions
from eutonos import release_signing as rsg
from eutonos import release_source as rs
from eutonos import tokenslang as ts
from eutonos import update_check as uc

BASE = "https://github.com/Owner/Repo/releases/download/"
SEED = os.urandom(32)  # a throwaway key made for this run; no real key is ever used or stored
PUB = rsg.public_key(SEED).hex()


def github(net, **extra):
    """A GitHubSource over the fake network that trusts only this run's throwaway key."""
    return rs.GitHubSource("Owner/Repo", opener=net, keys=(PUB,), **extra)


class Response(io.BytesIO):
    pass


class FakeNet:
    """A stand-in for ``release_source.urlopen``: url -> bytes, every call recorded, anything else is a 404."""

    def __init__(self, pages=None):
        self.pages = dict(pages or {})
        self.calls = []

    def __call__(self, url, headers):
        self.calls.append((url, dict(headers)))
        body = self.pages.get(url)
        if body is None:
            raise urllib.error.HTTPError(url, 404, "not found", {}, None)
        if isinstance(body, Exception):
            raise body
        return Response(body)

    def add_release(self, tag, manifest, wheel=b"wheel", *, draft=False, with_manifest=True, signed=True):
        row = {"tag_name": tag, "published_at": "2026-01-01T00:00:00Z", "draft": draft, "assets": []}
        if with_manifest:
            data = json.dumps(manifest).encode("utf-8")
            row["assets"].append({"name": rs.MANIFEST_NAME, "browser_download_url": f"{BASE}{tag}/{rs.MANIFEST_NAME}"})
            self.pages[f"{BASE}{tag}/{rs.MANIFEST_NAME}"] = data
            if signed:
                name = rsg.SIGNATURE_NAME
                row["assets"].append({"name": name, "browser_download_url": f"{BASE}{tag}/{name}"})
                self.pages[f"{BASE}{tag}/{name}"] = rsg.signature_text(rsg.sign(SEED, data)).encode("ascii")
        row["assets"].append({"name": manifest["wheel"], "size": len(wheel),
                              "browser_download_url": f"{BASE}{tag}/{manifest['wheel']}"})
        self.pages[f"{BASE}{tag}/{manifest['wheel']}"] = wheel
        rows = self.rows
        rows.append(row)
        self.pages[f"{rs.API}/repos/Owner/Repo/releases?per_page={rs.LIST_LIMIT}"] = json.dumps(rows).encode("utf-8")
        return row

    rows = property(lambda self: self.__dict__.setdefault("_rows", []))


def manifest_for(wheel=b"wheel", **change):
    digest = hashlib.sha256(wheel).hexdigest()
    value = {"profile": rs.PROFILE, "version": "1.0.0", "artifact_id": "eutonos-1.0.0-abcdef012345",
             "wheel": "eutonos-1.0.0-py3-none-any.whl", "sha256": digest, "channels": ["stable"], "min_compatible": None}
    value.update(change)
    return value


class GitHubSourceTests(unittest.TestCase):
    def test_the_default_repository_is_the_public_releases_one(self):
        self.assertEqual(rs.DEFAULT_REPO, "EUTONOS/releases")
        with unittest.mock.patch.dict("os.environ"):
            os.environ.pop("EUTONOS_RELEASE_REPO", None)
            self.assertEqual(rs.GitHubSource().repo, "EUTONOS/releases")

    def test_only_owner_slash_name_reaches_a_url(self):
        for bad in ("Owner", "Owner/Repo/extra", "../x/y", "Owner/Re po", "Owner/Repo?x=1", "https://x/y"):
            with self.assertRaises(ts.ReadError, msg=bad):
                rs.GitHubSource(bad, opener=FakeNet())
        self.assertEqual(rs.GitHubSource("Owner/Repo.git-x", opener=FakeNet()).repo, "Owner/Repo.git-x")

    def test_releases_come_from_unauthenticated_https_gets_only(self):
        net = FakeNet()
        net.add_release("v1", manifest_for())
        net.add_release("v0", manifest_for(version="0.9.0", artifact_id="eutonos-0.9.0-abcdef012345"), draft=True)
        net.add_release("plain", manifest_for(), with_manifest=False)
        source = github(net)
        found = source.releases()
        self.assertEqual([item["id"] for item in found], ["v1"])  # drafts and releases without a manifest are skipped
        self.assertEqual(found[0]["published"], "2026-01-01T00:00:00Z")
        self.assertEqual(found[0]["manifest"]["version"], "1.0.0")
        self.assertTrue(net.calls[0][0].startswith("https://api.github.com/repos/Owner/Repo/releases"))
        for url, headers in net.calls:
            self.assertTrue(url.startswith("https://"), url)
            self.assertTrue(rs._allowed_url(url), url)
            self.assertFalse({key.lower() for key in headers} & {"authorization", "cookie"}, headers)

    def test_a_wheel_is_downloaded_and_checked_against_the_manifest(self):
        net = FakeNet()
        net.add_release("v1", manifest_for(wheel=b"real wheel bytes"), wheel=b"real wheel bytes")
        source = github(net)
        found = source.releases()
        with tempfile.TemporaryDirectory() as temp:
            target = source.fetch_wheel(found[0], Path(temp) / "dl")
            self.assertEqual(target.read_bytes(), b"real wheel bytes")

    def test_a_download_that_does_not_match_its_sha256_is_refused_and_not_kept(self):
        net = FakeNet()
        net.add_release("v1", manifest_for(wheel=b"expected"), wheel=b"tampered")
        source = github(net)
        found = source.releases()
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(ts.ReadError) as caught:
                source.fetch_wheel(found[0], Path(temp) / "dl")
            self.assertEqual(caught.exception.code, "sha256-mismatch")
            self.assertFalse((Path(temp) / "dl").exists() and any((Path(temp) / "dl").iterdir()))

    def test_bodies_over_their_size_cap_are_refused(self):
        net = FakeNet()
        manifest = manifest_for(wheel=b"abcd", files={"eutonos-1.0.0-py3-none-any.whl": {"sha256": hashlib.sha256(b"abcd").hexdigest(), "size": 4}})
        net.add_release("v1", manifest, wheel=b"abcd plus a lot more than declared")
        source = github(net)
        found = source.releases()
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(ts.ReadError) as caught:
                source.fetch_wheel(found[0], Path(temp))
            self.assertEqual(caught.exception.code, "unavailable")
        big = FakeNet()
        big.add_release("v1", manifest_for())
        big.pages[f"{BASE}v1/{rs.MANIFEST_NAME}"] = b"x" * (rs.MANIFEST_LIMIT + 1)
        with self.assertRaises(ts.ReadError) as caught:
            github(big).releases()
        self.assertEqual(caught.exception.code, "unavailable")

    def test_a_download_link_off_github_or_over_http_is_never_followed(self):
        for link in ("http://github.com/Owner/Repo/releases/download/v1/eutonos-release.json",
                     "https://evil.example/eutonos-release.json",
                     "https://github.com.evil.example/x/eutonos-release.json",
                     "https://user@github.com/x/eutonos-release.json"):
            net = FakeNet()
            row = net.add_release("v1", manifest_for())
            row["assets"][0]["browser_download_url"] = link
            net.pages[f"{rs.API}/repos/Owner/Repo/releases?per_page={rs.LIST_LIMIT}"] = json.dumps(net.rows).encode("utf-8")
            self.assertEqual(github(net).releases(), [], link)
            self.assertEqual([url for url, _ in net.calls if not url.startswith("https://api.github.com/")], [], link)
        net = FakeNet()
        row = net.add_release("v1", manifest_for())
        row["assets"][-1]["browser_download_url"] = "https://evil.example/w.whl"
        net.pages[f"{rs.API}/repos/Owner/Repo/releases?per_page={rs.LIST_LIMIT}"] = json.dumps(net.rows).encode("utf-8")
        source = github(net)
        with self.assertRaises(ts.ReadError) as caught:
            source.fetch_wheel(source.releases()[0], Path("."))
        self.assertEqual(caught.exception.code, "missing")

    def test_the_real_opener_refuses_other_hosts_and_redirects_off_github(self):
        with self.assertRaises(ts.ReadError):
            rs.urlopen("http://example.com/x")
        with self.assertRaises(ts.ReadError):
            rs.urlopen("https://example.com/x")
        redirects = rs._GitHubRedirects()
        with self.assertRaises(urllib.error.URLError):
            redirects.redirect_request(unittest.mock.Mock(), None, 302, "Found", {}, "https://evil.example/x")
        with self.assertRaises(urllib.error.URLError):
            redirects.redirect_request(unittest.mock.Mock(), None, 302, "Found", {}, "http://objects.githubusercontent.com/x")

    def test_network_and_http_failures_are_clear_unavailable_errors(self):
        for failure, text in ((OSError("no network"), "Could not reach GitHub"),
                              (urllib.error.HTTPError("u", 403, "rate limited", {}, None), "rate limiting"),
                              (urllib.error.HTTPError("u", 500, "boom", {}, None), "500")):
            net = FakeNet({f"{rs.API}/repos/Owner/Repo/releases?per_page={rs.LIST_LIMIT}": failure})
            with self.assertRaises(ts.ReadError) as caught:
                github(net).releases()
            self.assertEqual(caught.exception.code, "unavailable")
            self.assertIn(text, str(caught.exception))
        net = FakeNet({f"{rs.API}/repos/Owner/Repo/releases?per_page={rs.LIST_LIMIT}": b"<html>"})
        with self.assertRaises(ts.ReadError):
            github(net).releases()

    def test_a_damaged_manifest_skips_that_release_only(self):
        net = FakeNet()
        net.add_release("bad", manifest_for())
        net.pages[f"{BASE}bad/{rs.MANIFEST_NAME}"] = b"{nope"
        net.add_release("v2", manifest_for(version="2.0.0", artifact_id="eutonos-2.0.0-abcdef012345"))
        self.assertEqual([item["id"] for item in github(net).releases()], ["v2"])


def release_net(version="1.3.0", wheel=b"wheel", **change):
    net = FakeNet()
    digest = hashlib.sha256(wheel).hexdigest()
    net.add_release("v" + version, manifest_for(wheel=wheel, version=version, channels=["dev"],
                                                artifact_id=ir.artifact_id(version, digest), **change), wheel=wheel)
    return net


class UpdateCheckTests(ti.UpdateBase):
    def check(self, **extra):
        if isinstance(extra.get("source"), Path):
            extra.setdefault("allow_unsigned", True)  # the folder releases made by make_release carry no signature
        return installer.check_update(home=self.h, **extra)

    def test_a_check_reports_a_newer_release_and_downloads_no_wheel(self):
        self.first()
        manifest = ti.make_release(self.releases, "r2", version="1.3.0", body="two")
        uc.set_mode("manual", home=self.h)  # manual changes nothing beyond the check's own record
        before = {k: v for k, v in ti.settled(self.h).items() if "update-check" not in k}
        with unittest.mock.patch.object(rs.FolderSource, "fetch_wheel", side_effect=AssertionError("downloaded")):
            result = self.check(source=self.releases)
        self.assertEqual(result["outcome"], "update-available", result)
        self.assertEqual(result["available"]["artifact_id"], manifest["artifact_id"])
        self.assertEqual((result["available"]["version"], result["installed"]["version"]), ("1.3.0", "1.2.3"))
        self.assertEqual(result["notice"], "off")
        self.assertEqual({k: v for k, v in ti.settled(self.h).items() if "update-check" not in k}, before)

    def test_no_release_up_to_date_and_older_are_not_updates(self):
        self.first()
        self.assertEqual(self.check(source=self.releases)["outcome"], "no-release")
        ti.make_release(self.releases, "r0", version="0.9.0", body="old", channels=("dev",))
        self.assertEqual(self.check(source=self.releases)["outcome"], "up-to-date")  # older than installed
        self.assertEqual(self.update(source=self.releases)["outcome"], "refused")

    def test_a_check_on_an_uninstalled_home_and_an_unreachable_source_change_nothing(self):
        self.assertEqual(self.check(source=self.releases)["reason"], "not-installed")
        self.first()
        before = {k: v for k, v in ti.settled(self.h).items() if "update-check" not in k}

        def offline(url, headers):
            raise OSError("no network")

        result = self.check(source=rs.GitHubSource("Owner/Repo", opener=offline))
        self.assertEqual((result["outcome"], result["reason"]), ("unavailable", "unavailable"), result)
        self.assertEqual({k: v for k, v in ti.settled(self.h).items() if "update-check" not in k}, before)

    def test_the_default_source_is_the_saved_releases_repository_over_https(self):
        self.first()
        net = FakeNet()
        with unittest.mock.patch.object(rs, "urlopen", net), unittest.mock.patch.dict("os.environ"):
            os.environ.pop("EUTONOS_RELEASE_REPO", None)
            self.assertEqual(self.check()["outcome"], "unavailable")  # the fake net has no pages: a 404
            self.assertIn("/repos/EUTONOS/releases/releases", net.calls[0][0])
            uc.set_repo("Owner/Repo", home=self.h)
            net.calls.clear()
            self.check()
            self.assertIn("/repos/Owner/Repo/releases", net.calls[0][0])

    def test_update_downloads_over_https_verifies_and_installs(self):
        self.first()
        wheel = ti.build_wheel(self.base / "wb", version="1.3.0", body="two")
        data = wheel.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        net = FakeNet()
        net.add_release("v1.3.0", {"profile": rs.PROFILE, "version": "1.3.0", "artifact_id": ir.artifact_id("1.3.0", digest),
                         "wheel": wheel.name, "sha256": digest, "channels": ["dev"], "min_compatible": None}, wheel=data)
        result = self.update(source=github(net))
        self.assertEqual(result["outcome"], "updated", result)
        self.assertEqual(result["version"], "1.3.0")

    def test_a_tampered_download_is_refused_before_anything_is_written(self):
        self.first()
        wheel = ti.build_wheel(self.base / "wb", version="1.3.0", body="two")
        digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
        net = FakeNet()
        net.add_release("v1.3.0", {"profile": rs.PROFILE, "version": "1.3.0", "artifact_id": ir.artifact_id("1.3.0", digest),
                                   "wheel": wheel.name, "sha256": digest, "channels": ["dev"], "min_compatible": None},
                        wheel=b"not the wheel")
        before = ti.settled(self.h)
        result = self.update(source=github(net))
        self.assertEqual((result["outcome"], result["reason"]), ("refused", "sha256-mismatch"), result)
        self.assertEqual(ti.settled(self.h), before)

    def test_updates_default_to_auto_and_manual_leaves_no_pending_action(self):
        self.first()
        ti.make_release(self.releases, "r2", version="1.3.0", body="two")
        self.assertEqual((uc.status(self.h)["mode"], uc.status(self.h)["notice"]), ("auto", "notify"))
        self.assertEqual(uc.set_mode("manual", home=self.h)["outcome"], "changed")
        self.assertEqual(uc.status(self.h)["notice"], "manual")
        self.assertEqual(self.check(source=self.releases)["notice"], "off")
        self.assertEqual(pending_actions.list_actions(self.h), [])

    def test_with_the_notice_on_a_check_leaves_one_pending_action_and_an_update_clears_it(self):
        self.first()
        ti.make_release(self.releases, "r2", version="1.3.0", body="two")
        self.assertEqual(uc.set_notice("on", home=self.h)["outcome"], "enabled")
        self.assertEqual(uc.set_notice("on", home=self.h)["outcome"], "unchanged")
        first = self.check(source=self.releases)
        self.assertEqual(first["notice"], "pending")
        self.check(source=self.releases)
        (entry,) = pending_actions.list_actions(self.h)
        self.assertEqual((entry["source"], entry["harness"]), ("update:available", "any"))
        self.assertIn("1.3.0", entry["action"])
        self.assertIn("eutonos-install update", entry["action"])
        self.assertEqual(self.update()["outcome"], "updated")
        self.assertEqual(self.check(source=self.releases)["notice"], "cleared")
        self.assertEqual(pending_actions.list_actions(self.h), [])
        status = uc.status(self.h)
        self.assertEqual((status["enabled"], status["last_available"]), (True, None))

    def test_switching_the_notice_off_dismisses_what_it_left(self):
        self.first()
        ti.make_release(self.releases, "r2", version="1.3.0", body="two")
        uc.set_notice("on", home=self.h)
        self.check(source=self.releases)
        self.assertEqual(len(pending_actions.list_actions(self.h)), 1)
        self.assertEqual(uc.set_notice("off", home=self.h)["outcome"], "disabled")
        self.assertEqual(pending_actions.list_actions(self.h), [])
        self.assertEqual(uc.set_notice("sometimes", home=self.h)["outcome"], "refused")

    def test_a_damaged_settings_file_is_loud_not_the_defaults(self):
        self.first()
        target = uc.path(self.h)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("{nope", encoding="utf-8")
        with self.assertRaises(uc.UpdateCheckError):
            uc.read(self.h)
        self.assertEqual(uc.status(self.h)["outcome"], "failed")
        self.assertEqual(uc.set_notice("on", home=self.h)["outcome"], "failed")
        self.assertEqual(self.check(source=self.releases)["outcome"], "no-release")  # a folder source ignores the repo

    def test_the_command_line_has_check_and_the_update_notice_setting(self):
        self.first()
        ti.make_release(self.releases, "r2", version="1.3.0", body="two")
        out = io.StringIO()
        with unittest.mock.patch("sys.stdout", out):
            code = installer.main(["--home", str(self.h), "update", "--check", "--source", str(self.releases),
                                  "--allow-unsigned"])
        self.assertEqual(code, 0, out.getvalue())
        self.assertEqual(json.loads(out.getvalue())["outcome"], "update-available")
        out = io.StringIO()
        with unittest.mock.patch("sys.stdout", out):
            self.assertEqual(installer.main(["--home", str(self.h), "setting", "set", "update-notice", "on"]), 0)
        self.assertEqual(json.loads(out.getvalue())["outcome"], "enabled")
        out = io.StringIO()
        with unittest.mock.patch("sys.stdout", out):
            installer.main(["--home", str(self.h), "setting", "status"])
        self.assertIn("update-mode", out.getvalue())
        self.assertIn("EUTONOS/releases", out.getvalue())
        out = io.StringIO()
        with unittest.mock.patch("sys.stdout", out):
            self.assertEqual(installer.main(["--home", str(self.h), "setting", "set", "update-mode", "manual"]), 0)
        self.assertEqual((json.loads(out.getvalue())["outcome"], uc.read(self.h)["mode"]), ("changed", "manual"))
        with unittest.mock.patch("sys.stdout", io.StringIO()):
            self.assertEqual(installer.main(["--home", str(self.h), "setting", "set", "update-mode", "often"]), 1)


class UpdateModeTests(unittest.TestCase):
    """The three modes in the settings file: the default, the migration of the earlier notice value, the alias, the
    daily schedule and the recorded attempts. No network and no install."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.h = Path(self._tmp.name) / "home"
        self.h.mkdir()

    def write_raw(self, value):
        target = uc.path(self.h)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(value), encoding="utf-8")

    def pend(self):
        return pending_actions.add(self.h, source=uc.SOURCE, action="Tell the person.", why="A release is waiting.")

    def test_no_file_means_auto_and_the_modes_are_the_three_words(self):
        self.assertEqual(uc.MODES, ("manual", "notify", "auto"))
        self.assertEqual((uc.read(self.h)["mode"], uc.mode(self.h)), ("auto", "auto"))
        status = uc.status(self.h)
        self.assertEqual((status["setting"], status["mode"], status["modes"]), ("update-mode", "auto", list(uc.MODES)))
        self.assertEqual(set(status["labels"]), set(uc.MODES))

    def test_an_earlier_notice_only_file_keeps_its_meaning_with_no_data_loss(self):
        for notice, mode in (("manual", "manual"), ("notify", "notify")):
            with self.subTest(notice=notice):
                self.write_raw({"profile": "eutonos.update-check.v1", "notice": notice, "repo": "Owner/Repo",
                                "last_checked": "2026-10-01T00:00:00Z",
                                "last_available": {"version": "9.0.0", "artifact_id": "a"}})
                found = uc.read(self.h)
                self.assertEqual((found["mode"], found["repo"], found["last_checked"]),
                                 (mode, "Owner/Repo", "2026-10-01T00:00:00Z"))
                self.assertEqual(found["last_available"]["version"], "9.0.0")
                self.assertIsNone(found["staged"])
                uc.set_repo("Owner/Repo", home=self.h)  # a later write keeps the old fields and adds the mode
                again = json.loads(uc.path(self.h).read_text(encoding="utf-8"))
                self.assertEqual((again["mode"], again["notice"]), (mode, "manual" if mode == "manual" else "notify"))
                self.assertEqual(again["last_available"]["version"], "9.0.0")

    def test_set_mode_is_idempotent_validates_and_keeps_a_notice_for_a_rolled_back_runtime(self):
        self.assertEqual(uc.set_mode("auto", home=self.h)["outcome"], "unchanged")
        self.assertEqual(uc.set_mode("notify", home=self.h)["outcome"], "changed")
        self.assertEqual(uc.set_mode("notify", home=self.h)["outcome"], "unchanged")
        saved = json.loads(uc.path(self.h).read_text(encoding="utf-8"))
        self.assertEqual((saved["mode"], saved["notice"]), ("notify", "notify"))
        self.assertEqual(uc.set_mode("manual", home=self.h)["outcome"], "changed")
        self.assertEqual(json.loads(uc.path(self.h).read_text(encoding="utf-8"))["notice"], "manual")
        for bad in ("sometimes", "", None, ["auto"]):
            self.assertEqual(uc.set_mode(bad, home=self.h)["outcome"], "refused", bad)
        self.assertEqual(uc.mode(self.h), "manual")

    def test_the_notice_words_still_map_onto_the_modes(self):
        self.assertEqual(uc.set_notice("on", home=self.h)["outcome"], "enabled")
        self.assertEqual(uc.mode(self.h), "notify")
        self.assertEqual(uc.set_notice("off", home=self.h)["outcome"], "disabled")
        self.assertEqual(uc.mode(self.h), "manual")
        self.assertEqual(uc.set_notice("on", home=self.h)["outcome"], "enabled")
        self.assertEqual(uc.set_notice("on", home=self.h)["outcome"], "unchanged")
        self.assertEqual(uc.set_notice("auto", home=self.h)["outcome"], "refused")  # the old setting has no third state

    def test_leaving_auto_removes_the_staged_copy_and_manual_dismisses_the_offer(self):
        found = {"version": "9.0.0", "artifact_id": "eutonos-9.0.0-aaaaaaaaaaaa"}
        self.write_raw({"profile": "eutonos.update-check.v1", "mode": "auto", "last_available": found, "staged": found})
        staged = self.h / "updates" / "staged" / found["artifact_id"]
        staged.mkdir(parents=True)
        (staged / "x.whl").write_bytes(b"x")
        self.pend()
        self.assertEqual(uc.set_mode("notify", home=self.h)["outcome"], "changed")
        self.assertFalse(staged.exists())
        self.assertIsNone(uc.read(self.h)["staged"])
        self.assertEqual(uc.set_mode("manual", home=self.h)["outcome"], "changed")
        self.assertEqual(pending_actions.list_actions(self.h), [])
        self.assertIsNone(uc.read(self.h)["last_available"])

    def test_due_is_about_daily_sooner_after_a_failed_attempt_and_never_in_manual(self):
        import datetime
        t0 = datetime.datetime(2026, 10, 26, 12, 0, tzinfo=datetime.timezone.utc)
        self.assertTrue(uc.due(self.h, now=t0))  # never checked
        uc.record_attempt("refused:no-trusted-key", home=self.h, now=t0)
        self.assertFalse(uc.due(self.h, now=t0 + datetime.timedelta(hours=22)))
        self.assertTrue(uc.due(self.h, now=t0 + datetime.timedelta(hours=24)))
        uc.record_attempt("unavailable:unavailable", home=self.h, now=t0)
        self.assertFalse(uc.due(self.h, now=t0 + datetime.timedelta(hours=1)))
        self.assertTrue(uc.due(self.h, now=t0 + datetime.timedelta(hours=3)))  # a failed check is retried sooner
        uc.set_mode("manual", home=self.h)
        self.assertFalse(uc.due(self.h, now=t0 + datetime.timedelta(days=30)))

    def test_record_attempt_writes_only_the_time_and_a_result_word(self):
        self.assertTrue(uc.record_attempt("failed:OSError", home=self.h)["recorded"])
        found = uc.read(self.h)
        self.assertEqual(found["last_result"], "failed:OSError")
        self.assertIsNotNone(found["last_attempt"])
        self.assertEqual((found["last_checked"], found["last_available"], found["staged"]), (None, None, None))
        self.assertEqual(pending_actions.list_actions(self.h), [])  # a quiet status: it never nags

    def test_mark_staged_only_in_auto_and_only_for_the_release_last_found(self):
        found = {"version": "9.0.0", "artifact_id": "eutonos-9.0.0-aaaaaaaaaaaa"}
        self.write_raw({"profile": "eutonos.update-check.v1", "mode": "auto", "last_available": found})
        self.assertFalse(uc.mark_staged({"version": "8.0.0", "artifact_id": "other"}, "1.0.0", home=self.h)["marked"])
        marked = uc.mark_staged(found, "1.0.0", home=self.h)
        self.assertTrue(marked["marked"], marked)
        self.assertEqual(uc.read(self.h)["staged"]["artifact_id"], found["artifact_id"])
        (entry,) = pending_actions.list_actions(self.h)
        self.assertIn("eutonos-install update --staged", entry["action"])
        uc.set_mode("notify", home=self.h)
        self.assertFalse(uc.mark_staged(found, "1.0.0", home=self.h)["marked"])

    def test_after_update_clears_the_offer_the_staged_copy_and_the_pending_action(self):
        found = {"version": "9.0.0", "artifact_id": "eutonos-9.0.0-aaaaaaaaaaaa"}
        self.write_raw({"profile": "eutonos.update-check.v1", "mode": "auto", "last_available": found, "staged": found})
        self.pend()
        self.assertTrue(uc.after_update(home=self.h)["cleared"])
        self.assertEqual((uc.read(self.h)["last_available"], uc.read(self.h)["staged"]), (None, None))
        self.assertEqual(pending_actions.list_actions(self.h), [])
        self.assertEqual(uc.mode(self.h), "auto")


if __name__ == "__main__":
    unittest.main()
