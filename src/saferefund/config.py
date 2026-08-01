"""Static settings for the toy application."""

from decimal import Decimal
from typing import Final

DATABASE_URL: Final = "sqlite:///./saferefund.db"
REFUND_APPROVAL_THRESHOLD: Final = Decimal("500.00")
APPROVAL_TTL_SECONDS: Final = 900
VERIFICATION_TTL_SECONDS: Final = 900
MAX_AGENT_STEPS: Final = 12
MAX_INVALID_OUTPUTS: Final = 3
MODEL_CALL_TIMEOUT_SECONDS: Final = 30.0
DENIAL_LOOP_THRESHOLD: Final = 3
UNTRUSTED_FIELD_MAX_CHARS: Final = 2000
INVALID_OUTPUT_PREVIEW_CHARS: Final = 200
VERIFICATION_SUBJECT: Final = "Verify your email address"
VERIFICATION_BODY: Final = "Use this verification token to continue: {token}"
UNKNOWN_SENDER_SUBJECT: Final = "We could not find your account"
UNKNOWN_SENDER_BODY: Final = (
    "Please contact support using the email address on your account."
)
