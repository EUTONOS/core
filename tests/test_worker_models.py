"""Tests for `eutonos.worker_models` (AWR slice-16e8b55993c3): family/pin
resolution at `new`, per-session provenance, resume model consistency,
after-job worker turnover, launch fencing, and the read-only `status` CLI.

No real claude/codex/model is ever launched or called; `eutonos.model_catalog`
is mocked per test (its real, cherry-picked implementation is also exercised
directly in a couple of places, see `test_against_real_model_catalog_*`)."""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from unittest import mock

import eutonos as eutonos_pkg
from eutonos import worker_models as wm
from eutonos import local_lock as ll


@contextmanager
def model_catalog_unimportable():
    """Force `from eutonos import model_catalog` to raise ImportError, for real,
    regardless of module-attribute caching. Merely setting
    `sys.modules['eutonos.model_catalog']` to `None` is not enough by itself:
    once anything in the process has done `import eutonos.model_catalog`
    successfully (every other test in this file has), Python caches it as an
    attribute on the `eutonos` package object, and `from eutonos import X`
    reads that cached attribute directly without ever consulting `sys.modules`
    again. Removing the attribute too forces a fresh import attempt, which is
    where the `None` sentinel in `sys.modules` then correctly raises
    ImportError (a documented CPython convention)."""
    had_attr = hasattr(eutonos_pkg, "model_catalog")
    prior = getattr(eutonos_pkg, "model_catalog", None)
    if had_attr:
        delattr(eutonos_pkg, "model_catalog")
    try:
        with mock.patch.dict(sys.modules, {"eutonos.model_catalog": None}):
            yield
    finally:
        if had_attr:
            eutonos_pkg.model_catalog = prior


def _req(mode="new", *, session_id=None, model=None, effort=None, conversation_id=None,
        caller_work_id=None, request_id="req-1"):
    req = {"id": request_id, "mode": mode, "to": "claude"}
    if session_id is not None:
        req["session_id"] = session_id
    if model is not None:
        req["model"] = model
    if effort is not None:
        req["effort"] = effort
    if conversation_id is not None:
        req["conversation_id"] = conversation_id
    if caller_work_id is not None:
        req["caller_work_id"] = caller_work_id
    return req


def _plan(resolved, *, requested=None, selection_kind="family", family=None,
         supported_efforts=None, basis="test"):
    return {"requested": requested, "resolved": resolved, "selection_kind": selection_kind,
            "family": family, "supported_efforts": supported_efforts, "basis": basis}


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        environment = mock.patch.dict(os.environ, {"EUTONOS_HOME": str(self.root / "eutonos-home"), "CODEX_HOME": str(self.root / "codex-home")})
        environment.start()
        self.addCleanup(environment.stop)

    def _catalog(self, table):
        """Patch `eutonos.model_catalog.resolve_choice` with a small lookup
        table keyed by (choice, harness) -> resolved dict fields."""
        def resolve_choice(choice, harness, *, root=None, effort=None):
            key = (choice, harness)
            if key not in table:
                raise ValueError(f"Model choice unavailable: {choice}")
            return {"requested": choice, **table[key]}
        patcher = mock.patch("eutonos.model_catalog.resolve_choice", side_effect=resolve_choice)
        patcher.start()
        self.addCleanup(patcher.stop)


# --------------------------------------------------------------- resolve_choice

class ResolveChoiceTests(Base):
    def test_family_resolves_through_model_catalog(self):
        self._catalog({("sol", "codex"): {"resolved": "gpt-6-sol", "selection_kind": "family",
                                          "family": "sol", "supported_efforts": ["low", "high"],
                                          "basis": "codex-native-cache"}})
        result = wm.resolve_choice("sol", "codex", root=self.root)
        self.assertEqual(result["resolved"], "gpt-6-sol")
        self.assertEqual(result["selection_kind"], "family")
        self.assertEqual(result["family"], "sol")

    def test_catalog_value_error_becomes_worker_model_error(self):
        self._catalog({})
        with self.assertRaises(wm.WorkerModelError) as ctx:
            wm.resolve_choice("sol", "codex", root=self.root)
        self.assertEqual(ctx.exception.code, "model-unresolved")

    def test_catalog_unavailable_bare_family_refuses(self):
        with model_catalog_unimportable():
            with self.assertRaises(wm.WorkerModelError) as ctx:
                wm.resolve_choice("sol", "codex", root=self.root)
            self.assertEqual(ctx.exception.code, "model-unresolved")

    def test_catalog_unavailable_exact_pin_passes_through(self):
        with model_catalog_unimportable():
            result = wm.resolve_choice("gpt-6-luna", "codex", root=self.root)
            self.assertEqual(result["resolved"], "gpt-6-luna")
            self.assertEqual(result["selection_kind"], "pin")
            self.assertEqual(result["basis"], "catalog-unavailable")

    def test_empty_choice_refuses(self):
        with self.assertRaises(wm.WorkerModelError):
            wm.resolve_choice("", "claude", root=self.root)


# ------------------------------------------------------------------- plan_new

