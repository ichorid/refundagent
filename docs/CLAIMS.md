# Claims registry

This is the single index of normative claims made about this repository. It exists so checking
whether documentation still matches the code is a diff against this table instead of an
open-ended re-read of `README.md`, `docs/ARCHITECTURE.md`, and `DEBT.md`.

Two kinds of row:

- **`enforced`** (and `enforced, version-pinned`) — the claim has a mechanism in source and
  a guard test that fails if the mechanism and the claim drift apart. The guard test column
  is a pytest node id (`path::function`).
- **`convention`** — the claim is a documented limitation with no runtime guard. Every
  `convention` row must have a matching bullet in `DEBT.md`; that bullet *is* the claim's
  only enforcement (a human reads it before relying on the property it doesn't have). The
  guard test column for these rows is the exact substring quoted from `DEBT.md` that a
  reader can search for.

`tests/invariants/test_claims_registry_integrity.py` checks both properties mechanically:
every `enforced` guard test must exist as a real function in the named module, and every
`convention` quote must appear verbatim in `DEBT.md`. It does not check that the claim is
*true* — that's still what the underlying guard test (for `enforced` rows) or human judgment
(for `convention` rows) is for. It only checks that the pointer is live, so a renamed test or
a rewritten `DEBT.md` bullet turns red immediately instead of silently going stale until the
next manual audit.

This table is seeded from the claim-specific checks that already existed in
`tests/invariants/test_documentation_drift_guards.py` (the `enforced` rows are a direct
listing of those checks) plus every bulleted item in `DEBT.md` (the `convention` rows). It is
not yet a line-by-line audit of every `always`/`never`/`only`/`cannot`/`exactly`/
`guarantee` sentence in `README.md` and `docs/ARCHITECTURE.md` — see "Known gaps" below.

## Enforced claims

| ID | Claim | Mechanism | Guard test | Boundary |
|---|---|---|---|---|
| CLM-001 | Orders are invisible in the prompt/state until an authorized `orders_listed` event exists | `src/saferefund/agent/prompt.py::disclosed_order_ids` | `tests/invariants/test_documentation_drift_guards.py::test_documented_order_disclosure_requires_orders_listed_event` | enforced |
| CLM-002 | The documented model trust boundary (trusted transport / untrusted response bytes) matches `ModelGateway` | `src/saferefund/agent/gateway.py::ModelGateway` | `tests/invariants/test_documentation_drift_guards.py::test_documented_model_trust_boundary_matches_model_gateway` | enforced |
| CLM-003 | The model boundary owns runtime type validation and parsing, not `model.propose()` callers | `src/saferefund/agent/model_boundary.py::invoke_model_boundary` | `tests/invariants/test_documentation_drift_guards.py::test_documented_model_boundary_owns_type_validation_and_parsing` | enforced |
| CLM-004 | Documented immutable refund-intent fields match the enforced field set | `src/saferefund/domain/tables.py::_REFUND_INTENT_FIELD_NAMES` | `tests/invariants/test_documentation_drift_guards.py::test_documented_refund_intent_immutability_matches_enforcement` | enforced |
| CLM-005 | Every scenario cited as an "exact sequence" actually uses the shared exact-sequence assertion helpers | `tests/support` sequence helpers (`assert_exact_event_type_sequence` and siblings) | `tests/invariants/test_documentation_drift_guards.py::test_documented_exact_sequence_scenarios_use_sequence_helpers` | enforced |
| CLM-006 | Future-module adapter-import scanning is exhaustive static coverage; import-linter alone is not called a security boundary | `tests/invariants/adapter_import_scanner.py::scan_all_non_gate_production_modules` | `tests/invariants/test_documentation_drift_guards.py::test_documented_future_module_adapter_scanning_is_exhaustive` | enforced |
| CLM-007 | Relational scope (customer/case/order ownership) is validated at event append, not only via independent foreign keys | `src/saferefund/repositories/relational_scope.py::validate_event_relational_scope` | `tests/invariants/test_documentation_drift_guards.py::test_documented_relational_scope_checks_exist` | enforced |
| CLM-008 | PostgreSQL concurrency claims name `tests/postgres` as the evidence and the exact tested PostgreSQL version; the claim does not generalize past it | `tests/postgres/conftest.py::TESTED_POSTGRESQL_VERSION` | `tests/invariants/test_documentation_drift_guards.py::test_documented_postgresql_concurrency_points_to_postgres_tests` | enforced, version-pinned |
| CLM-009 | Claims about external effects (mail, ticketing) name a crash window or a transactional-outbox guarantee; no unqualified "atomic" claim | `DEBT.md` effect-first crash-window bullet | `tests/invariants/test_documentation_drift_guards.py::test_external_effect_claim_names_crash_windows_or_outbox_guarantee` | enforced |
| CLM-010 | Documented `case_closed.outcome` values match the `CaseOutcome` enum exactly | `src/saferefund/domain/enums.py::CaseOutcome` | `tests/invariants/test_documentation_drift_guards.py::test_documented_case_outcomes_match_the_enum` | enforced |
| CLM-011 | Documented gate façade signatures (§9) match the live function signatures | `src/saferefund/gate/__init__.py` façade operations | `tests/invariants/test_documentation_drift_guards.py::test_documented_facade_signatures_match_the_code` | enforced |
| CLM-012 | Documented import-linter contract list (§16) matches `.importlinter` in order | `.importlinter` | `tests/invariants/test_documentation_drift_guards.py::test_documented_import_contracts_match_the_importlinter_file` | enforced |
| CLM-013 | Invariant module docstrings never restate a resolved defect as a current one | `tests/invariants/*` narrative modules | `tests/invariants/test_documentation_drift_guards.py::test_invariant_module_narratives_do_not_assert_resolved_defects_are_current` | enforced |

### Cross-references from README's "Invariants and guarantees" table

`README.md` carries its own curated, reviewer-facing claims table. These rows register that
table's entries that were not already covered above (order disclosure, model trust boundary,
and PostgreSQL concurrency are the same claims as CLM-001/002/003/008 and are not repeated).
Each guard test below was read in full before citing, to confirm it asserts the specific
property claimed, not just a nearby one.

