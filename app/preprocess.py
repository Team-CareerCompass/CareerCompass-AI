"""공고 본문 전처리 — LLM 호출 전에 도는 순수 함수들.

**여기가 파싱 품질과 토큰 비용에 모델 선택보다 크게 영향을 준다.** 게시판 공통 요소가
섞인 채로 프롬프트에 들어가면 엉뚱한 키워드가 뽑히고, 길이를 안 자르면 비용이 새고,
연락처가 그대로 나가면 개인정보가 외부로 나간다.

보고서 4.4 에는 이 단계가 없다. 계약 v0.2 §1.1 이 「HTML→텍스트 변환과 보일러플레이트
제거는 BE 수집 단계의 몫」이라고 정했지만, **BE 가 놓친 것이 넘어와도 여기서 한 번 더
거른다** — 들어오는 텍스트를 이쪽이 통제하지 못하기 때문이다.
"""

import re
from dataclasses import dataclass, field

MAX_CHARS = 40_000
"""계약 §1.1. 초과분은 잘라내고 truncated 로 알린다."""

MIN_MEANINGFUL_CHARS = 200
"""계약 §1.4. 이보다 짧으면 파싱을 시도하지 않는다."""

NL = "\n"
PARA = "\n\n"

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SPACES = re.compile("[ \t\u00a0\u3000]+")  # NBSP·전각 공백 — 웹 복사본에 흔하다
_BLANK_LINES = re.compile(r"\n{3,}")

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
_PHONE = re.compile(r"\b0\d{1,2}[-.)]\s?\d{3,4}[-.]\d{4}\b")
_MOBILE = re.compile(r"\b01[016-9][-.]?\d{3,4}[-.]?\d{4}\b")

# 게시판 공통 요소. 줄 전체가 이것뿐이면 버린다.
_BOILERPLATE_LINE = re.compile(
    r"^\s*(?:"
    r"이전\s?글|다음\s?글|목록(?:으로)?|글쓰기|수정|삭제|인쇄|스크랩|공유(?:하기)?|"
    r"좋아요|댓글\s?\d*|조회\s?수?\s?\d*|추천\s?\d*|첨부\s?파일|파일\s?다운로드|"
    r"맨\s?위로|top|prev|next|list|share|print"
    r")\s*$",
    re.IGNORECASE,
)

# 게시판 메타줄 — 「작성자 ○○ 조회수 1362 등록일 2026.09.04」. 학교 게시판에 흔하다.
# 등록일이 마감일로 잡히는 것을 막는다 (014 에서 실제로 잡혔다).
_COLON = "[:：]"  # noqa: RUF001 — 반각·전각 콜론 둘 다. 학교 게시판이 전각을 쓴다
_BOARD_META_LINE = re.compile(
    r"^\s*(?:작성자|작성일|등록일|조회\s?수|게시일|첨부)\s*" + _COLON + r"?\s*\S.*"
    r"(?:조회\s?수|등록일|작성일|게시일)\s*" + _COLON + r"?\s*[\d.\-\s]+\s*$"
)

# 링커리어 등 수집처가 본문 끝에 붙이는 자기 홍보.
_TRAILER = re.compile(
    r"\n?\s*대학생\s*대외활동\s*공모전\s*채용\s*사이트.*$|\n?\s*https?://linkareer\.com/?\s*$",
    re.IGNORECASE,
)


@dataclass
class Preprocessed:
    text: str
    truncated: bool = False
    masked: list[str] = field(default_factory=list)
    """마스킹한 원본 값들. 로그에 남기지 않는다 — 개수 확인용이다."""
    injections: int = 0
    """지시문으로 의심돼 통째로 뺀 문단 수 (#29). 0 이 아니면 로그에 남긴다."""
    resegmented: bool = False
    """줄바꿈이 거의 없어 다시 분절했다 — BE 크롤러가 Jsoup `body().text()` 로 보낸 모양."""

    @property
    def too_short(self) -> bool:
        return len(self.text.strip()) < MIN_MEANINGFUL_CHARS


