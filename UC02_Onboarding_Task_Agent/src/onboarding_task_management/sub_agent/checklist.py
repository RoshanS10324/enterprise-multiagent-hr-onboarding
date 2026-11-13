"""Client onboarding configuration: explicit task templates.

Task content is **never** inferred or generated. It comes only from a
:class:`ClientOnboardingConfig` supplied by the caller. Review C-6 records that
the real checklist content is owned by HR Ops and is not yet available, so the
prototype ships synthetic configurations in :mod:`synthetic` instead of
hardcoding client content here.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from onboarding_task_management.contracts import (
    ChangeDriver,
    ChecklistIssue,
    ChecklistIssueSeverity,
    DimensionVerdict,
    OwnerAssignmentIssue,
    Role,
    SelectionDimension,
    TaskCategory,
)
from onboarding_task_management.errors import MissingConfigurationError


def _find_cycle(graph: dict[str, tuple[str, ...]]) -> list[str] | None:
    """Return one dependency cycle as a readable path, or ``None`` if acyclic."""
    WHITE, GREY, BLACK = 0, 1, 2
    colour = dict.fromkeys(graph, WHITE)
    path: list[str] = []

    def visit(node: str) -> list[str] | None:
        colour[node] = GREY
        path.append(node)
        for nxt in graph.get(node, ()):
            if nxt not in colour:
                continue  # unknown keys are reported separately
            if colour[nxt] is GREY:
                return path[path.index(nxt):] + [nxt]
            if colour[nxt] is WHITE:
                found = visit(nxt)
                if found is not None:
                    return found
        path.pop()
        colour[node] = BLACK
        return None

    for key in graph:
        if colour[key] is WHITE:
            found = visit(key)
            if found is not None:
                return found
    return None


class TaskTemplate(BaseModel):
    """One checklist line in a client's configuration."""

    model_config = ConfigDict(frozen=True)

    #: Stable per-client identifier. Also the suffix of the generated task_id,
    #: which is what makes task IDs deterministic across runs.
    key: str
    category: TaskCategory
    title: str
    #: Required, not defaulted. A default of "" would skip the blank check
    #: below entirely, since pydantic does not validate defaults (finding M3).
    title_ar: str
    owner: Role
    #: Due date offset in **business days** relative to the joining date.
    #: Negative = before joining (e.g. -10 = ten business days before day 1).
    offset_days: int
    #: ``key`` values of prerequisite templates within the same config.
    depends_on: tuple[str, ...] = ()
    #: Which attribute changes force this task to be re-planned.
    sensitive_to: tuple[ChangeDriver, ...] = (ChangeDriver.JOINING_DATE,)
    #: Applicability filters. Empty tuple means "applies to all".
    locations: tuple[str, ...] = ()
    projects: tuple[str, ...] = ()
    role_ids: tuple[str, ...] = ()
    #: UC02-BR-02. Grade filtering. Empty = applies to all grades.
    grades: tuple[str, ...] = ()
    #: UC02-BR-02 nationality dimension, expressed as opaque GROUP CODES.
    #: Empty = applies to all groups, which is the default: no client has an
    #: approved nationality policy, so nothing is filtered until one exists.
    nationality_groups: tuple[str, ...] = ()
    #: UC02-BR-08. Applicability flags that must ALL be present on the
    #: onboarding for this template to apply, e.g. ("needs_travel",).
    #: Not every employee needs travel, accommodation or family insurance,
    #: so these activities are opt-in per onboarding and never inferred.
    requires_attributes: tuple[str, ...] = ()
    #: UC02-BR-12. Completion requires an evidence/confirmation reference.
    evidence_required: bool = False
    #: UC02-BR-11. Role that must approve this action under the approved
    #: policy (Business Framework §8). None = no action-specific approver.
    approval_role: Role | None = None
    #: One of ``contracts.PRIORITIES``. Declared by the client configuration;
    #: ``normal`` when it states none. UC-02 never raises a priority itself.
    priority: str = "normal"
    #: Business days of work before the due date. 0 = the task is a point in time.
    duration_days: int = 0
    #: Optional client-approved ceiling on failed reports for this task. ``None``
    #: (the default) means no limit was approved; UC-02 never chooses one.
    max_failed_reports: int | None = None

    @field_validator("max_failed_reports")
    @classmethod
    def _positive_limit(cls, value: int | None) -> int | None:
        if value is not None and value < 1:
            raise ValueError("max_failed_reports must be at least 1 when set")
        return value

    @field_validator("priority")
    @classmethod
    def _known_priority(cls, value: str) -> str:
        from onboarding_task_management.contracts import PRIORITIES

        if value not in PRIORITIES:
            raise ValueError(f"unknown priority {value!r}; expected one of {PRIORITIES}")
        return value

    @field_validator("duration_days")
    @classmethod
    def _non_negative_duration(cls, value: int) -> int:
        if value < 0:
            raise ValueError("duration_days must not be negative")
        return value

    @field_validator("key")
    @classmethod
    def _key_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("TaskTemplate.key must not be blank")
        return value

    @field_validator("title", "title_ar")
    @classmethod
    def _titles_not_blank(cls, value: str) -> str:
        """Both languages are mandatory.

        Bilingual output is a UAE requirement (blueprint D3), and ``title_ar``
        previously defaulted to ``""`` with no check, so a client config could
        silently produce Arabic-less tasks (finding M3).
        """
        if not value.strip():
            raise ValueError("TaskTemplate.title and title_ar must not be blank")
        return value

    def dimension_verdicts(
        self,
        *,
        location: str,
        project: str,
        role_id: str,
        grade: str = "",
        nationality_group: str = "",
        attributes: Sequence[str] = (),
    ) -> tuple[DimensionVerdict, ...]:
        """Per-dimension applicability result, in a fixed order.

        This is the **single** place the filter rules are evaluated:
        :meth:`applies_to` is the boolean reduction of these verdicts, and
        :mod:`selection` turns the same verdicts into explanations. Keeping
        one implementation means the answer and the reason can never disagree.
        """
        supplied_attributes = sorted(set(attributes))
        return (
            DimensionVerdict(
                dimension=SelectionDimension.LOCATION,
                configured=list(self.locations),
                supplied=[location],
                matched=not self.locations or location in self.locations,
            ),
            DimensionVerdict(
                dimension=SelectionDimension.PROJECT,
                configured=list(self.projects),
                supplied=[project],
                matched=not self.projects or project in self.projects,
            ),
            DimensionVerdict(
                dimension=SelectionDimension.ROLE_ID,
                configured=list(self.role_ids),
                supplied=[role_id],
                matched=not self.role_ids or role_id in self.role_ids,
            ),
            DimensionVerdict(
                dimension=SelectionDimension.GRADE,
                configured=list(self.grades),
                supplied=[grade],
                matched=not self.grades or grade in self.grades,
            ),
            DimensionVerdict(
                dimension=SelectionDimension.NATIONALITY_GROUP,
                configured=list(self.nationality_groups),
                supplied=[nationality_group],
                matched=(
                    not self.nationality_groups
                    or nationality_group in self.nationality_groups
                ),
            ),
            DimensionVerdict(
                dimension=SelectionDimension.ATTRIBUTES,
                configured=list(self.requires_attributes),
                supplied=supplied_attributes,
                missing=sorted(
                    set(self.requires_attributes) - set(supplied_attributes)
                ),
                matched=set(self.requires_attributes) <= set(supplied_attributes),
            ),
        )

    def applies_to(
        self,
        *,
        location: str,
        project: str,
        role_id: str,
        grade: str = "",
        nationality_group: str = "",
        attributes: Sequence[str] = (),
    ) -> bool:
        """Whether this template is in scope for a given onboarding.

        The nationality dimension of UC02-BR-02 is implemented as an opaque
        **group code** supplied by the caller, never a nationality value, and
        it is unfiltered by default. Mapping a nationality to a group is a
        Governance/Legal policy that does not exist yet; UC-02 provides the
        configurable boundary and does not invent the policy.
        """
        return all(
            verdict.matched
            for verdict in self.dimension_verdicts(
                location=location,
                project=project,
                role_id=role_id,
                grade=grade,
                nationality_group=nationality_group,
                attributes=attributes,
            )
        )


