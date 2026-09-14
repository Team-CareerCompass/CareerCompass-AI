"""요청·응답 모델. `docs/AI_CONTRACT_v0.2.md` 와 1:1 로 대응한다.

정본 근거는 BE 저장소의 `analysis/gateway/LlmGateway.java` 다 — 필드 이름이 그쪽 레코드와
1:1 이어야 한다. **계약과 이 파일은 함께 고친다.** 한쪽만 바꾸지 않는다.
"""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class Base(BaseModel):
    """JSON 은 camelCase, 파이썬은 snake_case."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class Usage(Base):
    """모든 성공 응답에 싣는다. BE 는 totalTokens 만 읽고 나머지는 #28 의 입력이다."""

    provider: str
    model: str
    prompt_version: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    latency_ms: int = 0
    cost_krw: float = 0.0


# --------------------------------------------------------------------------
# §1 공고 구조화 파싱
# --------------------------------------------------------------------------


class PostingType(StrEnum):
    RECRUIT = "recruit"
    SCHOLARSHIP = "scholarship"
    CONTEST = "contest"
    ACTIVITY = "activity"
    OTHER = "other"


class Image(Base):
    url: str
    order: int = 1


class FormQuestion(Base):
    order: int
    question: str
    max_chars: int | None = None


class ParseRequest(Base):
    """BE 는 title·rawContent 만 보낸다. 나머지는 선택이다 (계약 §1.1)."""

    title: str
    raw_content: str
    posting_id: int | None = None
    collected_at: str | None = None
    images: list[Image] = Field(default_factory=list)


class ParseResult(Base):
    """BE 의 `ParsedPosting` 레코드와 1:1. 자격 조건은 평평하다 — 중첩 객체가 아니다."""

    parsing_failed: bool = False
    type: PostingType | None = None
    keywords: list[str] = Field(default_factory=list)
    qualification_year: str | None = None
    qualification_gpa: str | None = None
    qualification_major: str | None = None
    preferences: list[str] = Field(default_factory=list)
    due_date: str | None = None
    due_date_raw: str | None = None
    form_questions: list[FormQuestion] = Field(default_factory=list)
    truncated: bool = False
    usage: Usage | None = None


class ParseFailure(Base):
    """파싱 실패도 200 이다. BE 는 reason 을 사람이 읽는 문장으로 쓴다."""

    parsing_failed: bool = True
    reason: str
    reason_code: str
    usage: Usage | None = None


# --------------------------------------------------------------------------
# §2 강점·약점 코멘트
# --------------------------------------------------------------------------


class CommentsRequest(Base):
    """점수는 BE 가 낸다. 이쪽은 계산에 쓴 근거를 문장으로 옮긴다."""

    matched_keywords: list[str] = Field(default_factory=list)
    missing_qualifications: list[str] = Field(default_factory=list)
    matched_preferences: list[str] = Field(default_factory=list)
    top_experience_title: str = ""


class CommentsResult(Base):
    strength: str | None = None
    weakness: str | None = None
    usage: Usage | None = None


# --------------------------------------------------------------------------
# §3 지원서 초안
# --------------------------------------------------------------------------


class FactCheck(Base):
    passed: bool
    unverified: list[str] = Field(default_factory=list)
    fallback: bool = False
    """모델 출력을 버리고 입력 문자열만으로 만든 안전 초안이다 (#29). BE 는 무시해도 된다."""


class DraftRequest(Base):
    """experienceSummaries 는 매칭도 순으로 정렬되어 오고, 이미 마스킹되어 있다."""

    question: str
    max_chars: int = 0
    """0 은 제한 없음이다 — BE 가 null 을 0 으로 바꿔 보낸다."""

    tone: str = "formal"
    posting_title: str = ""
    keywords: list[str] = Field(default_factory=list)
    experience_summaries: list[str] = Field(default_factory=list)


class DraftResult(Base):
    answer: str
    char_count: int = 0
    used_indexes: list[int] = Field(default_factory=list)
    """experienceSummaries 의 몇 번째를 인용했는지. BE 가 id 를 안 보내므로 인덱스로 답한다."""

    fact_check: FactCheck | None = None
    usage: Usage | None = None