class PlanNewTests(Base):
    def test_no_model_uses_default_with_selection_kind_default(self):
        plan = wm.plan_new({"model": None}, harness="claude", root=self.root,
                           default_model="claude-sonnet-5")
        self.assertEqual(plan, {"requested": None, "resolved": "claude-sonnet-5",
                                "selection_kind": "default", "family": None,
                                "supported_efforts": None, "basis": "claude-default"})

    def test_no_model_no_default_is_unspecified(self):
        plan = wm.plan_new({"model": None}, harness="codex", root=self.root)
        self.assertEqual(plan["selection_kind"], "unspecified")
        self.assertIsNone(plan["resolved"])

    def test_requested_model_delegates_to_resolve_choice(self):
        self._catalog({("luna", "codex"): {"resolved": "gpt-6-luna", "selection_kind": "family",
                                           "family": "luna", "supported_efforts": None, "basis": "t"}})
        plan = wm.plan_new({"model": "luna"}, harness="codex", root=self.root)
        self.assertEqual(plan["resolved"], "gpt-6-luna")


# ---------------------------------------------------------------- plan_launch

class PlanLaunchTests(Base):
    def test_haiku_refuses_for_new(self):
        with self.assertRaises(wm.WorkerModelError) as ctx:
            wm.plan_launch(_req("new", model="claude-haiku-4-5"), harness="claude", root=self.root)
        self.assertEqual(ctx.exception.code, "model-unavailable")

    def test_haiku_5_5_is_not_refused_for_new_or_resume(self):
        for request in (_req("new", model="claude-haiku-5-5"),
                        _req("resume", session_id="s1", model="claude-haiku-5-5")):
            try:
                wm.plan_launch(request, harness="claude", root=self.root)
            except wm.WorkerModelError as error:
                self.assertNotIn("cannot run auto mode", str(error))

    def test_haiku_refuses_for_resume(self):
        with self.assertRaises(wm.WorkerModelError) as ctx:
            wm.plan_launch(_req("resume", session_id="s1", model="Claude-Haiku-4-5"),
                           harness="claude", root=self.root)
        self.assertEqual(ctx.exception.code, "model-unavailable")

    def test_haiku_only_refused_for_claude_harness(self):
        # The owner's Haiku refusal is Claude-specific; a codex request naming something
        # containing "haiku" is just an ordinary pin lookup, not the Claude-specific refusal.
        # A resolvable catalog entry proves it reaches normal resolution instead of refusing.
        self._catalog({("some-haiku-lookalike", "codex"): {
            "resolved": "some-haiku-lookalike", "selection_kind": "pin",
            "family": None, "supported_efforts": None, "basis": "test"}})
        plan = wm.plan_launch(_req("new", model="some-haiku-lookalike"), harness="codex", root=self.root)
        self.assertEqual(plan["selection_kind"], "pin")


# ------------------------------------------------------------ record/finalize

class RecordAndFinalizeTests(Base):
    def test_record_new_then_load(self):
        entry = wm.record_new(self.root, session_id="s1", harness="claude",
                              req=_req("new", request_id="r1"),
                              plan=_plan("claude-sonnet-5", selection_kind="default"))
        self.assertEqual(entry["resolved"], "claude-sonnet-5")
        self.assertTrue(entry["busy"])
        loaded = wm.load_entry(self.root, "s1")
        self.assertEqual(loaded["session_id"], "s1")

    def test_finalize_missing_session_returns_none(self):
        self.assertIsNone(wm.finalize(self.root, session_id="nope", req=_req("resume", session_id="nope")))

    def test_finalize_clears_busy(self):
        wm.record_new(self.root, session_id="s1", harness="claude", req=_req("new", request_id="r1"),
                      plan=_plan("claude-sonnet-5", selection_kind="default"))
        entry = wm.finalize(self.root, session_id="s1", req=_req("new", request_id="r1"))
        self.assertFalse(entry["busy"])
        self.assertIsNone(entry["in_flight_request_id"])

    def test_finalize_records_observed_model(self):
        wm.record_new(self.root, session_id="s1", harness="claude", req=_req("new", request_id="r1"),
                      plan=_plan("claude-sonnet-5", selection_kind="default"))
        entry = wm.finalize(self.root, session_id="s1", req=_req("new", request_id="r1"),
                            observed="claude-sonnet-5-20260901", observed_source="modelUsage")
        self.assertEqual(entry["observed"], "claude-sonnet-5-20260901")
        self.assertEqual(entry["observed_source"], "modelUsage")

    def test_provenance_view_excludes_profile(self):
        entry = wm.record_new(self.root, session_id="s1", harness="claude", req=_req("new"),
                              plan=_plan("claude-sonnet-5"))
        view = wm.provenance_view(entry)
        self.assertNotIn("profile", view)
        self.assertEqual(view["session_id"], "s1")

    def test_load_entry_rejects_oversized_file(self):
        wm.record_new(self.root, session_id="s1", harness="claude", req=_req("new"), plan=_plan("m"))
        path = wm.registry_dir(self.root) / "s1.json"
        path.write_text("x" * (wm.MAX_ENTRY_BYTES + 10), encoding="utf-8")
        self.assertIsNone(wm.load_entry(self.root, "s1"))

    def test_load_entry_rejects_session_id_mismatch(self):
        wm.record_new(self.root, session_id="s1", harness="claude", req=_req("new"), plan=_plan("m"))
        # A file that exists under s2's path but was never a valid s2 record (e.g. copied) is not trusted.
        (wm.registry_dir(self.root) / "s2.json").write_text(
            (wm.registry_dir(self.root) / "s1.json").read_text(encoding="utf-8"), encoding="utf-8")
        self.assertIsNone(wm.load_entry(self.root, "s2"))

    def test_load_entry_rejects_corrupt_json(self):
        wm.record_new(self.root, session_id="s1", harness="claude", req=_req("new"), plan=_plan("m"))
        path = wm.registry_dir(self.root) / "s1.json"
        path.write_text("{not valid json at all", encoding="utf-8")
        self.assertIsNone(wm.load_entry(self.root, "s1"))

    def test_load_entry_rejects_malformed_resolved_field(self):
        """Sol review, 2026-09-29: a corrupt/foreign record whose `resolved`
        field is not a plain string must never be trusted -- it could end up
        smuggled straight into a launched app's `-m` argv."""
        wm.record_new(self.root, session_id="s1", harness="claude", req=_req("new"), plan=_plan("m"))
        path = wm.registry_dir(self.root) / "s1.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["resolved"] = ["not", "a", "string"]
        path.write_text(json.dumps(data), encoding="utf-8")
        self.assertIsNone(wm.load_entry(self.root, "s1"))

    def test_load_entry_rejects_malformed_observed_field(self):
        wm.record_new(self.root, session_id="s1", harness="claude", req=_req("new"), plan=_plan("m"))
        path = wm.registry_dir(self.root) / "s1.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["observed"] = {"nested": "dict"}
        path.write_text(json.dumps(data), encoding="utf-8")
        self.assertIsNone(wm.load_entry(self.root, "s1"))

    def test_load_entry_rejects_non_bool_busy_field(self):
        wm.record_new(self.root, session_id="s1", harness="claude", req=_req("new"), plan=_plan("m"))
        path = wm.registry_dir(self.root) / "s1.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["busy"] = "yes"
        path.write_text(json.dumps(data), encoding="utf-8")
        self.assertIsNone(wm.load_entry(self.root, "s1"))

    def test_load_entry_rejects_missing_harness(self):
        wm.record_new(self.root, session_id="s1", harness="claude", req=_req("new"), plan=_plan("m"))
        path = wm.registry_dir(self.root) / "s1.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        del data["harness"]
        path.write_text(json.dumps(data), encoding="utf-8")
        self.assertIsNone(wm.load_entry(self.root, "s1"))