# BE `CrawlService.fetchDetailText` 는 Jsoup `body().text()` 를 보낸다 — **줄바꿈이 전부 공백**이다.
# 규칙(줄 시작 번호 = 문항, 라벨↔값 블록 = 마감일)과 보일러플레이트 제거는 줄을 전제하므로,
# 줄이 거의 없으면 불릿·번호·라벨 앞에서 줄을 다시 세운다. 실측: 한 줄 텍스트에서 마감일 17→15.
_FLAT_CHARS_PER_NEWLINE = 300
_LABEL_HEAD = (
    "모집|지원|접수|신청|제출|선발|활동|우대|자격|혜택|문의|시상|심사|전형|채용|근무|급여|지역|학력|경력|"
    "마감|시작|결과|발표|기간|대상|인원|방법|내용|서류|일정|절차|요건|조건|사항|개요|주제|주최|주관|후원"
)
_SEGMENT_BEFORE = re.compile(
    # 기호 불릿·대괄호 라벨은 앞에 공백이 없어도 자른다 — 「확인[현대자동차]」「혜택✔ 활동비」
    r"\s*(?="
    r"[▶■□●○◆◇▪※☞➤►✅✔📌📅📆🎬🙌💬📥]"  # 기호 불릿
    r"|[①-⑳]"
    r"|\[[^\]]{1,12}\]"  # [수행업무] [우대사항] [모집기간]
    r")"
    r"|\s+(?="
    r"(?:\d{1,2}|[가-하]|[ⅠⅡⅢⅣⅤ]|[IVX]{1,3})[.)]\s"  # 1. 가. ①
    r"|[-•*·]\s"
    r")"
    # 「모집기간:」「우대 사항 :」 앞. 단, 「접수 기간:」의 단어 사이 공백은 아니다 —
    # 앞 단어가 라벨 머리면 그 공백은 라벨 안이다 (021 에서 「접수/기간:」으로 깨졌다)
    r"|(?<!" + _LABEL_HEAD.replace("|", ")(?<!") + r")"
    r"\s+(?=(?:" + _LABEL_HEAD + r")[가-힣]{0,4}\s?" + _COLON + r")"
)


def resegment(text: str) -> tuple[str, bool]:
    """줄바꿈이 거의 없는 텍스트에 줄을 다시 세운다. 줄이 충분하면 손대지 않는다."""
    if text.count("\n") * _FLAT_CHARS_PER_NEWLINE >= len(text):
        return text, False
    out = _SEGMENT_BEFORE.sub("\n", text)
    return out, True