| ID | Claim | Mechanism | Guard test | Boundary |
|---|---|---|---|---|
| CLM-050 | Free text (including adversarial prompt injection) never decides ownership, amount, verification, approval, or lifecycle state | `src/saferefund/projections/` (pure fold: seed rows + validated events only, §2.2) | `tests/invariants/test_injection_evidence_causality.py::test_a_prompt_obedient_model_still_cannot_obtain_a_refund` | enforced |
| CLM-051 | The model cannot express cross-principal effects: no action carries a customer id or reply recipient, and only `link_order` carries an `order_id` | `src/saferefund/actions/models.py` action union | `tests/unit/test_action_structure.py::test_no_identity_or_recipient_fields_on_action_models` | enforced |
| CLM-052 | `CaseSummary` folds only its own case's events; events belonging to another case never influence it | `src/saferefund/projections/case.py` | `tests/unit/test_case_projection.py::test_case_projection_ignores_customer_mismatched_case_events` | enforced |
| CLM-053 | Inbound delivery is idempotent per `(customer_id, opening_message_id)`; a reused message id from another sender cannot resume or disclose another customer's case | `src/saferefund/api/routes.py` inbound case resolution | `tests/invariants/test_inbound_dedup_customer_scope.py::test_reused_message_id_cannot_resume_another_customers_open_case` | enforced |
| CLM-054 | Policy is fail-closed: an action type absent from `ACTION_OBLIGATIONS` is denied `R_EXHAUSTED`, never silently allowed | `src/saferefund/policy/policy.py::evaluate` | `tests/invariants/test_policy_fail_closed_coverage.py::test_exhausted_denial_is_reachable_for_uncovered_action_type` | enforced |
| CLM-055 | The cumulative refund threshold reads order history across cases, so opening a new case does not reset it | `src/saferefund/projections/order.py::OrderSummary` | `tests/integration/test_refund_lifecycle.py::test_cumulative_threshold_survives_new_case_on_same_order` | enforced |
| CLM-056 | Operator approval is one-shot: under contention, only one competing approve/reject can win and payment is called at most once | `src/saferefund/gate/operator.py` | `tests/invariants/test_approval_one_shot_concurrency.py::test_second_approver_cannot_reuse_a_stale_pending_status` | enforced |
| CLM-057 | The approval window is the half-open interval `[created_at, approval_expires_at)`; `approval_expires_at` is the first instant approval is refused | `src/saferefund/repositories/refunds.py::approval_window_is_open` | `tests/invariants/test_approval_expiry_boundary.py::test_refund_is_expired_at_exactly_its_expiry_instant` | enforced |
| CLM-058 | Money has durable intent before payment: `refund_proposed`/`refund_auto_approved` are recorded before `refund_executed`, which is appended only after the payment adapter succeeds | `src/saferefund/gate/refund.py` | `tests/integration/test_refund_lifecycle.py::test_happy_path_small_refund` | enforced |
| CLM-059 | External effects are gate-mediated: the gate façade exports exactly the mediated operations, and an unmediated adapter call from outside the gate is rejected | `src/saferefund/gate/__init__.py` | `tests/invariants/test_gate_layering_and_mediation.py::test_gate_facade_exports_exactly_the_mediated_operations` | enforced |
| CLM-060 | Cases cannot loop forever on model failure: the agent step limit and the invalid-output (parse-failure) limit each force escalation and closure | `src/saferefund/config.py::MAX_AGENT_STEPS`, `MAX_INVALID_OUTPUTS` | `tests/integration/test_termination.py::test_step_limit` | enforced |
| CLM-061 | Verification is customer-wide, not case-local: successful verification resumes every open case for that customer, not only the case that requested it | `src/saferefund/gate/__init__.py::confirm_verification` | `tests/integration/test_verification.py::test_verification_unblocks_all_cases` | enforced |
| CLM-062 | Seed rows and events are append-only at the ORM boundary: an `UPDATE`/`DELETE` through a SQLAlchemy session raises `ImmutableRowError` | ORM `before_update`/`before_delete` listeners (§4.7) | `tests/invariants/test_append_only_immutability.py::test_updating_a_persisted_event_is_rejected` | enforced |

