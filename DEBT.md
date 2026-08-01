# Remaining debt

The README states this branch's deliberately small operating envelope. The
remaining work is the production machinery intentionally omitted from the demo:

- **Durable effects.** Payment, mail, and ticketing run before the surrounding
  transaction commits. A crash can leave an external effect without its matching
  database record; production needs durable intent and an outbox/reconciliation path.
- **Distributed correctness.** The process-local lock does not coordinate workers
  or hosts. Production needs database-level concurrency control and target-dialect
  contention tests.
- **Durable lifecycle history.** Refund approval and expiry are mutable-row,
  lazy workflows. There is no approved-but-unpaid recovery state, append-only event
  history, or replayable projection.
- **Harder enforcement boundary.** `service.run_agent_action` is the supported
  gate, but Python application code can still call private helpers. Production
  needs a capability or process boundary if untrusted code—not merely model
  response text—enters the process.