# ---------------------------------------------------------------- plan_resume

class PlanResumeTests(Base):
    def _new_and_finalize(self, session_id, plan, *, conversation_id=None, caller_work_id=None,
                          request_id="r1"):
        req = _req("new", request_id=request_id, conversation_id=conversation_id,
                   caller_work_id=caller_work_id)
        wm.record_new(self.root, session_id=session_id, harness="codex", req=req, plan=plan)
        wm.finalize(self.root, session_id=session_id, req=req)

    def test_unknown_session_refuses(self):
        with self.assertRaises(wm.WorkerModelError) as ctx:
            wm.plan_resume(_req("resume", session_id="ghost"), harness="claude", root=self.root)
        self.assertEqual(ctx.exception.code, "resume-model-unknown")

    def test_resume_refuses_when_harness_does_not_match_recorded(self):
        """Sol review, 2026-09-29: "bind the stored harness to the requested
        harness -- the same UUID under an incompatible harness is refused."
        A codex session id must never be resumable as a claude harness, or
        vice versa, even though the registry has an otherwise-valid entry."""
        self._new_and_finalize("s1", _plan("gpt-6-sol", selection_kind="family", family="sol"))
        with self.assertRaises(wm.WorkerModelError) as ctx:
            wm.plan_resume(_req("resume", session_id="s1"), harness="claude", root=self.root)
        self.assertEqual(ctx.exception.code, "resume-harness-mismatch")
        # Confirmed unaffected: the correct harness still resumes normally.
        plan = wm.plan_resume(_req("resume", session_id="s1"), harness="codex", root=self.root)
        self.assertEqual(plan["resolved"], "gpt-6-sol")

    def test_same_job_keeps_recorded_model_even_with_newer_available(self):
        self._new_and_finalize("s1", _plan("gpt-6-sol", selection_kind="family", family="sol"),
                               conversation_id="conv-A")
        self._catalog({("sol", "codex"): {"resolved": "gpt-6.1-sol", "selection_kind": "family",
                                          "family": "sol", "supported_efforts": None, "basis": "t"}})
        plan = wm.plan_resume(_req("resume", session_id="s1", conversation_id="conv-A"),
                              harness="codex", root=self.root)
        self.assertEqual(plan["resolved"], "gpt-6-sol")  # unchanged, never the newer one

    def test_different_job_family_worker_retires_when_newer_exists(self):
        self._new_and_finalize("s1", _plan("gpt-6-sol", selection_kind="family", family="sol"),
                               conversation_id="conv-A")
        self._catalog({("sol", "codex"): {"resolved": "gpt-6.1-sol", "selection_kind": "family",
                                          "family": "sol", "supported_efforts": None, "basis": "t"}})
        with self.assertRaises(wm.WorkerModelError) as ctx:
            wm.plan_resume(_req("resume", session_id="s1", conversation_id="conv-B"),
                          harness="codex", root=self.root)
        self.assertEqual(ctx.exception.code, "worker-retired")
        handoff = ctx.exception.extra["handoff"]
        self.assertEqual(handoff["newer_model"], "gpt-6.1-sol")
        self.assertEqual(handoff["prior_session_id"], "s1")
        self.assertEqual(set(handoff["continuity_required"]),
                         {"source_revision", "checks", "unresolved", "next_action"})
        entry = wm.load_entry(self.root, "s1")
        self.assertTrue(entry["retired"])

    def test_different_job_family_worker_stays_when_no_newer_model(self):
        self._new_and_finalize("s1", _plan("gpt-6-sol", selection_kind="family", family="sol"),
                               conversation_id="conv-A")
        self._catalog({("sol", "codex"): {"resolved": "gpt-6-sol", "selection_kind": "family",
                                          "family": "sol", "supported_efforts": None, "basis": "t"}})
        plan = wm.plan_resume(_req("resume", session_id="s1", conversation_id="conv-B"),
                              harness="codex", root=self.root)
        self.assertEqual(plan["resolved"], "gpt-6-sol")
        self.assertFalse(wm.load_entry(self.root, "s1")["retired"])

    def test_pin_never_retires_across_a_different_job(self):
        self._new_and_finalize("s1", _plan("gpt-6-sol", selection_kind="pin", family=None),
                               conversation_id="conv-A")
        # No catalog needed at all -- a pin's resume never even consults it for turnover.
        plan = wm.plan_resume(_req("resume", session_id="s1", conversation_id="conv-B"),
                              harness="codex", root=self.root)
        self.assertEqual(plan["resolved"], "gpt-6-sol")
        self.assertFalse(wm.load_entry(self.root, "s1")["retired"])

    def test_catalog_unavailable_never_retires(self):
        self._new_and_finalize("s1", _plan("gpt-6-sol", selection_kind="family", family="sol"),
                               conversation_id="conv-A")
        with model_catalog_unimportable():
            plan = wm.plan_resume(_req("resume", session_id="s1", conversation_id="conv-B"),
                                  harness="codex", root=self.root)
        self.assertEqual(plan["resolved"], "gpt-6-sol")
        self.assertFalse(wm.load_entry(self.root, "s1")["retired"])

    def test_work_id_decides_same_job_despite_conversation_change(self):
        """Codex review, 2026-09-29: the old rule preferred conversation_id and
        threw away caller_work_id, so the SAME work item's conversation moving
        from A to B (e.g. after an owner-visible upgrade) wrongly looked like a
        different job and retired the worker. W+A -> W+B must be the SAME job
        -- both sides carry a caller_work_id, so it decides, and it matches."""
        self._new_and_finalize("s1", _plan("gpt-6-sol", selection_kind="family", family="sol"),
                               conversation_id="conv-A", caller_work_id="W")
        self._catalog({("sol", "codex"): {"resolved": "gpt-6.1-sol", "selection_kind": "family",
                                          "family": "sol", "supported_efforts": None, "basis": "t"}})
        plan = wm.plan_resume(_req("resume", session_id="s1", conversation_id="conv-B", caller_work_id="W"),
                              harness="codex", root=self.root)
        self.assertEqual(plan["resolved"], "gpt-6-sol")  # unchanged -- never retired
        self.assertFalse(wm.load_entry(self.root, "s1")["retired"])

    def test_bare_conversation_continuing_under_a_newly_added_work_id_is_same_job(self):
        """W -> W+A: a bare conversation (recorded with no caller_work_id at
        all) continuing under a newly-added work id is also the SAME job --
        the work-id axis is only comparable once BOTH sides carry one, so
        with nothing recorded to disagree with, this is not a different job."""
        self._new_and_finalize("s1", _plan("gpt-6-sol", selection_kind="family", family="sol"),
                               conversation_id="conv-A")
        self._catalog({("sol", "codex"): {"resolved": "gpt-6.1-sol", "selection_kind": "family",
                                          "family": "sol", "supported_efforts": None, "basis": "t"}})
        plan = wm.plan_resume(_req("resume", session_id="s1", conversation_id="conv-A", caller_work_id="W"),
                              harness="codex", root=self.root)
        self.assertEqual(plan["resolved"], "gpt-6-sol")
        self.assertFalse(wm.load_entry(self.root, "s1")["retired"])

    def test_work_id_decides_different_job_even_with_matching_conversation(self):
        """W+A -> X+A: the work id decides even when the conversation id
        still matches -- a genuinely different work item (X) reusing the
        same conversation id must still be treated as a different job, and
        (with a family worker and a newer model available) retired."""
        self._new_and_finalize("s1", _plan("gpt-6-sol", selection_kind="family", family="sol"),
                               conversation_id="conv-A", caller_work_id="W")
        self._catalog({("sol", "codex"): {"resolved": "gpt-6.1-sol", "selection_kind": "family",
                                          "family": "sol", "supported_efforts": None, "basis": "t"}})
        with self.assertRaises(wm.WorkerModelError) as ctx:
            wm.plan_resume(_req("resume", session_id="s1", conversation_id="conv-A", caller_work_id="X"),
                          harness="codex", root=self.root)
        self.assertEqual(ctx.exception.code, "worker-retired")
        self.assertTrue(wm.load_entry(self.root, "s1")["retired"])

    def test_repeated_different_job_attempts_stay_refused(self):
        """Sol review, 2026-09-29: retirement must be sticky -- a SECOND different-job
        attempt after the first retirement must also be refused, not silently resumed."""
        self._new_and_finalize("s1", _plan("gpt-6-sol", selection_kind="family", family="sol"),
                               conversation_id="conv-A")
        self._catalog({("sol", "codex"): {"resolved": "gpt-6.1-sol", "selection_kind": "family",
                                          "family": "sol", "supported_efforts": None, "basis": "t"}})
        with self.assertRaises(wm.WorkerModelError) as first:
            wm.plan_resume(_req("resume", session_id="s1", conversation_id="conv-B"),
                          harness="codex", root=self.root)
        self.assertEqual(first.exception.code, "worker-retired")
        # A second, later different-job attempt (conv-C) must ALSO be refused --
        # not fall through to an ordinary resume just because "retired" already flipped once.
        with self.assertRaises(wm.WorkerModelError) as second:
            wm.plan_resume(_req("resume", session_id="s1", conversation_id="conv-C"),
                          harness="codex", root=self.root)
        self.assertEqual(second.exception.code, "worker-retired")

    def test_same_job_resume_of_a_retired_entry_still_works(self):
        self._new_and_finalize("s1", _plan("gpt-6-sol", selection_kind="family", family="sol"),
                               conversation_id="conv-A")
        self._catalog({("sol", "codex"): {"resolved": "gpt-6.1-sol", "selection_kind": "family",
                                          "family": "sol", "supported_efforts": None, "basis": "t"}})
        with self.assertRaises(wm.WorkerModelError):
            wm.plan_resume(_req("resume", session_id="s1", conversation_id="conv-B"),
                          harness="codex", root=self.root)
        # finalize the failed different-job attempt so the session is not left "busy"
        # (plan_resume only marks busy on an APPROVED plan, so nothing to clear here --
        # but confirm the ORIGINAL job (conv-A) can still resume normally).
        plan = wm.plan_resume(_req("resume", session_id="s1", conversation_id="conv-A"),
                              harness="codex", root=self.root)
        self.assertEqual(plan["resolved"], "gpt-6-sol")

    def test_exact_model_mismatch_refuses(self):
        self._new_and_finalize("s1", _plan("gpt-6-sol", selection_kind="pin"))
        with self.assertRaises(wm.WorkerModelError) as ctx:
            wm.plan_resume(_req("resume", session_id="s1", model="gpt-6-astra"), harness="codex", root=self.root)
        self.assertEqual(ctx.exception.code, "resume-model-mismatch")

    def test_family_mismatch_refuses(self):
        self._new_and_finalize("s1", _plan("gpt-6-sol", selection_kind="family", family="sol"))
        with self.assertRaises(wm.WorkerModelError) as ctx:
            wm.plan_resume(_req("resume", session_id="s1", model="luna"), harness="codex", root=self.root)
        self.assertEqual(ctx.exception.code, "resume-model-mismatch")

    def test_matching_family_request_is_accepted(self):
        self._new_and_finalize("s1", _plan("gpt-6-sol", selection_kind="family", family="sol"))
        plan = wm.plan_resume(_req("resume", session_id="s1", model="sol"), harness="codex", root=self.root)
        self.assertEqual(plan["resolved"], "gpt-6-sol")


