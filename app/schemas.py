"""요청·응답 모델. `docs/AI_CONTRACT_v0.1.md` 와 1:1 로 대응한다.

계약을 고치면 여기를 고치고, 여기를 고치면 계약을 고친다. 한쪽만 바꾸지 않는다.
"""

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class Base(BaseModel):
    """JSON 은 camelCase, 파이썬은 snake_case."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


# --------------------------------------------------------------------------
# 공통
# --------------------------------------------------------------------------


class PostingType(StrEnum):
    RECRUIT = "recruit"
    SCHOLARSHIP = "scholarship"
    CONTEST = "contest"
    ACTIVITY = "activity"


class ParseStatus(StrEnum):
    OK = "ok"
    PARTIAL = "partial"
    FAILED = "failed"


class Meta(Base):
    """호출마다 남긴다. 비용·지연 집계(#28)의 입력이다."""

    provider: str
    model: str
    prompt_version: str
    latency_ms: int
    cost_krw: float


class Image(Base):
    url: str
    order: int


class JobInterest(Base):
    code: str
    priority: int


class Profile(Base):
    """이름·학번·연락처·학교명은 넣지 않는다 (계약 D4)."""

    department: str | None = None
    gpa: float | None = None
    grad_year: int | None = None
    job_interests: list[JobInterest] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


class Experience(Base):
    id: int
    type: str
    title: str
    start_date: str | None = None
    end_date: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------
# §1 공고 구조화 파싱
# --------------------------------------------------------------------------


class Qualifications(Base):
    year: str | None = None
    gpa: str | None = None
    major: str | None = None


class FormQuestion(Base):
    order: int
    question: str
    max_chars: int | None = None
    required: bool = True


class ParseRequest(Base):
    posting_id: int
    title: str
    raw_content: str
    url: str | None = None
    collected_at: str | None = None
    images: list[Image] = Field(default_factory=list)


class ParseResult(Base):
    posting_id: int
    status: ParseStatus
    truncated: bool = False
    missing: list[str] = Field(default_factory=list)
    type: PostingType | None = None
    organization: str | None = None
    due_date: str | None = None
    due_date_raw: str | None = None
    keywords: list[str] = Field(default_factory=list)
    qualifications: Qualifications = Field(default_factory=Qualifications)
    preferences: list[str] = Field(default_factory=list)
    work_type: str | None = None
    form_questions: list[FormQuestion] = Field(default_factory=list)
    meta: Meta | None = None


# --------------------------------------------------------------------------
# §2 적합도 산출
# --------------------------------------------------------------------------


class ScoreRequest(Base):
    posting_id: int
    parsed: ParseResult
    profile: Profile
    experiences: list[Experience] = Field(default_factory=list)


class Axis(Base):
    axis: Literal["field_similarity", "qualification", "preference", "competition"]
    score: int | None
    weight: int
    basis: str | None = None


class ScoreResult(Base):
    score: int
    label: Literal["very_suitable", "suitable", "moderate", "low"]
    provisional: bool = False
    reweighted: bool = False
    breakdown: list[Axis]
    strength_comment: str
    weakness_comment: str
    cited_experience_ids: list[int] = Field(default_factory=list)
    meta: Meta | None = None


# --------------------------------------------------------------------------
# §3 지원서 초안 생성
# --------------------------------------------------------------------------


class GenerateRequest(Base):
    question: str
    max_chars: int | None = None
    tone: Literal["formal", "casual"] = "formal"
    parsed: ParseResult | None = None
    profile: Profile = Field(default_factory=Profile)
    experiences: list[Experience] = Field(default_factory=list)
    emphasize_experience_ids: list[int] = Field(default_factory=list)
    past_application_samples: list[str] = Field(default_factory=list)
    previous_answer: str | None = None


class FactCheck(Base):
    passed: bool
    unverified: list[str] = Field(default_factory=list)


class GenerateResult(Base):
    answer: str
    char_count: int
    used_experience_ids: list[int] = Field(default_factory=list)
    fact_check: FactCheck
    meta: Meta | None = None


# --------------------------------------------------------------------------
# §4 과거 지원서 항목 분류
# --------------------------------------------------------------------------


class ItemCategory(StrEnum):
    MOTIVATION = "motivation"
    BACKGROUND = "background"
    EXPERIENCE = "experience"
    COMPETENCY = "competency"
    ASPIRATION = "aspiration"
    OTHER = "other"


class ClassifyRequest(Base):
    past_application_id: int
    text: str
    label: str | None = None


class ClassifiedItem(Base):
    order: int
    category: ItemCategory
    content: str
    confident: bool


class ClassifyResult(Base):
    items: list[ClassifiedItem]
    meta: Meta | None = None


class DocFormat(StrEnum):
    PDF = "pdf"
    DOCX = "docx"
    TXT = "txt"


class ExtractTextResult(Base):
    """§4.3. 외부로 나가는 호출이 없다 — 추출과 OCR 을 전부 로컬에서 한다."""

    past_application_id: int
    format: DocFormat
    text: str
    char_count: int
    ocr_used: bool = False
    meta: Meta | None = None


# --------------------------------------------------------------------------
# §5 임베딩
# --------------------------------------------------------------------------


class EmbedRequest(Base):
    texts: list[str]
    purpose: Literal["experience", "posting", "profile"] = "experience"


class EmbedResult(Base):
    vectors: list[list[float]]
    dim: int
    model: str
