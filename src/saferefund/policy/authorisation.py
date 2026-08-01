"""Single-use capability evidence that one proposal cleared policy."""

from dataclasses import dataclass, field

from saferefund.actions.models import Action
from saferefund.policy.checks import ObligationId


class AuthorisationError(RuntimeError):
    """Raised when authorisation is missing, mismatched, or reused."""


@dataclass(slots=True)
class Authorisation:
    """Evidence that one specific proposal was permitted by one specific evaluation.

    Rung 5 on the mediation ladder (see ARCHITECTURE.md §9): constructible in-process,
    catches accidental bypass and makes deliberate bypass review-visible; not a process
    security boundary.
    """

    case_id: str
    action: Action
    obligations_discharged: frozenset[ObligationId]
    _spent: bool = field(default=False, repr=False)

    def spend(self, *, case_id: str, action: Action) -> None:
        """Consume this capability for exactly one effect on the bound proposal."""
        if self._spent:
            message = "authorisation already spent"
            raise AuthorisationError(message)
        if case_id != self.case_id or action is not self.action:
            message = "authorisation does not match this effect"
            raise AuthorisationError(message)
        self._spent = True


__all__ = ["Authorisation", "AuthorisationError"]
