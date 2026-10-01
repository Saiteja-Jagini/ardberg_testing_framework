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
    instruction: str = Field(min_length=20, max_length=12000)
    selected_frameworks: list[Literal["playwright", "vitest"]] = Field(default_factory=list)

    @field_validator("instruction")
    @classmethod
    def meaningful_instruction(cls, value: str) -> str:
        if len(value.split()) < 4:
            raise ValueError("Describe the behavior and expected result in at least four words")
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


class TestEnvironmentValue(BaseModel):
    key: str
    value: str
    evidence_file: str


class TestService(BaseModel):
    kind: Literal["postgres"]
    evidence_files: list[str]
    connection_environment_keys: list[str]
    setup_commands: list[str] = Field(default_factory=list)


class FrameworkAnalysis(BaseModel):
    application_framework: str
    language: str
    package_manager: str
    native_test_framework: str
    has_native_test_framework: bool
    install_commands: list[str]
    existing_test_commands: list[str]
    app_start_command: str
    app_ready_url: str
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