# --------------------------------------------------------------- launch fencing

class LaunchFencingTests(Base):
    def test_resume_while_busy_is_fenced(self):
        wm.record_new(self.root, session_id="s1", harness="claude", req=_req("new", request_id="r1"),
                      plan=_plan("claude-sonnet-5", selection_kind="default"))
        # No finalize yet -- the "new" launch is still in flight (busy=True by construction).
        with self.assertRaises(wm.WorkerModelError) as ctx:
            wm.plan_resume(_req("resume", session_id="s1", request_id="r2"), harness="claude", root=self.root)
        self.assertEqual(ctx.exception.code, "session-busy")
        self.assertEqual(ctx.exception.extra["in_flight_request_id"], "r1")

    def test_resume_fenced_even_for_the_same_job(self):
        req_new = _req("new", request_id="r1", conversation_id="conv-A")
        wm.record_new(self.root, session_id="s1", harness="claude", req=req_new,
                      plan=_plan("claude-sonnet-5", selection_kind="default"))
        with self.assertRaises(wm.WorkerModelError) as ctx:
            wm.plan_resume(_req("resume", session_id="s1", request_id="r2", conversation_id="conv-A"),
                          harness="claude", root=self.root)
        self.assertEqual(ctx.exception.code, "session-busy")

    def test_finalize_clears_the_fence_so_a_later_resume_succeeds(self):
        req_new = _req("new", request_id="r1")
        wm.record_new(self.root, session_id="s1", harness="claude", req=req_new,
                      plan=_plan("claude-sonnet-5", selection_kind="default"))
        wm.finalize(self.root, session_id="s1", req=req_new)
        plan = wm.plan_resume(_req("resume", session_id="s1", request_id="r2"), harness="claude", root=self.root)
        self.assertEqual(plan["resolved"], "claude-sonnet-5")

    def test_lock_timeout_refuses_as_registry_busy(self):
        wm.record_new(self.root, session_id="s1", harness="claude", req=_req("new", request_id="r1"),
                      plan=_plan("claude-sonnet-5", selection_kind="default"))
        wm.finalize(self.root, session_id="s1", req=_req("new", request_id="r1"))
        started = threading.Event()
        release = threading.Event()

        def hold_lock():
            with ll.local_lock(self.root):
                started.set()
                release.wait(5)

        holder = threading.Thread(target=hold_lock, daemon=True)
        holder.start()
        try:
            self.assertTrue(started.wait(5), "background holder never acquired the lock")
            with mock.patch.object(wm, "LOCK_TIMEOUT_SECONDS", 0.2):
                with self.assertRaises(wm.WorkerModelError) as ctx:
                    wm.plan_resume(_req("resume", session_id="s1", request_id="r2"),
                                  harness="claude", root=self.root)
                self.assertEqual(ctx.exception.code, "registry-busy")
        finally:
            release.set()
            holder.join(5)


