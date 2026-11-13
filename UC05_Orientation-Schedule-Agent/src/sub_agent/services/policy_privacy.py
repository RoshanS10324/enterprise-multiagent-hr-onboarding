"""Deterministic privacy classification of an HR policy question. Runs BEFORE retrieval and before any
model call, so a model can never override it (and never even sees a sensitive question).

Categories, in precedence order (the first that applies wins):

  injection              the question tries to instruct the assistant           -> refused
  salary_other           asks about another person's pay or employment terms    -> refused, logged privately
  salary_own             asks about / renegotiates the asker's own pay or terms  -> private HR escalation
  accessibility          a disability / accessibility / workplace-adjustment request
                                                                                 -> private HR escalation + neutral plan item
  medical                mentions a medical condition, diagnosis or treatment    -> private HR escalation
  unsupported_language   written in a language not enabled for policy Q&A        -> HR escalation
  general                an ordinary policy question                             -> retrieval + grounded answer

A general question about pay POLICY ("When is salary paid?", "How is gratuity calculated?",
"How many days of sick leave do I get?") is a policy question, not a privacy matter: the document
covers it. Only questions about a specific person's pay, terms or health are diverted.

Matching is keyword- and pattern-based (English, plus Arabic keywords), case-insensitive, on
Arabic-normalised text. It errs towards escalation: a false positive sends an answerable question to
HR; a false negative is what must not happen. The classification carries only the category and the
matched rule names -- never the matched words -- so it is safe to log.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from hr_onboarding_common.platform.guardrails import detect_injection, normalize_arabic

SENSITIVE = frozenset({"salary_own", "salary_other", "medical", "accessibility"})
ESCALATED = frozenset({"salary_own", "medical", "accessibility", "unsupported_language"})

# Pay NOUNS ("salary", "bonus") -- not "paid", which ordinary policy questions use ("Is sick leave paid?").
_PAY = (r"(?:salary|salaries|pay|wage|wages|compensation|remuneration|package|ctc|earnings|bonus|increment|"
        r"raise|hike|allowance|allowances|earn|earns|earning)")
_TERMS = r"(?:employment terms|contract terms|terms and conditions|terms of employment|employment contract|offer letter|contract)"
_THIRD = (r"(?:\b(?:his|her|their|someone|somebody|another|other|colleague|colleagues|co-?worker|team ?mate|"
          r"teammates|new (?:employee|hire|joiner|starter)|that employee|this employee)\b"
          r"|\b(?!my\b|our\b|the\b|a\b|company\b|organi[sz]ation\b|employer\b|today\b|year\b|month\b)[a-z]+'s\b)")

_RULES: dict[str, list[tuple[str, re.Pattern]]] = {
    "salary_other": [
        ("third_party_pay", re.compile(rf"{_THIRD}[^.?!]{{0,40}}\b{_PAY}\b|\b{_PAY}\b[^.?!]{{0,20}}\bof\b[^.?!]{{0,10}}{_THIRD}", re.I)),
        ("third_party_terms", re.compile(rf"{_THIRD}[^.?!]{{0,30}}\b{_TERMS}\b", re.I)),
        ("how_much_does_someone_earn", re.compile(r"\bhow much (?:does|do|is|are)\b(?!\s+(?:i|we)\b)[^.?!]{0,40}\b(?:earn|earning|make|making|paid|get paid)\b", re.I)),
        ("ar_others_salary", re.compile(r"راتب\s+(?:زميل|زميلي|الموظف الجديد|المدير|غيري)")),
    ],
    "salary_own": [
        ("own_pay", re.compile(rf"\b(?:my|mine)\b[^.?!]{{0,25}}\b{_PAY}\b|\b{_PAY}\b[^.?!]{{0,12}}\bmine\b", re.I)),
        ("ask_raise", re.compile(r"\b(?:get|want|need|ask for|request|deserve|due for)\s+(?:a\s+)?(?:raise|hike|increment|pay rise)\b",
                                 re.I)),
        ("negotiate", re.compile(r"\b(?:re-?negotiat\w*|negotiat\w*|counter[- ]?offer)\b", re.I)),
        ("change_compensation", re.compile(rf"\b(?:increase|raise|change|revise|adjust|improve|review)\b[^.?!]{{0,25}}\b(?:{_PAY}|{_TERMS})\b", re.I)),
        ("how_much_will_i", re.compile(r"\bhow much (?:will|do|am|would|can|should)\s+i\b[^.?!]{0,30}\b(?:earn|get paid|be paid|make|receive)\b", re.I)),
        ("ar_salary", re.compile(r"راتبي|رواتب|راتب|الاجر|اجري|تفاوض")),
    ],
    "accessibility": [
        ("accessibility", re.compile(r"\b(?:accessib\w*|wheel ?chair|disab\w*|impair\w*|handicap\w*|assistive|screen reader|sign language|ramp|hearing aid|mobility aid|braille)\b", re.I)),
        ("adjustment", re.compile(r"\b(?:reasonable|workplace|work ?station|ergonomic)\s+(?:adjustment|accommodation|adaptation)s?\b|\baccommodations? for\b", re.I)),
        ("ar_accessibility", re.compile(r"اعاقه|ذوي الهمم|كرسي متحرك|احتياجات خاصه")),
    ],
    "medical": [
        ("condition", re.compile(
            r"\b(?:diabet\w*|cancer|tumou?r|chemo\w*|asthma|epilep\w*|seizure\w*|hiv|aids|hepatitis|covid|"
            r"pregnan\w*|miscarriage|depress\w*|anxiety|panic attacks?|bipolar|schizo\w*|adhd|autis\w*|"
            r"ptsd|mental health|heart (?:condition|disease|attack)|stroke|surgery|operation on|"
            r"diagnos\w*|chronic|disease|illness|disorder|migraine\w*|injur\w*|fracture\w*|therapy|"
            r"medication|prescription|hospitali[sz]\w*|blood pressure|hypertension|kidney|dialysis|"
            r"allerg\w*|flu|fever|infection|syndrome|condition)\b", re.I)),
        ("first_person_health", re.compile(
            r"\bi(?:'m| am| was| have been| have| had| got)\b[^.?!]{0,25}\b(?:sick|ill|unwell|admitted|hospitali[sz]ed|"
            r"pregnant|diagnosed|suffering|recovering|undergoing|treated)\b", re.I)),
        ("my_health", re.compile(r"\bmy\s+(?:doctor|treatment|medical (?:condition|history|report|records?)|health|"
                                 r"symptoms?|disability|diagnosis|therapist)\b", re.I)),
        ("ar_medical", re.compile(r"مرض|مريض|حامل|تشخيص|علاج|سكري|سرطان|عمليه جراحيه")),
    ],
}
# Policy topics that contain medical-sounding words but are ordinary policy questions.
_POLICY_PHRASES = re.compile(r"\b(?:medical insurance|medical certificates?|sick leave|planned medical treatment|"
                             r"long[- ]term absence|government health authority)\b", re.I)

_ARABIC = re.compile(r"[؀-ۿݐ-ݿ]")
_OTHER_SCRIPT = re.compile(r"[Ѐ-ӿ֐-׿ऀ-ॿঀ-৿஀-௿぀-ヿ一-鿿가-힯]")
_ENGLISH_HINTS = frozenset("""the a an is are am do does can could how what when where why which who i my me we you your
of to in for on with at by from and or not will would should may must have has get leave days""".split())


@dataclass(frozen=True)
class Classification:
    category: str
    language: str
    rules: tuple[str, ...] = field(default_factory=tuple)

    @property
    def sensitive(self) -> bool:
        return self.category in SENSITIVE

    @property
    def escalate(self) -> bool:
        return self.category in ESCALATED


def detect_language(text: str) -> str:
    """'ar' for Arabic script, 'en' for Latin text with English function words, else 'und' (not
    reliably detected) -- never a guess beyond that."""
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return "und"
    arabic = sum(1 for c in letters if _ARABIC.match(c))
    if arabic / len(letters) > 0.3:
        return "ar"
    if _OTHER_SCRIPT.search(text):
        return "und"
    words = re.findall(r"[a-z']+", text.lower())
    if words and all(c.isascii() for c in letters) and sum(1 for w in words if w in _ENGLISH_HINTS) >= 1:
        return "en"
    return "und"


def classify_question(question: str, *, enabled_languages: tuple[str, ...] | list[str] = ("en",),
                      asks_about_other_employee: bool = False) -> Classification:
    language = detect_language(question)
    if detect_injection(question):
        return Classification("injection", language, ("injection",))
    text = normalize_arabic(question).replace("’", "'")
    without_policy_phrases = _POLICY_PHRASES.sub(" ", text)

    def hits(category: str, source: str) -> tuple[str, ...]:
        return tuple(name for name, rx in _RULES[category] if rx.search(source))

    other = hits("salary_other", text)
    own = hits("salary_own", text)
    pay_topic = bool(re.search(rf"\b{_PAY}\b|\b{_TERMS}\b|راتب", text, re.I))
    if other or (asks_about_other_employee and (own or pay_topic)):
        return Classification("salary_other", language, other or ("asked_for_another_employee",))
    if own:
        return Classification("salary_own", language, own)
    access = hits("accessibility", text)
    if access:
        return Classification("accessibility", language, access)
    medical = hits("medical", without_policy_phrases)
    if medical:
        return Classification("medical", language, medical)
    if language not in set(enabled_languages):
        return Classification("unsupported_language", language, ("language_not_enabled",))
    return Classification("general", language)
