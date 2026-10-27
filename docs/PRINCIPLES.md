# EUTONOS guiding principles

Status: owner-selected guiding statements, recorded 2026-09-04. The explanations and decision tests below are proposed operating interpretations. This document owns enduring guiding values and design priorities. [Product Goal](PRODUCT_GOAL.md) owns intended product outcomes and first-version scope. Neither owns task authorization, implementation status or source truth.

## Connect the parts with less wasted effort

> EUTONOS connects people, agents, tools, and knowledge so less effort is wasted finding context, repeating work, and keeping everything aligned.

People and agents are both participants, not merely users of a document database. Judge the system by useful outcomes and the attention it gives back, not only token counts, numbers of records, or installed modules. A smaller request that produces a worse result is not an improvement.

## Reuse established understanding

> The hub’s most valuable intelligence may be knowing what not to make a model figure out again.

Capture useful work once and make it reusable. Source-backed decisions, procedures, failed approaches and unresolved questions should survive a session change. Reuse is conditional on scope, source state and the claim being made. This is not permission to skip necessary source inspection, independent verification or a changed constraint.

## Remember at the right scope, under owner control

> remember what matters at the right level, preserve a reliable route to the source, and let the owner correct or forget it.

Keep detail at the lowest useful scope and expose bounded summaries or references upward. Do not interpret this as collecting everything forever, copying information into every level, or allowing every agent to read it. Memory is useful only when its provenance, limitations and owner controls are understandable.

## Consequences for design

**Choose work that advances the owner's goals.** Make the intended outcome and scope explicit. Help the owner assess worthwhile next work, defer tangents with a reason and reconsideration trigger, and deliberately revise goals when evidence warrants it. Recommendations do not silently change priorities, admitted scope or permission.

**Capture once; derive the views.** The same reviewed record should support navigation, context preparation and progress views without becoming several independently maintained stories. Current task, evidence, memory and permission retain different owners.

**Small tasks stay small.** A known source should be directly reachable. Everyday work should require little EUTONOS administration, repeated explanation or owner intervention. No agent, graph, MCP connection or language feature earns a mandatory place in a simple path merely because it exists. TokenSlang is a means of sharing meaning, not an end in itself.

**Help people finish and ship.** Explain the next useful action in ordinary language, perform predictable mechanics under the existing authority, and make saved, integrated, published and deployed state understandable. A framework that completes its own records while letting finished product work accumulate indefinitely has missed a user outcome. Milestone guidance should reduce uncertainty, not introduce a prompt after every small edit.

**Less context must not mean less truth.** Preserve required constraints, disagreements, unknowns and coverage boundaries. A relevant uncertainty is more useful than a confidently compressed mistake.

**Maintenance must earn its cost.** Prefer bounded mechanical capture and affected-record updates. Count model use, maintenance, retries, setup, mistakes, human interruptions and recovery against the work saved. Judge correct delivered results, reliable recovery, time and attention returned before optimizing detailed usage counters. Do not replace handoff writing with equally expensive memory administration.

**The owner keeps control and an exit.** Make selection, source, freshness and important side effects explainable on request. Correction, scoped remembering, suppression, export and removal need defined outcomes. Report what could not be changed or erased; never promise that deleting a memory retracts prior exports.

**A module failure stays local.** Preserve readable source and safe partial operation when an optional component is missing or stale. Missing authority still blocks the affected action; unrelated maintenance must not make the whole system unusable.

## The design decision test

Before adding a mandatory step, record or module, ask:

1. Does this work advance the owner's intended outcome within the admitted scope?
2. Which recurring human or agent effort does this remove?
3. Can existing source, a direct reference or deterministic code do it more simply?
4. What correctness, safety or user control must remain intact?
5. What will maintaining and recovering this cost, including attention?
6. Can the owner inspect, correct, disable or leave it without losing their work?

If the benefit is still speculative, keep it optional and test it rather than expanding the default path.

[Architecture](ARCHITECTURE.md) owns construction. [Operations](OPERATIONS.md) owns acceptance and experience-gap checks. Native work owners, located through [Shared Current Work](DEPLOYMENT_STARTUP.md#shared-current-work), own admitted assignments and live status. These principles do not admit implementation or expand any agent's authority.
