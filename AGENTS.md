# AGENTS.md

Contributor guidelines for this repository. This is not architecture — that's
`docs/ARCHITECTURE.md`. This is not the claims list — that's `docs/CLAIMS.md`. This is how
to work here.

## Verification means running the check, not recalling it

Run the full gate before considering a change done:

```bash
make check
```

Quote the real, unedited output in the commit body when documenting verification. A restated
summary ("mypy passed", "tests green") is not evidence — it is a claim about evidence. A
`Stop` hook is configured to run `make check` automatically and surface its captured output;
quote that output, not a recollection of the run.

## Never weaken a proof to match existing behavior

If a fix requires relaxing an assertion in `tests/invariants/`, or narrowing a claim in
`README.md`, `docs/ARCHITECTURE.md`, `docs/CLAIMS.md`, or `DEBT.md`, stop instead of doing
it silently. Either fix the underlying behavior so the existing claim stays true, or:

1. add/update the row in `docs/CLAIMS.md` for the claim being narrowed;
2. move it to `convention` with a new or updated `DEBT.md` bullet if no mechanism will
   enforce it;
3. only then edit the doc text, in the same change as the `docs/CLAIMS.md` row.

Do not just rewrite the prose and move on — that is exactly the "convention-only" drift the
claims registry exists to catch.

## Show the guard test red before the fix, not just green after

For any new or changed invariant/regression test, run it against the pre-fix code first and
capture the failure. A test that has never been observed to fail is not known to test
anything.

## Adding a new load-bearing claim

Every claim added to `README.md` or `docs/ARCHITECTURE.md` gets a row in `docs/CLAIMS.md` in
the same change: `enforced` with a real guard-test pointer if a mechanism exists, otherwise
`convention` with a matching `DEBT.md` bullet.
`tests/invariants/test_claims_registry_integrity.py` only catches drift in claims that are
already rows — it cannot catch a claim that was never added.