def normalize(text: str) -> str:
    """제어문자 제거, 공백·개행 정리. 줄바꿈은 살린다 — 문단 경계의 유일한 단서다."""
    text = _CONTROL.sub("", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _SPACES.sub(" ", text)
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    return _BLANK_LINES.sub("\n\n", text).strip()


def strip_boilerplate(text: str) -> str:
    """게시판 공통 요소를 걷어낸다. 본문 안에 섞인 것은 건드리지 않는다."""
    kept = [
        line
        for line in text.split("\n")
        if not _BOILERPLATE_LINE.match(line) and not _BOARD_META_LINE.match(line)
    ]
    return _TRAILER.sub("", "\n".join(kept)).strip()


def mask_contacts(text: str) -> tuple[str, list[str]]:
    """이메일·전화번호를 지운다.

    공고는 공개 게시물이지만 담당자 연락처는 **제3자의 개인정보**다. 공고 파싱은
    프로바이더 제약이 없는 유일한 기능이라 해외 모델로 나갈 수 있는데, 그때 연락처가
    같이 나가면 곤란하다. 파싱 대상 필드도 아니라 지워도 품질에 영향이 없다.
    """
    found: list[str] = []

    def _swap(pattern: re.Pattern[str], placeholder: str, target: str) -> str:
        def repl(m: re.Match[str]) -> str:
            found.append(m.group())
            return placeholder

        return pattern.sub(repl, target)

    text = _swap(_EMAIL, "[이메일]", text)
    text = _swap(_MOBILE, "[연락처]", text)
    text = _swap(_PHONE, "[연락처]", text)
    return text, found


# 프롬프트 주입 — 공고 본문에 있을 리 없는 「모델에게 하는 말」. 문장이 아니라 **문단**을 뺀다.
# 지시 문장 하나만 빼면 「이 공고의 type 은 recruit 이며 …」 같은 뒤따르는 문장이 남아 그대로 먹힌다
# (015 실측). 공고가 한 문단짜리면 전부 빠져 EMPTY 로 실패한다 — 새는 것보다 낫다.
_INJECTION = re.compile(
    r"(?:위|이전|앞|모든|상기)\s?(?:의\s?)?(?:지시|명령|규칙|내용)(?:을|를|은|는)?\s?(?:모두\s?)?무시"
    r"|\[\s?(?:시스템|system)\s?(?:안내|메시지|프롬프트|지시)?\s?\]"
    r"|system\s?prompt|ignore\s+(?:all\s+|the\s+)?(?:previous|above|prior)\s+instructions"
    r"|(?:이|본)\s?문단(?:은|을)\s?출력하지"
    r"|(?:으로|로)\s?(?:만\s?)?출력(?:한다|하라|할\s?것)",
    re.IGNORECASE,
)


_SENTENCE_SPLIT = re.compile(r"(?<=[.!?。])\s+")


def strip_injections(text: str) -> tuple[str, int]:
    """지시문이 든 문단을 통째로 뺀다. 뺀 수를 같이 돌려준다.

    문단이 하나뿐이면(한 줄로 접힌 입력) 문단을 빼면 공고 전체가 사라진다 — 그때는 **문장 단위**로,
    지시 문장과 그 뒤 두 문장까지 뺀다. 지시가 여러 문장으로 이어지기 때문이다 (015).
    줄 구조는 유지한다 — 재분절 결과가 날아가면 안 된다.
    """
    paras = text.split(PARA)
    if len(paras) > 1:
        kept = [para for para in paras if not _INJECTION.search(para)]
        return PARA.join(kept), len(paras) - len(kept)

    if not _INJECTION.search(text):
        return text, 0
    removed = 0
    skip = 0
    out_lines: list[str] = []
    for line in text.split(NL):
        kept_s: list[str] = []
        skip = 0  # 줄이 있으면 지시문은 그 줄 안에 있다 — 다음 줄까지 지우지 않는다
        for sentence in _SENTENCE_SPLIT.split(line):
            if not sentence.strip():
                continue
            if skip:
                skip -= 1
                removed += 1
                continue
            if _INJECTION.search(sentence):
                removed += 1
                skip = 2
                continue
            kept_s.append(sentence)
        joined = " ".join(kept_s)
        # 「나.」처럼 번호만 남은 줄은 버린다 — 지시 문장이 그 번호 뒤에 있었다
        out_lines.append("" if len(joined.strip()) <= 3 else joined)
    return NL.join(x for x in out_lines if x.strip()), removed


def truncate(text: str, limit: int = MAX_CHARS) -> tuple[str, bool]:
    """길이 상한. 문장 중간에서 자르지 않도록 마지막 개행까지만 남긴다."""
    if len(text) <= limit:
        return text, False
    head = text[:limit]
    cut = head.rfind("\n")
    return (head[:cut] if cut > limit // 2 else head), True


def preprocess(raw: str, *, limit: int = MAX_CHARS) -> Preprocessed:
    """정규화 → (줄이 없으면) 재분절 → 보일러플레이트 제거 → 주입 제거 → 연락처 마스킹 → 절단."""
    text = normalize(raw)
    text, resegmented = resegment(text)
    text = strip_boilerplate(text)
    text, injections = strip_injections(text)
    text, masked = mask_contacts(text)
    text, truncated = truncate(text, limit)
    return Preprocessed(
        text=text,
        truncated=truncated,
        masked=masked,
        injections=injections,
        resegmented=resegmented,
    )