# ------------------------------------------------ stale-busy recovery (Codex/Sol review, 2026-09-29)

class StaleBusyRecoveryTests(Base):
    """`plan_resume` never clears a busy reservation by itself; recovery is the
    manual `release`/`release_unlaunched` primitives (findings 1 & 2 from the
    a02c9a3 review). A terminal reply left by the retired file mailbox proves
    nothing now and is no longer read."""

    def _busy_entry(self, session_id="s1", *, in_flight_request_id="r-inflight"):
        wm.record_new(self.root, session_id=session_id, harness="claude",
                      req=_req("new", request_id=in_flight_request_id),
                      plan=_plan("claude-sonnet-5", selection_kind="default"))
        return in_flight_request_id

    def test_same_request_id_repeat_while_genuinely_in_flight_still_refuses(self):
        """A matching request id is NOT proof the earlier launch ended (Codex
        review, dropping the earlier draft's 2(a) same-id bypass): retrying
        `resume` with the SAME request id while the reservation is held must
        still refuse."""
        in_flight = self._busy_entry()
        with self.assertRaises(wm.WorkerModelError) as ctx:
            wm.plan_resume(_req("resume", session_id="s1", request_id=in_flight),
                          harness="claude", root=self.root)
        self.assertEqual(ctx.exception.code, "session-busy")

    def test_busy_stays_fenced_and_names_the_manual_release(self):
        in_flight = self._busy_entry()
        with self.assertRaises(wm.WorkerModelError) as ctx:
            wm.plan_resume(_req("resume", session_id="s1", request_id="r2"),
                          harness="claude", root=self.root)
        self.assertEqual(ctx.exception.code, "session-busy")
        self.assertIn("worker_models release --session s1", str(ctx.exception))
        self.assertTrue(wm.load_entry(self.root, "s1")["busy"])

    def test_a_reply_file_left_by_the_retired_file_mailbox_never_clears_busy(self):
        """The reply files belonged to the retired file mailbox's outbox. Nothing
        writes them now, and a leftover `ok` reply that matches the reservation
        must not clear the fence either."""
        in_flight = self._busy_entry("s1")
        outbox = self.root / ".eutonos-local" / "mailbox" / "outbox"
        outbox.mkdir(parents=True)
        (outbox / f"{in_flight}.reply.json").write_text(
            json.dumps({"id": in_flight, "status": "ok", "session_id": "s1"}), encoding="utf-8")
        with self.assertRaises(wm.WorkerModelError) as ctx:
            wm.plan_resume(_req("resume", session_id="s1", request_id="r2"),
                          harness="claude", root=self.root)
        self.assertEqual(ctx.exception.code, "session-busy")

    def test_release_unlaunched_clears_only_its_own_matching_reservation(self):
        in_flight = self._busy_entry("s1")
        cleared = wm.release_unlaunched(self.root, session_id="s1", request_id=in_flight)
        self.assertIsNotNone(cleared)
        entry = wm.load_entry(self.root, "s1")
        self.assertFalse(entry["busy"])
        self.assertIsNone(entry["in_flight_request_id"])

    def test_release_unlaunched_never_clears_a_different_requests_reservation(self):
        """Finding 1: never clear another request's reservation, or a launch
        that may still be running -- a stale/wrong request id must be a
        no-op."""
        self._busy_entry("s1", in_flight_request_id="r-real")
        cleared = wm.release_unlaunched(self.root, session_id="s1", request_id="r-other")
        self.assertIsNone(cleared)
        entry = wm.load_entry(self.root, "s1")
        self.assertTrue(entry["busy"])
        self.assertEqual(entry["in_flight_request_id"], "r-real")

    def test_release_unlaunched_is_a_noop_for_an_unknown_session(self):
        self.assertIsNone(wm.release_unlaunched(self.root, session_id="ghost", request_id="r1"))

    def test_release_clears_unconditionally_and_reports_what_it_cleared(self):
        self._busy_entry("s1", in_flight_request_id="r-stuck")
        result = wm.release(self.root, "s1")
        self.assertEqual(result, {"session_id": "s1", "found": True, "cleared": True,
                                  "prior_in_flight_request_id": "r-stuck"})
        entry = wm.load_entry(self.root, "s1")
        self.assertFalse(entry["busy"])
        self.assertIsNone(entry["in_flight_request_id"])
        # A subsequent resume now proceeds normally.
        plan = wm.plan_resume(_req("resume", session_id="s1", request_id="r2"), harness="claude", root=self.root)
        self.assertEqual(plan["resolved"], "claude-sonnet-5")

    def test_release_on_an_unknown_session_reports_not_found(self):
        self.assertEqual(wm.release(self.root, "ghost"), {"session_id": "ghost", "found": False})

    def test_cli_release_smoke(self):
        self._busy_entry("s1", in_flight_request_id="r-stuck")
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = wm.main(["release", "--root", str(self.root), "--session", "s1"])
        self.assertEqual(code, 0)
        parsed = json.loads(buf.getvalue())
        self.assertEqual(parsed, {"session_id": "s1", "found": True, "cleared": True,
                                  "prior_in_flight_request_id": "r-stuck"})


