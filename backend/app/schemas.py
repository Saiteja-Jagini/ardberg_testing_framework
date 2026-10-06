from typing import Any, Literal
from pydantic import BaseModel, Field, field_validator


class ResolveRequest(BaseModel):
    url: str


class PreviewRequest(BaseModel):
    repository: str
    pr_number: int = Field(gt=0)


class CreateRunRequest(BaseModel):
    repository: str
    pr_number: int = Field(gt=0)
    instruction: str = Field(default="", max_length=12000)
    selected_frameworks: list[Literal["playwright", "vitest"]] = Field(default_factory=list)
    mode: Literal["critique", "testing"] = "critique"

    @field_validator("instruction")
    @classmethod
    def meaningful_instruction(cls, value: str) -> str:
        return value.strip()


class StartInteractivePreviewRequest(BaseModel):
    command: str = Field(default="", max_length=1000)
    port: int | None = Field(default=None, ge=1, le=65535)
    ready_path: str = Field(default="", max_length=500)
    environment: dict[str, str] = Field(default_factory=dict)
    setup_commands: list[str] | None = Field(default=None, max_length=20)

    @field_validator("command", "ready_path")
    @classmethod
    def no_control_characters(cls, value: str) -> str:
        if any(ord(char) < 32 for char in value):
            raise ValueError("Control characters are not allowed")
        return value.strip()

    @field_validator("environment")
    @classmethod
    def valid_environment(cls, value: dict[str, str]) -> dict[str, str]:
        import re
        if len(value) > 40:
            raise ValueError("At most 40 preview environment values are allowed")
        for key, content in value.items():
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                raise ValueError(f"Invalid environment variable name: {key}")
            if len(content) > 2000 or any(ord(char) < 32 for char in content):
                raise ValueError(f"Invalid environment variable value for {key}")
        return value

    @field_validator("setup_commands")
    @classmethod
    def valid_setup_commands(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return value
        if any(not command.strip() or len(command) > 1000 or
               any(ord(char) < 32 for char in command) for command in value):
            raise ValueError("Each preview setup command must be one nonempty line")
        return [command.strip() for command in value]


class ManualObservationRequest(BaseModel):
    verdict: Literal["passed", "failed", "blocked"]
    steps: str = Field(min_length=5, max_length=5000)
    expected: str = Field(min_length=3, max_length=5000)
    actual: str = Field(min_length=3, max_length=5000)

    @field_validator("steps", "expected", "actual")
    @classmethod
    def nonempty_observation(cls, value: str) -> str:
        if len(value.strip()) < 3:
            raise ValueError("Manual observation fields must describe an actual test")
        return value.strip()


class NodeResult(BaseModel):
    node: str
    success: bool
    evidence: list[str] = Field(default_factory=list)
    artifacts: list[str] = Field(default_factory=list)
    error: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class TestCase(BaseModel):
    title: str
    behavior: str
    expected: str
    source: str
    framework: str
    patch_location: str = ""


class TestPlan(BaseModel):
    cases: list[TestCase]
    uncovered: list[str]
    rationale: str


class OmittedCase(BaseModel):
    title: str
    reason: str


class GeneratedPatch(BaseModel):
    patch: str
    explanation: str
    test_files: list[str]
    test_command: str
    uncovered_cases: list[OmittedCase] = Field(default_factory=list)


class SkillChoice(BaseModel):
    names: list[str]
    reasons: list[str]


class CommandSelection(BaseModel):
    commands: list[str]
    rationale: str


class DependencyRepair(BaseModel):
    missing_dependency: bool
    repair_command: str = ""
    reason: str


class TestEnvironmentValue(BaseModel):
    key: str
    value: str
    evidence_file: str


class TestService(BaseModel):
    kind: Literal["postgres"]
    evidence_files: list[str]
    connection_environment_keys: list[str]
    setup_commands: list[str] = Field(default_factory=list)
    required_extensions: list[str] = Field(default_factory=list)
    baseline_setup_commands: list[str] = Field(default_factory=list)
    seed_commands: list[str] = Field(default_factory=list)
    upgrade_commands: list[str] = Field(default_factory=list)


class FrameworkAnalysis(BaseModel):
    application_framework: str
    language: str
    package_manager: str
    native_test_framework: str
    has_native_test_framework: bool
    install_commands: list[str]
    existing_test_commands: list[str]
    security_check_commands: list[str] = Field(default_factory=list)
    app_start_command: str
    app_ready_url: str
    interactive_preview_command: str = ""
    interactive_preview_ready_url: str = ""
    interactive_preview_setup_commands: list[str] = Field(default_factory=list)
    test_environment: list[TestEnvironmentValue]
    services: list[TestService] = Field(default_factory=list)
    playwright: "SpecialistDecision"
    vitest: "SpecialistDecision"
    explanation: str


class SpecialistDecision(BaseModel):
    mode: Literal["direct", "adapter", "unsupported"]
    target: Literal["browser", "http_api", "javascript_module", "none"]
    evidence_files: list[str]
    reason: str
    covered_behaviors: list[str]
    setup_command: str = ""
    ready_url: str = ""
    required_environment: list[str] = Field(default_factory=list)
    service_dependencies: list[str] = Field(default_factory=list)


class InstructionAssessment(BaseModel):
    testable: bool
    reason: str
    behaviors: list[str]


class ImpactClaim(BaseModel):
    text: str
    evidence_files: list[str]
    origin: Literal["pr_description", "commit_message", "code", "user_instruction"]


class ImpactArea(BaseModel):
    surface: Literal["frontend", "api", "database", "security", "cross_cutting"]
    summary: str
    changed_files: list[str]
    related_files: list[str] = Field(default_factory=list)
    confidence: Literal["confirmed", "potential"]
    reason: str


class BrowserReviewTarget(BaseModel):
    path: str
    evidence_files: list[str]
    reason: str


class ImpactCheck(BaseModel):
    surface: Literal["frontend", "api", "database", "security", "cross_cutting"]
    behavior: str
    expected: str
    source_files: list[str]
    method: Literal["native_test", "playwright", "vitest", "manual"]
    prerequisites: list[str] = Field(default_factory=list)


class ImpactMap(BaseModel):
    feature_summary: str
    claims: list[ImpactClaim] = Field(default_factory=list)
    areas: list[ImpactArea] = Field(default_factory=list)
    browser_targets: list[BrowserReviewTarget] = Field(default_factory=list)
    checks: list[ImpactCheck] = Field(default_factory=list)
    review_gaps: list[str] = Field(default_factory=list)


class SecurityConcern(BaseModel):
    path: str
    evidence_quote: str
    concern: str
    test_to_confirm: str


class SecurityReview(BaseModel):
    concerns: list[SecurityConcern] = Field(default_factory=list)
    uncovered: list[str] = Field(default_factory=list)


class ReportDraft(BaseModel):
    markdown: str
    evidence_event_ids: list[int]
    uncovered_requests: list[str]
    claims: list["EvidenceClaim"] = Field(default_factory=list)


class EvidenceClaim(BaseModel):
    text: str
    event_ids: list[int] = Field(default_factory=list)
    artifact_paths: list[str] = Field(default_factory=list)
    source_paths: list[str] = Field(default_factory=list)
    expected_status: Literal["passed", "failed", "cancelled", "running", "not_started"] | None = None


class ReportVerification(BaseModel):
    supported: bool
    unsupported_claims: list[str] = Field(default_factory=list)
    reason: str