## Convention claims (documented, not runtime-guarded)

Every row below is a `DEBT.md` bullet. The "Guard test" column is the exact substring to
search for in `DEBT.md`; `test_claims_registry_integrity.py` asserts it is present verbatim.

| ID | Claim | Guard test (`DEBT.md` substring) | Boundary |
|---|---|---|---|
| CLM-020 | The €500 auto-approval threshold is scoped per order, not per customer, so ten orders can each clear €499 with no human involved | `The five-hundred threshold is per order` | convention |
| CLM-021 | There is no periodic sweeper for expired `pending_approval` refunds; expiry only fires when ordinary traffic touches the customer | `No periodic approval-expiry sweeper` | convention |
| CLM-022 | The operator approval view shows a refund id, order, and amount but not the case's reasoning or customer message | `The operator approves blind` | convention |
| CLM-023 | Nothing records the model's stated rationale for proposing a refund | `Nothing records why a refund was proposed` | convention |
| CLM-024 | The agent's only lever is a full refund; no partial refund, replacement, or store credit | `A refund is the only thing this agent can do` | convention |
| CLM-025 | Reply text content is not governed by the gate; only the decision to send is | `The reply text is not governed at all` | convention |
| CLM-026 | Escalation without a prior reply leaves the customer with no acknowledgement | `Escalation is a dead end for the customer` | convention |
| CLM-027 | Every inbound email opens a new case; there is no cross-case conversational memory | `Every email opens a new case` | convention |
| CLM-028 | Verification is an unbound bearer token with no rate limiting or entropy requirement | `Verification is a bearer token with no binding` | convention |
| CLM-029 | Customer identity rests entirely on the envelope sender; no SPF/DKIM/DMARC | `Identity rests entirely on the envelope sender` | convention |
| CLM-030 | Action labels (including `INTERNAL_SIDE_EFFECT`) are static, not payload-derived | `Labels are static` | convention |
| CLM-031 | Future adapter-import scanning is exhaustive static repository coverage, not a runtime/process security boundary | `Future adapter imports are statically scanned, not runtime-enforced` | convention |
| CLM-032 | Model worker egress is not OS-enforced in this repository; only serialized response bytes are the untrusted boundary | `Model worker egress is not OS-enforced here` | convention |
| CLM-033 | The model boundary terminates on model/protocol failure; infrastructure exceptions after a valid parsed action still propagate and can leave a case resumable | `The model boundary closes the case; infrastructure failures do not` | convention |
| CLM-034 | Named request/prompt limits bound storage growth at the cost of eliding legitimate very-large histories | `Named request and prompt limits trade audit fidelity for bounded storage` | convention |
| CLM-035 | Mailer/ticketing effects are not atomic with their audit events (effect-first crash window) | `Mailer and ticketing effects are not atomic with their audit events` | convention |
| CLM-036 | The payment call happens after commit; a crash between the two leaves an `approved` refund with no `refund_executed` event, recoverable only via `refund_id` as an idempotency key | `Money can move without a record` | convention |
| CLM-037 | The audit log stores raw email bodies, reply text, and verification tokens with no redaction, retention limit, or access control | `The audit log is also the leak` | convention |
| CLM-038 | Append-only guarantees on seeds/events/refund-intent are ORM-boundary only; raw SQL, another process, or a migration can bypass them; `events.case_id` tamper to another same-customer case is not rejected at append time | `Append-only seeds, events, and refund intent have layered but incomplete guarantees` | convention |
| CLM-039 | Projections are replayed in full on every action; no incremental projection exists for long-lived customers | `Projections are replayed in full on every action` | convention |
| CLM-040 | Single-flight is one in-process lock per case and breaks with more than one worker process | `Single-flight is one in-process lock per case` | convention |
| CLM-041 | The customer-row `FOR UPDATE` lock only serializes policy evaluation on PostgreSQL; SQLite parses and ignores it | `SQLite parses and ignores that lock` | convention |
| CLM-042 | The policy list is a single closed ordered list, correct only while there is exactly one tenant and one rule set | `The closed ordered policy list in one function is the right call while there is exactly one policy` | convention |

## Known gaps

- This registry does not yet enumerate every strong sentence in `README.md` and
  `docs/ARCHITECTURE.md` individually — it enumerates every *mechanism* that
  `test_documentation_drift_guards.py` already checks, plus every `DEBT.md` bullet. A
  README/architecture sentence with no corresponding row here has not yet been through the
  registry process; close the gap by re-reading `always`/`never`/`only`/`cannot`/`exactly`/
  `structural`/`guarantee` statements by hand until each one is added as a row.
- Adding a new load-bearing claim to the docs should come with a new row here in the same
  change, `enforced` with a real guard test if a mechanism exists, otherwise `convention`
  with a new `DEBT.md` bullet. `test_claims_registry_integrity.py` will not catch a claim
  that was never added to this table — it only catches drift in claims that were.