# ------------------------------------------------------------- job binding preservation

class JobBindingPreservationTests(Base):
    def test_work_binding_survives_a_conversation_only_followup(self):
        first = _req("new", request_id="r1", caller_work_id="W", conversation_id="A")
        wm.record_new(self.root, session_id="s1", harness="codex", req=first,
                      plan=_plan("gpt-6-sol", selection_kind="family", family="sol"))
        wm.finalize(self.root, session_id="s1", req=first)
        followup = _req("resume", session_id="s1", request_id="r2", conversation_id="A")
        wm.plan_resume(followup, harness="codex", root=self.root)
        wm.finalize(self.root, session_id="s1", req=followup)
        self.assertEqual(wm.load_entry(self.root, "s1")["caller_work_id"], "W")
        self._catalog({("sol", "codex"): {"resolved": "gpt-6.1-sol", "selection_kind": "family",
                                          "family": "sol", "basis": "t"}})
        next_conversation = _req("resume", session_id="s1", request_id="r3",
                                 caller_work_id="W", conversation_id="B")
        plan = wm.plan_resume(next_conversation, harness="codex", root=self.root)
        self.assertEqual(plan["resolved"], "gpt-6-sol")
        wm.finalize(self.root, session_id="s1", req=next_conversation)
        entry = wm.load_entry(self.root, "s1")
        self.assertFalse(entry["retired"])
        self.assertIsNone(entry["job_completed_at"])
        self.assertEqual((entry["caller_work_id"], entry["conversation_id"]), ("W", "B"))

    def test_work_only_job_can_gain_a_conversation_without_turnover(self):
        first = _req("new", request_id="r1", caller_work_id="W")
        wm.record_new(self.root, session_id="s1", harness="codex", req=first,
                      plan=_plan("gpt-6-sol", selection_kind="family", family="sol"))
        wm.finalize(self.root, session_id="s1", req=first)
        self._catalog({("sol", "codex"): {"resolved": "gpt-6.1-sol", "selection_kind": "family",
                                          "family": "sol", "basis": "t"}})
        followup = _req("resume", session_id="s1", request_id="r2",
                        caller_work_id="W", conversation_id="A")
        plan = wm.plan_resume(followup, harness="codex", root=self.root)
        self.assertEqual(plan["resolved"], "gpt-6-sol")
        wm.finalize(self.root, session_id="s1", req=followup)
        entry = wm.load_entry(self.root, "s1")
        self.assertFalse(entry["retired"])
        self.assertIsNone(entry["job_completed_at"])
        self.assertEqual((entry["caller_work_id"], entry["conversation_id"]), ("W", "A"))

    def test_finalize_preserves_job_binding_when_request_carries_no_identity(self):
        """Sol review, 2026-09-29 (end-to-end sequence): named new job -> resume with
        no incoming conversation/work ID -> finalize -> the family advances -> a
        different named job. `finalize` must never clear job_key/conversation_id on an
        identity-less request, or the later, genuinely different job would look like
        "job unknown" and bypass after-job turnover."""
        req_new = _req("new", request_id="r1", conversation_id="conv-A")
        wm.record_new(self.root, session_id="s1", harness="codex", req=req_new,
                      plan=_plan("gpt-6-sol", selection_kind="family", family="sol"))
        wm.finalize(self.root, session_id="s1", req=req_new)

        # Resume with only session_id -- no conversation_id/caller_work_id at all.
        bare_resume = _req("resume", session_id="s1", request_id="r2")
        plan = wm.plan_resume(bare_resume, harness="codex", root=self.root)
        self.assertEqual(plan["resolved"], "gpt-6-sol")
        wm.finalize(self.root, session_id="s1", req=bare_resume)

        entry = wm.load_entry(self.root, "s1")
        self.assertEqual(entry["job_key"], "conversation:conv-A")
        self.assertEqual(entry["conversation_id"], "conv-A")

        # The family advances.
        self._catalog({("sol", "codex"): {"resolved": "gpt-6.1-sol", "selection_kind": "family",
                                          "family": "sol", "supported_efforts": None, "basis": "t"}})
        # A genuinely different named job must still trigger turnover -- it must NOT be
        # bypassed because finalize wiped the job binding on the identity-less resume above.
        with self.assertRaises(wm.WorkerModelError) as ctx:
            wm.plan_resume(_req("resume", session_id="s1", request_id="r3", conversation_id="conv-B"),
                          harness="codex", root=self.root)
        self.assertEqual(ctx.exception.code, "worker-retired")


