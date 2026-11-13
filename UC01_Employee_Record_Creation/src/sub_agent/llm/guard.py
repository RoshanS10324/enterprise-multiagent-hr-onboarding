"""The check that runs before any prompt is built.

Redaction here is structural, not pattern-matching. A request carries
named inputs; the task spec declares which names are allowed; anything
else is refused. That is why salary cannot reach a model: no task
declares `salary_and_terms`, so passing it is an error rather than
something a regex has to notice.

The second check catches the subtler case -- a value that is a
`SecretStr`, or a stringified one. `OnboardingForm.salary_and_terms` is
a `SecretStr`, so `str()` on it yields "**********" rather than the
figure; a caller doing that is not leaking, but they are sending
noise to a model, and it signals a wiring mistake worth failing on.
"""

from re import compile as re_compile

from pydantic import SecretStr

from .base import LlmInputRefused, LlmTaskSpec

# What `str(SecretStr(...))` renders as. Seeing this in an input means a
# caller tried to pass a secret and pydantic masked it on the way.
_MASKED = "**********"

# Field names that must never be sent to a model, whatever a task spec
# says. A task that declares one of these fails at load time, so this is
# belt and braces rather than the only line of defence.
FORBIDDEN_INPUT_NAMES = frozenset(
    {
        "salary",
        "salary_and_terms",
        "compensation",
        "compensation_terms",
        "bank",
        "bank_account",
        "iban",
        "loan",
        "loan_details",
    }
)

# Identity and contact fields. Never sent to a model either: an advisory
# task needs to know WHICH signal matched, never the person's details.
# Kept separate from FORBIDDEN_INPUT_NAMES because that set mirrors the
# audit log's forbidden keys (audit_integrity.FORBIDDEN_AUDIT_KEYS), and
# the audit log does legitimately name some of these structurally.
PERSONAL_INPUT_NAMES = frozenset(
    {
        "id_number",
        "national_id",
        "emirates_id",
        "candidate_name",
        "full_name",
        "name",
        "candidate_email",
        "email",
        "phone",
        "mobile",
        "date_of_birth",
        "dob",
        "address",
        "nationality",
    }
)

# An email address inside any input value (e.g. pasted into a subject).
_EMAIL_VALUE_RE = re_compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# Nine or more digits in a row (separators allowed): a phone, ID or account
# number pasted into free text. UUIDs (case ids) are exempt.
_NUMBER_RUN_RE = re_compile(r"(?:\+?\d[\s-]?){9,}")
_UUID_RE = re_compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


def _carries_personal_value(value: str) -> bool:
    if _EMAIL_VALUE_RE.search(value):
        return True
    return bool(_NUMBER_RUN_RE.search(_UUID_RE.sub("", value)))


def check_task_spec(spec: LlmTaskSpec) -> None:
    """Reject a task that declares a forbidden input. Called at load
    time, so a bad brain/ file fails the suite rather than shipping."""
    forbidden = set(spec.inputs_allowed) & (FORBIDDEN_INPUT_NAMES | PERSONAL_INPUT_NAMES)
    if forbidden:
        raise LlmInputRefused(spec.task, sorted(forbidden))


def check_inputs(spec: LlmTaskSpec, inputs: dict[str, str]) -> None:
    """Reject undeclared names, forbidden or personal names, values that
    contain an email address or a long digit run, and masked secrets.

    Raises LlmInputRefused naming the offending KEYS only. Values are
    never included in the message.
    """
    offending = set(inputs) - set(spec.inputs_allowed)
    offending |= set(inputs) & (FORBIDDEN_INPUT_NAMES | PERSONAL_INPUT_NAMES)
    offending |= {
        name for name, value in inputs.items() if isinstance(value, str) and _carries_personal_value(value)
    }
    # The isinstance branch is unreachable via LlmRequest, whose
    # `dict[str, str]` annotation makes pydantic reject a raw SecretStr
    # first. It is kept for callers that reach check_inputs directly.
    # The _MASKED branch is the one that fires in practice: a secret
    # that was str()-ed on the way in is an ordinary string by then.
    offending |= {
        name
        for name, value in inputs.items()
        if isinstance(value, SecretStr) or (isinstance(value, str) and _MASKED in value)
    }

    if offending:
        raise LlmInputRefused(spec.task, sorted(offending))


def build_prompt(spec: LlmTaskSpec, inputs: dict[str, str]) -> str:
    """Assemble the prompt after the guard has passed.

    Only declared inputs are rendered, in sorted order so the same
    request always produces byte-identical text -- which is what makes
    the fake client's output reproducible in evals.
    """
    check_inputs(spec, inputs)
    rendered = "\n".join(f"{name}: {inputs[name]}" for name in sorted(inputs))
    return f"{spec.prompt.rstrip()}\n\n{rendered}\n"
