# CareerCompass-AI

건국대학교 졸업 프로젝트 **CareerCompass** 의 AI/NLP 서비스.

공고 본문을 구조화하고, 적합도의 강점·약점 코멘트를 쓰고, 경험 요약을 근거로 자기소개서 초안을 만드는 부분이 여기 있다.

담당은 **서성덕 (@kislevite)** — 2026-05-18 제안서 「팀원 정보」의 역할이 AI/NLP 다.

> 2026-03-06 제안서(`… – CamBridge`)는 **폐기된 주제의 문서**다. 거기 적힌 역할 분담은 현행이 아니다.

**정본은 [`docs/AI_CONTRACT_v0.2.md`](docs/AI_CONTRACT_v0.2.md) 다.** BE 가 무엇을 보내고 무엇을 받는지, 그리고 왜 그렇게 정했는지가 거기 있다.

## 현재 상태

**골격이 서 있고 계약은 v0.2 다.** 엔드포인트 셋이 더미 응답을 낸다. 모델은 아직 부르지 않는다.

> **v0.2 에서 범위가 줄었다.** [BE #51](https://github.com/Team-CareerCompass/CareerCompass-BE/pull/51)(백엔드 전체 구현)이 적합도 산출·문서 분류·텍스트 추출·유사 공고·추천·캐싱·큐를 이미 구현했다. 중복해서 만들지 않고 **BE 의 `HttpLlmGateway` 가 부르는 모양에 맞췄다.** 자세한 내역은 계약 §0.

| 엔드포인트 | 무엇 | 이슈 |
| --- | --- | --- |
| `POST /v1/parse-posting` | 공고 본문 → 구조화 필드 + 지원서 양식 | #7 #8 #10 #33 |
| `POST /v1/comments` | 적합도 근거 → 강점·약점 두 문장 | #14 |
| `POST /v1/draft-answer` | 질문 + 경험 요약 → 항목 초안 하나 | #16 #18 #19 |

### 마일스톤

| | 범위 | 상태 |
| --- | --- | --- |
| **A0 기반** | 골격·게이트웨이·프롬프트 관리·평가셋·PII | #1 완료 · #2 #3 #5 #6 #35 남음 |
| **A1 공고 파싱** | 구조화 파싱·마감일·양식 인식·이미지 공고 | 계약 확정 · **다음 작업** |
| **A2 적합도** | 강점·약점 코멘트 | #14 만 남음 — 점수는 BE |
| **A3 지원서 생성** | 항목별 초안·톤·재생성 | #16 #18 #19 |
| **A4 문서 분류** | — | **BE 가 구현함** |
| **A5 추천·로드맵** | — | **BE 가 구현함** |
| **A6 운영·품질** | 비용 측정·가드·배포 | #28 #29 #31 |

### 다음에 할 것

1. **#6 PII 문서** — BE 가 `PrivacyMasker` 로 마스킹해 보내므로 범위가 줄었다. 이쪽에서 **프로바이더로 나가는 것**만 정하면 된다.
2. **#2 게이트웨이 + #3 프롬프트 외부화 + #35 리플레이 캐시** — 한 덩어리다. 따로 하면 캐시 키를 두 번 고친다.
3. **#7 공고 파싱** — 착수 전에 `fixtures/postings/` 를 채운다. 지금 8건.

## 띄우기

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"   # Windows: .venv\Scripts\pip
uvicorn app.main:app --reload
curl localhost:8000/health
```

또는 `docker compose up --build`.

**인증 헤더는 필요 없다.** BE 가 토큰을 보내지 않으므로 기본값이 꺼짐이고, 운영에서는 네트워크 경계로 막는다(#31).

```bash
curl -X POST localhost:8000/v1/parse-posting \
  -H 'Content-Type: application/json' -H 'X-Prompt-Version: v1' \
  -d '{"title":"테스트 공고","rawContent":"..."}'
```

BE 쪽에서는 `app.ai.base-url` 만 채우면 `HeuristicLlmGateway` 대신 `HttpLlmGateway` 가 켜진다.

### 더미 응답을 고르는 법

고정 응답만 내려주면 BE 가 실패 경로를 짤 수 없다. BE 는 `postingId` 를 보내지 않으므로 **제목의 표식**으로 고른다.

```json
{"title": "[stub:NO_KEYWORDS] 아무거나", "rawContent": "..."}
```

| 표식 | 결과 |
| --- | --- |
| `[stub:NO_KEYWORDS]` | 파싱 실패 — 키워드 부족 |
| `[stub:IMAGE_ONLY]` | 파싱 실패 — 본문이 이미지뿐 |
| `[stub:EMPTY]` | 파싱 실패 — 「붙임 파일 참조」 |
| `[stub:NOT_A_POSTING]` | 파싱 실패 — 공고가 아님 |
| `[stub:PARTIAL]` | 성공하되 마감일·양식 없음 |
| 표식 없음 | 정상 |

**파싱 실패도 `200` 이다.** BE 의 `callWithRetry` 가 429·5xx 만 재시도하고 나머지는 `LLM_UNAVAILABLE` 로 바꾸기 때문에, 4xx 로 내면 정상적인 파싱 실패가 서버 장애로 둔갑한다.

타임아웃 처리를 시험하려면 `?delay=15000` (밀리초). BE 의 기본 타임아웃이 20초다.

## 정해진 것

근거는 계약 §0 에 있다.

- **이 서비스는 상태를 갖지 않는다.** 큐·캐시·재시도·작업 상태·SSE·버전 보관은 전부 BE 가 한다.
- **점수는 BE 가 낸다.** 4축 가중 산출은 `SuitabilityCalculator` 다. 이쪽은 BE 가 계산에 쓴 근거를 받아 문장으로 옮긴다.
- **경험 카드는 정렬되어 온다.** 우선순위 규칙도 BE 가 적용했다. id 대신 **인덱스**로 무엇을 인용했는지 답한다.
- **PII 는 BE 가 마스킹해서 보낸다.** 이쪽에서 다시 지우지 않는다. 남은 판단은 「프로바이더로 무엇을 보내는가」다(#6).
- **자소서 항목 분류는 BE 가 한다.** 보고서 4.4.2 는 프롬프트 기반 LLM 분류로 결론냈지만, BE #51 이 자체 구현했다. KoBERT 파인튜닝은 어느 쪽으로도 기본안이 아니다.
- **파이썬 서비스인 이유**는 파인튜닝 때문이 아니라 프롬프트·평가셋을 빠르게 돌려야 하기 때문이다(#5).

## 아직 안 정한 것

| 항목 | 이슈 | 막고 있는 것 |
| --- | --- | --- |
| 프로바이더 확정 — Bedrock 인리전 / HyperCLOVA X | #5 #28 | 비용 추정치가 실측이 아니다 |
| 이미지 공고 멀티모달 (계약 §1.4) | #33 | BE 가 `images` 를 보내야 한다 |
| 첨부 HWP·PDF | #34 | 「붙임 파일 참조」 공고 |
| `NOT_A_POSTING` 제외 경로 | — | BE 에 목록 제외 수단이 없다 |

**프로바이더**는 서울 리전 인리전 온디맨드에 2024년 모델 두 종(Claude 3.5 Sonnet · Claude 3 Haiku)만 있다는 것을 확인했다. 인리전 추론 자체는 성립하므로 보고서 4.4.1 의 전제는 지켜진다. 다만 지원서 생성이 건당 132원 대 35원으로 갈려서 HCX 를 함께 검토 중이다. **#2 게이트웨이는 두 프로바이더를 모두 어댑터로 두고, #5 평가셋으로 비교한 뒤 고른다.**

## 다른 저장소와의 관계

| 저장소 | 담당 | 상태 |
| --- | --- | --- |
| [CareerCompass-FE](https://github.com/Team-CareerCompass/CareerCompass-FE) | 정일혁 (@1hyok) · 이준혁 (@Sadturtleman) | Android. 이슈 149건 닫힘, §6 에디터·§7 진행 중 |
| [CareerCompass-BE](https://github.com/Team-CareerCompass/CareerCompass-BE) | 조영탁 (@Tak002) | M0~M9 전체 구현([PR #51](https://github.com/Team-CareerCompass/CareerCompass-BE/pull/51)) |

**BE 가 이 서비스를 호출한다.** AI 서버가 없어도 BE 는 온프레미스 휴리스틱으로 동작한다 — 이 서비스는 **품질을 올리는 쪽**이지 없으면 멈추는 쪽이 아니다.

## 규약

- **BE 의 `LlmGateway` · `HttpLlmGateway` 가 정본 근거다.** 그쪽이 바뀌면 계약과 `app/schemas.py` 를 고친다.
- **계약과 `app/schemas.py` 는 함께 고친다.** 한쪽만 바꾸지 않는다.
- 스펙과 다르게 구현하기로 정했으면 해당 저장소에 이슈로 알린다. 조용히 다르게 만들지 않는다.
- 프롬프트를 고치면 평가셋을 돌린다(#5). 눈으로 두세 개 보고 넘어가지 않는다.