# ------------------------------------------------------------------ observed model

class ObservedModelTests(unittest.TestCase):
    def test_single_key_is_observed(self):
        self.assertEqual(wm.observed_model_from_claude_result(
            {"modelUsage": {"claude-sonnet-5-20260901": {}}}), "claude-sonnet-5-20260901")

    def test_zero_keys_is_unobserved(self):
        self.assertIsNone(wm.observed_model_from_claude_result({"modelUsage": {}}))

    def test_multiple_keys_is_ambiguous_and_unobserved(self):
        self.assertIsNone(wm.observed_model_from_claude_result(
            {"modelUsage": {"a": {}, "b": {}}}))

    def test_missing_field_is_unobserved(self):
        self.assertIsNone(wm.observed_model_from_claude_result({"result": "hi"}))

    def test_non_dict_is_unobserved(self):
        self.assertIsNone(wm.observed_model_from_claude_result(None))


# ------------------------------------------------------------ registry location

class RegistryLocationTests(Base):
    """The registry lives in `.eutonos-local/workers`. A record the registry kept under the
    older `.eutonos-local/mailbox/workers` folder is still read when the new one is absent,
    and the older folder is never deleted."""

    def _legacy_copy(self, session_id="s1"):
        """Move a recorded entry to the older folder, as a pre-rename checkout has it."""
        wm.record_new(self.root, session_id=session_id, harness="claude", req=_req("new", request_id="r1"),
                      plan=_plan("claude-sonnet-5", selection_kind="default"))
        wm.finalize(self.root, session_id=session_id, req=_req("new", request_id="r1"))
        legacy = wm.legacy_registry_dir(self.root)
        legacy.mkdir(parents=True)
        current = wm.registry_dir(self.root) / f"{session_id}.json"
        (legacy / current.name).write_bytes(current.read_bytes())
        current.unlink()
        wm.registry_dir(self.root).rmdir()
        return legacy / current.name

    def test_records_are_written_to_the_workers_folder(self):
        wm.record_new(self.root, session_id="s1", harness="claude", req=_req("new"), plan=_plan("m"))
        self.assertEqual(wm.registry_dir(self.root), self.root / ".eutonos-local" / "workers")
        self.assertTrue((self.root / ".eutonos-local" / "workers" / "s1.json").is_file())
        self.assertFalse((self.root / ".eutonos-local" / "mailbox").exists())

    def test_an_entry_in_the_older_folder_is_read_when_the_new_one_is_absent(self):
        legacy_file = self._legacy_copy()
        self.assertFalse(wm.registry_dir(self.root).exists())
        entry = wm.load_entry(self.root, "s1")
        self.assertEqual(entry["resolved"], "claude-sonnet-5")
        self.assertEqual(wm.status(self.root, "s1")["resolved"], "claude-sonnet-5")
        self.assertEqual([row["session_id"] for row in wm.status(self.root)["sessions"]], ["s1"])
        self.assertTrue(legacy_file.is_file())

    def test_a_resume_of_an_older_entry_works_and_leaves_the_older_file_alone(self):
        legacy_file = self._legacy_copy()
        before = legacy_file.read_bytes()
        plan = wm.plan_resume(_req("resume", session_id="s1", request_id="r2"), harness="claude", root=self.root)
        self.assertEqual(plan["resolved"], "claude-sonnet-5")
        # The update was saved to the new folder, which now wins; the older file is untouched.
        self.assertTrue((wm.registry_dir(self.root) / "s1.json").is_file())
        self.assertTrue(wm.load_entry(self.root, "s1")["busy"])
        self.assertEqual(legacy_file.read_bytes(), before)

    def test_the_new_folder_wins_and_status_lists_each_session_once(self):
        self._legacy_copy("s1")
        wm.record_new(self.root, session_id="s1", harness="claude", req=_req("new", request_id="r9"),
                      plan=_plan("claude-sonnet-5", selection_kind="default"))
        wm.record_new(self.root, session_id="s2", harness="codex", req=_req("new"), plan=_plan("m2"))
        self.assertEqual(wm.load_entry(self.root, "s1")["created_request_id"], "r9")
        rows = wm.status(self.root)["sessions"]
        self.assertEqual([row["session_id"] for row in rows], ["s1", "s2"])

    def test_an_invalid_entry_in_the_older_folder_is_still_refused(self):
        legacy_file = self._legacy_copy()
        legacy_file.write_text("{not valid json at all", encoding="utf-8")
        self.assertIsNone(wm.load_entry(self.root, "s1"))