class MilestoneTemplate(BaseModel):
    """A named checkpoint in a client's onboarding plan.

    Which tasks constitute a milestone is **declared by the client**, exactly
    as task content is — it is never inferred from categories, owners or due
    dates. Milestone state is then derived arithmetically from those tasks.
    """

    model_config = ConfigDict(frozen=True)

    key: str
    title: str
    title_ar: str
    #: Target date as a business-day offset from the joining date; negative is
    #: before day 1, matching :class:`TaskTemplate.offset_days`.
    offset_days: int
    #: ``TaskTemplate.key`` values that must all be COMPLETE to achieve this.
    requires: tuple[str, ...]
    #: Applicability filters, same semantics as TaskTemplate. Empty = all.
    locations: tuple[str, ...] = ()
    projects: tuple[str, ...] = ()
    role_ids: tuple[str, ...] = ()
    grades: tuple[str, ...] = ()
    #: UC02-BR-07/08. A milestone covering optional activities must itself be
    #: optional, or an employee who needs none of them would be reported as
    #: at risk for tasks that correctly never existed.
    requires_attributes: tuple[str, ...] = ()

    @field_validator("key")
    @classmethod
    def _key_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("MilestoneTemplate.key must not be blank")
        return value

    @field_validator("title", "title_ar")
    @classmethod
    def _titles_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError(
                "MilestoneTemplate.title and title_ar must not be blank"
            )
        return value

    @field_validator("requires")
    @classmethod
    def _requires_not_empty(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("MilestoneTemplate.requires must name at least one task")
        if len(set(value)) != len(value):
            raise ValueError("MilestoneTemplate.requires contains duplicates")
        return value

    def applies_to(
        self,
        *,
        location: str,
        project: str,
        role_id: str,
        grade: str = "",
        attributes: Sequence[str] = (),
    ) -> bool:
        if self.locations and location not in self.locations:
            return False
        if self.projects and project not in self.projects:
            return False
        if self.role_ids and role_id not in self.role_ids:
            return False
        if self.grades and grade not in self.grades:
            return False
        if self.requires_attributes and not set(self.requires_attributes) <= set(attributes):
            return False
        return True


APPROVAL_STATUSES = frozenset({"approved", "draft", "retired", "unspecified"})


ASSIGNMENT_MODES = frozenset({"role", "named_individual", "use_case"})


class DocumentRequirement(BaseModel):
    """One document the client's configuration requires. UC-02 only lists and tracks it; the document
    collection process (and UC-03) decide how it is collected and verified."""

    model_config = ConfigDict(frozen=True)

    key: str
    document_type: str
    mandatory: bool = True
    reason: str = "required by the client's approved configuration"
    locations: tuple[str, ...] = ()
    projects: tuple[str, ...] = ()
    role_ids: tuple[str, ...] = ()
    grades: tuple[str, ...] = ()
    nationality_groups: tuple[str, ...] = ()
    requires_attributes: tuple[str, ...] = ()
    allow_not_applicable: bool = False

    def applies_to(self, *, location: str, project: str, role_id: str, grade: str = "",
                   nationality_group: str = "", attributes: Sequence[str] = ()) -> tuple[bool, str]:
        """``(applies, explanation)``. The explanation names the first filter that excluded it, or the match."""
        checks = (
            ("location", self.locations, location), ("project", self.projects, project),
            ("role", self.role_ids, role_id), ("grade", self.grades, grade),
            ("nationality group", self.nationality_groups, nationality_group),
        )
        for name, allowed, actual in checks:
            if allowed and actual not in allowed:
                return False, f"excluded: {name} {actual!r} is not in the configured {name}s"
        missing = [a for a in self.requires_attributes if a not in attributes]
        if missing:
            return False, f"excluded: requires attribute(s) {missing} the onboarding does not declare"
        return True, self.reason


class AssignmentRule(BaseModel):
    """WHO is responsible for a category of work, as the client configured it.

    Separate from the task template (which says WHAT and WHEN). A category with no rule is an
    *assignment gap*, reported rather than guessed. UC-02 invents no team names: ``team`` is the client's label.
    """

    model_config = ConfigDict(frozen=True)

    category: TaskCategory
    team: str
    #: The use case that executes the work when it is a downstream agent (``UC-03`` / ``UC-04`` / ``UC-05``);
    #: empty when people, not an agent, do it. UC-02 coordinates and tracks; it never performs the work.
    use_case: str = ""
    #: ``role`` (any holder of the owner role), ``named_individual`` (HR must name a person) or ``use_case``.
    mode: str = "role"
    #: Role the work escalates to when late. ``None`` = the client named none (a gap for escalation).
    escalation_owner: Role | None = None

    @field_validator("mode")
    @classmethod
    def _known_mode(cls, value: str) -> str:
        if value not in ASSIGNMENT_MODES:
            raise ValueError(f"unknown assignment mode {value!r}; expected one of {sorted(ASSIGNMENT_MODES)}")
        return value

    @field_validator("team")
    @classmethod
    def _team_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("AssignmentRule.team must not be blank")
        return value


class NonResponsePolicy(BaseModel):
    """DL-14: how a client chases an employee who has not responded.

    Both numbers are the client's own. UC-02 supplies no default for either: when a client has
    approved no policy, reminder attempts are refused rather than guessed.
    """

    model_config = ConfigDict(frozen=True)

    #: Whole days that must pass between one attempt and the next.
    interval_days: int = Field(ge=1)
    #: Attempts allowed before the case is escalated to the HR onboarding officer.
    max_attempts: int = Field(ge=1)


class AttributeFamily(BaseModel):
    """An employee attribute the checklist depends on, as ``<family>:<value>`` (e.g. ``location_status:in_country``).

    ``required`` families must be stated for every employee: when one is missing the checklist is flagged for HR review instead of
    silently dropping the tasks that depend on it. ``values`` lists the accepted values (empty = any value)."""

    model_config = ConfigDict(frozen=True)

    family: str
    values: tuple[str, ...] = ()
    required: bool = True
    reason: str = ""


class EmployeeEligibility(BaseModel):
    """The client's declared catalogue: which roles, grades, work locations, nationality groups and employment types it onboards.

    Comes from the client's configuration (``uc01_employee_record`` section). An empty tuple means the client declared none for that
    dimension, so it is not validated. ``None`` on the configuration means no catalogue exists at all (legacy configurations)."""

    model_config = ConfigDict(frozen=True)

    roles: tuple[str, ...] = ()
    grades: tuple[str, ...] = ()
    #: Accepted work-location values: codes and names both match.
    locations: tuple[str, ...] = ()
    nationality_groups: tuple[str, ...] = ()
    employment_types: tuple[str, ...] = ()
    attribute_families: tuple[AttributeFamily, ...] = ()


class ClientOnboardingConfig(BaseModel):
    """The full explicit checklist configuration for one client."""

    model_config = ConfigDict(frozen=True)

    client_id: str
    #: Configuration version label and validity window. Empty / ``None`` means
    #: the source did not state one; UC-02 never invents a version.
    version: str = ""
    effective_from: date | None = None
    effective_to: date | None = None
    #: ``approved`` | ``draft`` | ``retired`` | ``unspecified``. Only an
    #: explicit ``approved`` configuration is ever used: ``unspecified`` means
    #: the source recorded no status, and that is refused, not assumed. Any
    #: other value is rejected when the configuration is built.
    approval_status: str = "unspecified"
    templates: tuple[TaskTemplate, ...] = ()
    #: Optional. A client with no milestones simply has none reported.
    milestones: tuple[MilestoneTemplate, ...] = ()
    #: Optional DL-14 policy. ``None`` = the client approved none (see NonResponsePolicy).
    non_response_policy: NonResponsePolicy | None = None
    #: Optional WHO mapping per task category. Empty = nothing is mapped, so every category is an assignment gap.
    assignments: tuple[AssignmentRule, ...] = ()
    #: Optional document requirements. Empty = none configured, which keeps the document gate closed.
    document_requirements: tuple[DocumentRequirement, ...] = ()
    #: ``"synthetic"`` for test-only configuration, empty for a client's real approved configuration. Carried onto every
    #: onboarding built from it, so synthetic data is always identifiable and never mistaken for real client policy.
    data_origin: str = ""
    #: Optional eligibility catalogue. When present, an employee outside it is refused (``EmployeeNotEligibleError``).
    eligibility: EmployeeEligibility | None = None

    def documents_for(self, *, location: str, project: str, role_id: str, grade: str = "",
                      nationality_group: str = "", attributes: Sequence[str] = ()):
        """``(included, excluded)`` as ``(requirement, explanation)`` pairs for this employee."""
        included, excluded = [], []
        for req in self.document_requirements:
            ok, why = req.applies_to(location=location, project=project, role_id=role_id, grade=grade,
                                     nationality_group=nationality_group, attributes=attributes)
            (included if ok else excluded).append((req, why))
        return included, excluded

    def assignment_for(self, category: TaskCategory) -> AssignmentRule | None:
        return next((a for a in self.assignments if a.category is category), None)

    @field_validator("approval_status")
    @classmethod
    def _known_approval_status(cls, value: str) -> str:
        if value not in APPROVAL_STATUSES:
            raise ValueError(
                f"unknown approval_status {value!r}; expected one of {sorted(APPROVAL_STATUSES)}"
            )
        return value

    @field_validator("templates")
    @classmethod
    def _validate_templates(
        cls, templates: tuple[TaskTemplate, ...]
    ) -> tuple[TaskTemplate, ...]:
        seen: set[str] = set()
        for template in templates:
            if template.key in seen:
                raise ValueError(f"duplicate template key: {template.key}")
            seen.add(template.key)

        # Dependencies must resolve inside this config.
        for template in templates:
            for dependency in template.depends_on:
                if dependency not in seen:
                    raise ValueError(
                        f"template {template.key!r} depends on unknown key {dependency!r}"
                    )
                if dependency == template.key:
                    raise ValueError(f"template {template.key!r} depends on itself")

        # Reject cycles here rather than letting them surface later from
        # topological_order at plan time (finding L4). A cycle means no task in
        # the loop is ever startable, so the config is simply invalid.
        cycle = _find_cycle({t.key: t.depends_on for t in templates})
        if cycle is not None:
            raise ValueError(
                "dependency cycle in client configuration: "
                + " -> ".join(cycle)
            )
        return templates

    @model_validator(mode="after")
    def _validate_milestones(self) -> "ClientOnboardingConfig":
        """Milestone keys must be unique and reference real task templates."""
        known = {t.key for t in self.templates}
        seen: set[str] = set()
        for milestone in self.milestones:
            if milestone.key in seen:
                raise ValueError(f"duplicate milestone key: {milestone.key}")
            seen.add(milestone.key)
            for required in milestone.requires:
                if required not in known:
                    raise ValueError(
                        f"milestone {milestone.key!r} requires unknown task "
                        f"template {required!r}"
                    )
        return self

    def applicable_milestones(
        self,
        *,
        location: str,
        project: str,
        role_id: str,
        grade: str = "",
        attributes: Sequence[str] = (),
    ) -> tuple[MilestoneTemplate, ...]:
        """Milestones in scope for this onboarding."""
        return tuple(
            m
            for m in self.milestones
            if m.applies_to(
                location=location,
                project=project,
                role_id=role_id,
                grade=grade,
                attributes=attributes,
            )
        )

    def dependency_scope_conflicts(self) -> list[ChecklistIssue]:
        """Templates that can be in scope while a prerequisite is not.

        A dependent whose filters are *wider* than its prerequisite's admits a
        context in which the dependent is planned but the prerequisite never
        exists. :func:`planner.build_tasks` drops such a dangling dependency so
        the task is not blocked forever — correct at plan time, but it silently
        removes a prerequisite the client configured, which for a sequence like
        "verify documents before visa processing" is a decision a human has to
        make. The conflict is therefore reported, never resolved here.

        The check is static (configuration only) and deterministic: for each
        value-filter dimension the prerequisite is narrower when it restricts
        values the dependent does not already restrict to a subset of; for
        ``requires_attributes`` it is narrower when it demands a flag the
        dependent does not itself demand.
        """
        by_key = {t.key: t for t in self.templates}
        issues: list[ChecklistIssue] = []
        value_dimensions = (
            ("locations", "location"),
            ("projects", "project"),
            ("role_ids", "role_id"),
            ("grades", "grade"),
            ("nationality_groups", "nationality_group"),
        )
        for template in self.templates:
            for dependency_key in template.depends_on:
                prerequisite = by_key.get(dependency_key)
                if prerequisite is None:  # already rejected by validation
                    continue
                narrower: list[str] = []
                for attribute, label in value_dimensions:
                    prerequisite_values = set(getattr(prerequisite, attribute))
                    dependent_values = set(getattr(template, attribute))
                    if not prerequisite_values:
                        continue
                    if not dependent_values or not dependent_values <= prerequisite_values:
                        narrower.append(label)
                extra_flags = set(prerequisite.requires_attributes) - set(
                    template.requires_attributes
                )
                if extra_flags:
                    narrower.append(
                        "attributes(" + ",".join(sorted(extra_flags)) + ")"
                    )
                if narrower:
                    issues.append(
                        ChecklistIssue(
                            code="dependency_scope_conflict",
                            severity=ChecklistIssueSeverity.REVIEW_REQUIRED,
                            subject=template.key,
                            detail=(
                                f"template {template.key!r} can apply where its "
                                f"prerequisite {dependency_key!r} does not "
                                f"(narrower on: {', '.join(narrower)}); the "
                                "prerequisite would be dropped from the plan "
                                "instead of blocking the task"
                            ),
                        )
                    )
        return issues

    def owner_issues(self) -> list[OwnerAssignmentIssue]:
        """UC02-BR-03. Report templates with a missing or invalid owner.

        ``TaskTemplate.owner`` is a required, typed field, so pydantic already
        rejects an unknown role at load time. This method covers what typing
        cannot: an owner that parses but is not a legitimate *assignable*
        owner - today, the SYSTEM pseudo-role. No default owner is ever
        substituted; the issue is reported for correction.
        """
        issues: list[OwnerAssignmentIssue] = []
        for template in self.templates:
            if template.owner is Role.SYSTEM:
                issues.append(
                    OwnerAssignmentIssue(
                        template_key=template.key,
                        client_id=self.client_id,
                        problem="owner is the SYSTEM pseudo-role, which cannot own work",
                        configured_owner=template.owner.value,
                    )
                )
        return issues

    def applicable(
        self,
        *,
        location: str,
        project: str,
        role_id: str,
        grade: str = "",
        nationality_group: str = "",
        attributes: Sequence[str] = (),
    ) -> tuple[TaskTemplate, ...]:
        """Templates in scope for this onboarding, dependency-closed.

        A template whose own filters match but whose prerequisite is out of
        scope would otherwise be permanently blocked. Such dangling
        dependencies are dropped from the generated task's ``depends_on``
        rather than silently blocking day 1 — the planner records the
        prerequisite set it actually used.
        """
        return tuple(
            t
            for t in self.templates
            if t.applies_to(
                location=location,
                project=project,
                role_id=role_id,
                grade=grade,
                nationality_group=nationality_group,
                attributes=attributes,
            )
        )


class ConfigRegistry(BaseModel):
    """Per-client configuration lookup, isolated by ``client_id``."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    configs: dict[str, ClientOnboardingConfig] = Field(default_factory=dict)

    def register(self, config: ClientOnboardingConfig) -> None:
        self.configs[config.client_id] = config

    def get(
        self, client_id: str, *, on: date | None = None
    ) -> ClientOnboardingConfig:
        """The client's configuration, refusing one that is not in force.

        ``on`` is the date the configuration must be effective on (the
        joining date when planning). Omitted, only the approval status is
        checked.
        """
        config = self.configs.get(client_id)
        if config is None:
            raise MissingConfigurationError(
                "no onboarding configuration registered for client",
                client_id=client_id,
            )
        if config.approval_status != "approved":
            raise MissingConfigurationError(
                "client onboarding configuration is not approved for use",
                client_id=client_id,
                approval_status=config.approval_status,
            )
        if on is not None and (
            (config.effective_from is not None and on < config.effective_from)
            or (config.effective_to is not None and on > config.effective_to)
        ):
            raise MissingConfigurationError(
                "client onboarding configuration is not effective on this date",
                client_id=client_id,
                on=on.isoformat(),
                version=config.version,
            )
        if not config.templates:
            raise MissingConfigurationError(
                "client onboarding configuration contains no task templates",
                client_id=client_id,
            )
        return config


def topological_order(templates: tuple[TaskTemplate, ...]) -> tuple[TaskTemplate, ...]:
    """Order templates so prerequisites precede dependents.

    Raises :class:`MissingConfigurationError` on a dependency cycle, which would
    otherwise produce a plan where nothing is ever startable.
    """
    by_key = {t.key: t for t in templates}
    in_scope = set(by_key)
    ordered: list[TaskTemplate] = []
    permanent: set[str] = set()
    temporary: set[str] = set()

    def visit(key: str) -> None:
        if key in permanent:
            return
        if key in temporary:
            raise MissingConfigurationError(
                "dependency cycle in client configuration", template_key=key
            )
        temporary.add(key)
        for dependency in by_key[key].depends_on:
            if dependency in in_scope:
                visit(dependency)
        temporary.discard(key)
        permanent.add(key)
        ordered.append(by_key[key])

    for template in templates:
        visit(template.key)
    return tuple(ordered)
