# HR policy knowledge: the real policy document

The HR policy assistant answers only from one document: `HR-Onboarding-Common/src/hr_onboarding_common/context_layer/HR_Policy_Consultants.md`. This page covers how that document is loaded, searched and cited, and how to update it.

The privacy rules and HR escalation are in [HR_POLICY_PRIVACY_AND_ESCALATION.md](HR_POLICY_PRIVACY_AND_ESCALATION.md). What changed and how it was tested is in [HR_POLICY_IMPLEMENTATION_REPORT.md](HR_POLICY_IMPLEMENTATION_REPORT.md).

## 1. The source

**The document.** It is the employer's own HR policy, converted from "HR Policy Consultants.pdf" (10 pages):
- 28 policy sections under `##` and `###` headings
- `<!-- Page N -->` markers showing where each PDF page begins
- one table (leave during probation)

**No fallback.** It is the only source of policy content. The synthetic per-client policies that UC05 used before (`context_layer/knowledge/policies/`, for ACME, GLOBEX and "global") have been deleted. If the document cannot be loaded, no question is answered from anything else; the question goes to HR (section 6).

**One policy for every tenant.** The document names no client and covers "Enterprise employees", so it applies to every tenant. Searches still receive the tenant's `client_id`, so a client-specific policy could be added later without changing callers. No client-specific policy exists today.

## 2. Loading and packaging

The loader is `hr_onboarding_common.context_layer.hr_policy` (`load_policy_document`, `cached_policy_document`). It looks for the document in this order:

1. an explicit path passed by the caller (tests, or a reviewed copy mounted on a server);
2. the `HR_POLICY_SOURCE_PATH` environment variable;
3. the copy inside the installed package, read with `importlib.resources`.

There is no absolute path anywhere.

**Packaging.** Common's wheel includes every file under `src/hr_onboarding_common/`, so the document ships with the package. This was checked by installing a fresh build of Common into the UC05 worker environment and loading the document from the installed copy.

**Failure.** A missing, unreadable, non-UTF-8 or empty file raises `PolicySourceUnavailableError`, and so does a file with no policy sections or with malformed front matter.

## 3. What ingestion does to the text

Each `##` or `###` section becomes one retrievable unit:

| Field | Content |
|---|---|
| `heading` | the heading, exactly as written |
| `path` | the heading; a `###` subsection also carries its parent, e.g. `Remunerations and Reimbursements > Payroll` |
| `pages` | the PDF pages the section spans, from the page markers, e.g. `pp. 6–7` |
| `text` | the section body, verbatim; only the page-marker comments are removed |

Lists, the table, block quotes and bold text are kept as written, and a test checks that every line of every section appears in the source file. The table of contents is navigation, not policy, so it isn't indexed.

**Versioning.** The document carries no version or effective date. Its version is therefore the first 12 hex digits of the SHA-256 of its exact contents, so any edit gives a new version, and every citation records it. If the document gains YAML front matter with `version` and `effective_date`, those values are used instead, and before its effective date the document is not used.

## 4. Retrieval

Retrieval lives in UC05's `context_layer/knowledge/policy_index.py` (`PolicyKnowledgeBase`). It reuses Common's existing keyword index (BM25, `KnowledgeIndex`) through a new, backward-compatible constructor, `KnowledgeIndex.from_sections`. On top of that it adds five steps:

1. **Multi-part questions.** Questions with several parts ("…? Also, …", "and also") are split, and each part is searched on its own.
2. **Topic words only.** Question words that say nothing about the subject ("what", "how", "can", "my", "policy" and so on) are ignored when judging relevance.
3. **A small synonym table.** It maps everyday words to the policy's own terms, for example "paid" → payroll, "quit" → resignation, "overtime" → outside business hours. It only widens the search; it never adds text to an answer.
4. **Ranking.** A section is kept only if it shares at least one topic word with the question. Sections rank by how much of their own heading the question names, plus how many of the question's topic words their text contains.
5. **Injection screening.** Any section containing text that looks like instructions to the assistant is dropped before the model sees it.

The result is at most 4 sections. If nothing relevant is found, the result is empty, the model is never called, and the question goes to HR as "not covered".

## 5. Answers and citations

`services/policy_qa.py` sends the retrieved sections to the model as untrusted data, with the `hr-policy-answerer` v2 instructions (`context_layer/skills/hr-policy-answerer/SKILL.md`).

**What the code checks before showing an answer:**
- every claim's quote appears in the cited section (`quote_is_grounded`);
- a claim may only reference a section that was actually provided;
- an answer containing a pay figure that isn't in a cited quote is withheld.

If nothing survives these checks, there is no answer and the question goes to HR.

**Citations.** Each citation holds the document id `hr-policy-consultants`, the title, the section path, the page range and the version. For example:

```
HR POLICIES — Annual leave (pp. 6–7)
```

**Response format for an answered question:**

```
Answer:
<grounded answer>

Policy Reference:
HR POLICIES — <section> (<pages>)

Additional Information:
<only when the policy supports a clarification>
```

**API compatibility.** `PolicyAnswer` keeps the fields it had (`answer`, `citations`, `no_answer_reason`). New fields are added with defaults: `category`, `language`, `message`, `policy_reference`, `additional_information` and `escalation`.

## 6. Updating or reloading the policy

1. Edit `HR_Policy_Consultants.md`, keeping `##`/`###` headings and the `<!-- Page N -->` markers. Optionally add front matter (`version`, `effective_date`).
2. Run `pytest tests/test_policy_knowledge_base.py tests/test_hr_policy_privacy_escalation.py` in UC05. The retrieval tests name the sections that typical questions must hit, so review them if headings change.
3. Reinstall Common wherever it is installed as a copy (`uv pip install --reinstall --no-deps HR-Onboarding-Common` into each worker environment, or `scripts/run_tests.ps1`), then restart UC05.

To use a reviewed copy without rebuilding, set `HR_POLICY_SOURCE_PATH`. A running process loads the document once (`PolicyKnowledgeBase.document`); `reload()` picks up a new version without a restart.