# ------------------------------------------------------------------------- status

class StatusTests(Base):
    def test_status_for_known_session(self):
        wm.record_new(self.root, session_id="s1", harness="claude", req=_req("new"),
                      plan=_plan("claude-sonnet-5", selection_kind="default"))
        result = wm.status(self.root, "s1")
        self.assertTrue(result["found"])
        self.assertEqual(result["resolved"], "claude-sonnet-5")

    def test_status_for_unknown_session(self):
        result = wm.status(self.root, "ghost")
        self.assertEqual(result, {"session_id": "ghost", "found": False})

    def test_status_lists_all_sessions(self):
        wm.record_new(self.root, session_id="s1", harness="claude", req=_req("new"), plan=_plan("m1"))
        wm.record_new(self.root, session_id="s2", harness="codex", req=_req("new"), plan=_plan("m2"))
        result = wm.status(self.root)
        self.assertEqual({row["session_id"] for row in result["sessions"]}, {"s1", "s2"})

    def test_cli_status_smoke(self):
        wm.record_new(self.root, session_id="s1", harness="claude", req=_req("new"), plan=_plan("m1"))
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = wm.main(["status", "--root", str(self.root), "--session", "s1"])
        self.assertEqual(code, 0)
        printed = json.loads(buf.getvalue())
        self.assertTrue(printed["found"])


if __name__ == "__main__":
    unittest.main()
