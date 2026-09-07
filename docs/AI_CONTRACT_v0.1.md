# CareerCompass AI 서비스 — BE 계약 v0.1 (초안)

BE 가 이 서비스를 호출할 때의 요청·응답 모양을 정한다. 이슈 [#1](https://github.com/Team-CareerCompass/CareerCompass-AI/issues/1) 의 산출물이고, 이 문서가 서면 BE 의 M5(분석)·M6(지원서)가 시작될 수 있다.

> **상태: 초안.** 아래 「결정 기록」의 D1~D4 는 아직 확정 전이다. 확정되면 이 문단을 지운다.

---

## 0. 결정 기록

구현보다 먼저 정해야 했던 것들과, 현재 잡아 둔 기본값.

### D1. 이 서비스는 상태를 갖지 않는다 (stateless)

큐·재시도·작업 상태 관리는 **BE 가 한다.** 이 서비스는 요청 하나에 응답 하나를 동기로 돌려주는 순수 함수처럼 동작한다.

명세서가 공고 파싱을 「비동기」로 못박았지만, 그것은 **사용자 화면 기준의 비동기**다. BE 가 자기 워커에서 이 서비스를 동기로 부르면 그 요구는 충족된다. 이 서비스에 큐를 또 두면 작업 상태가 두 곳에 생기고, 장애가 났을 때 어느 쪽이 진실인지 알 수 없어진다.

지원서 생성의 스트리밍(SSE)도 **BE 가 FE 에게 여는 것**이고, 이 서비스는 항목 단위 생성 요청을 병렬로 받을 뿐이다(§3).

### D2. 임베딩 저장·검색은 BE 가 한다

이 서비스는 텍스트를 받아 벡터를 돌려준다(§5). pgvector 인덱싱과 유사도 검색은 BE 소유의 DB 에서 BE 가 한다.

**단, MVP 에서는 그것도 미룬다.** 적합도 산출(§2)과 초안 생성(§3)은 **BE 가 후보 경험 카드를 요청 본문에 실어 보내고** 이 서비스가 그 안에서 계산한다. 경험 카드는 사용자당 최대 30개다. 30개에 ANN 인덱스는 과설계고, 왕복이 한 번 줄어든다. 느려지면 그때 §5 로 옮긴다.

### D3. 기본안은 Bedrock 인리전 유지, HCX 는 검토 중

**보고서 4.4.1 의 전제는 성립한다.** 서울 리전(ap-northeast-2) 온디맨드 인리전 추론으로 확인된 텍스트 모델은 다음과 같다(2026-09-07 `bedrock:ListFoundationModels` 기준).

```
anthropic.claude-3-5-sonnet-20240620-v1:0
anthropic.claude-3-haiku-20240307-v1:0
twelvelabs.pegasus-1-2-v1:0
```

크로스리전·글로벌 프로파일을 쓰지 않고도 호출할 수 있으므로 개인정보 국외이전이 발생하지 않는다. **다만 쓸 수 있는 것이 2024년 모델 두 종뿐이다.** 최신 Claude 는 `apac.*` 프로파일로만 열려 있고 그것은 ap-northeast-1·ap-southeast-1·ap-southeast-2·ap-south-1 로도 라우팅되므로 **쓰지 않는다.**

기본안:

| 용도 | 모델 |
| --- | --- |
| 공고 파싱 · 문서 분류 | `anthropic.claude-3-haiku-20240307-v1:0` (인리전) |
| 지원서 초안 생성 | `anthropic.claude-3-5-sonnet-20240620-v1:0` (인리전) |

**HyperCLOVA X 를 검토 중이다.** 근거는 비용과 한국어다 — 국내 사업자라 국외이전 논쟁 자체가 없고, 출력 토큰 단가가 낮고, 입력이 전부 한국어라 토크나이저가 유리할 수 있다. 아래는 실측 전 추정이며(입력 3,000 / 출력 600 토큰, 지원서는 4항목 × 입력 3,000 / 출력 1,000 가정), 확정 시 실측으로 교체한다.

| 모델 | 공고 1건 파싱 | 지원서 1건 생성 |
| --- | --- | --- |
| Claude 3 Haiku (인리전) | 약 2.1원 | 약 10원 |
| Claude 3.5 Sonnet (인리전) | 약 24.7원 | 약 132원 |
| HCX-DASH-002 | 약 1.4원 | 약 7원 |
| HCX-005 | 약 6.8원 | 약 35원 |

파싱은 두 쪽 다 2원 안팎이라 **가격이 아니라 품질로 고를 자리**다. 생성은 132원 대 35원으로 갈리므로 **여기가 실제 판단 지점**이다.

**어느 쪽도 지금 고르지 않는다.** #2 게이트웨이는 두 프로바이더를 모두 어댑터로 두고, #5 평가셋으로 파싱 정확도와 실측 비용을 비교한 뒤 #28 에서 확정한다.

### D4. PII 경계는 기능별로 다르다

보고서 4.4.1 은 공고 본문과 자소서를 「동일하게」 처리한다고 했으나, 공고는 공개 텍스트고 자소서는 개인정보다. 같은 제약을 걸 이유가 없다.

| 엔드포인트 | 개인정보 | 프로바이더 제약 |
| --- | --- | --- |
| §1 파싱 | 없음 (공개 게시물) | 자유 — 품질·가격 우선 |
| §2 적합도 | **있음** (전공·학점·경험) | 국내 처리 고정 |
| §3 초안 생성 | **있음** (경험·과거 자소서) | 국내 처리 고정 |
| §4 문서 분류 | **있음** (자소서 전문) | 국내 처리 고정 |
| §5 임베딩 | **있음** (경험 카드) | 국내 처리 고정 |

「국내 처리 고정」은 **Bedrock 서울 인리전(ap-northeast-2) 또는 HCX** 를 뜻한다. `apac.*` 등 크로스리전·글로벌 프로파일은 어느 경우에도 쓰지 않는다(D3).

텍스트 추출·OCR 은 외부 호출 없이 로컬 라이브러리로만 한다(보고서 4.4.1 유지).

상세는 [#6](https://github.com/Team-CareerCompass/CareerCompass-AI/issues/6) 에서 필드 단위로 확정한다.

---

## 공통 규약

### 호출 방식

- 내부 네트워크 전용. 공개 인터넷에 노출하지 않는다.
- 인증은 공유 시크릿 헤더 `X-Internal-Token` 하나. 사용자 JWT 는 이 서비스로 넘어오지 않는다.
- `Content-Type: application/json`, UTF-8.
- `X-Request-Id` 를 BE 가 넣어 보내면 로그에 그대로 남긴다. 비용 추적(#28)의 키다.

### 응답 봉투

BE 의 `API_SPEC` 과 같은 모양을 쓴다. BE 가 그대로 감싸 내보낼 수 있게.

```json
{ "ok": true, "data": {} }
```

```json
{ "ok": false, "error": { "code": "PARSING_FAILED", "message": "핵심 키워드 3개 미만" } }
```

`code` 는 BE `API_SPEC` §9 의 코드를 쓴다. 이 서비스가 내는 것은 다음 다섯 개뿐이다.

| 코드 | HTTP | 언제 |
| --- | --- | --- |
| `INVALID_INPUT` | 400 | 필수 필드 누락·타입 불일치 |
| `PROFILE_INCOMPLETE` | 422 | 적합도 산출에 필요한 프로필이 없음 |
| `PARSING_FAILED` | 422 | 공고에서 유의미한 구조를 못 뽑음 |
| `RATE_LIMITED` | 429 | 일·월 비용 상한 초과 포함 |
| `LLM_UNAVAILABLE` | 503 | 프로바이더 장애·타임아웃 |

**부분 성공은 에러가 아니다.** `ok: true` 로 두고 `data.status` 로 알린다(§1.3).

### 타임아웃

BE 는 아래 값보다 넉넉하게 잡는다. 이 서비스는 이 시간 안에 반드시 응답한다(실패 응답이라도).

| 엔드포인트 | 목표 p95 | 상한 |
| --- | --- | --- |
| §1 파싱 | 8초 | 20초 |
| §2 적합도 | 3초 | 10초 |
| §3 초안 1항목 | 10초 | 30초 |
| §4 문서 분류 | 15초 | 40초 |
| §5 임베딩 | 1초 | 5초 |

목표치는 보고서 3.2 비기능 요구사항에서 가져왔다.

---

## 1. 공고 구조화 파싱

`POST /v1/parse-posting` — F3-1 · F4-1 · 이슈 #7 #8 #9 #10

공고 본문 하나를 구조화 필드로 바꾼다. **지원서 양식 인식(F4-1)도 여기서 함께 한다** — 같은 본문을 두 번 읽으면 비용이 두 배다.

### 1.1 요청

```json
{
  "postingId": 101,
  "title": "2026 카카오 SW 인턴십",
  "rawContent": "...공고 본문 전문...",
  "url": "https://...",
  "collectedAt": "2026-05-18T07:00:00+09:00",
  "images": [
    { "url": "s3://careercompass/postings/101/1.png", "order": 1 }
  ]
}
```

- `rawContent` 는 HTML 이 아니라 **텍스트**로 준다. HTML→텍스트 변환과 게시판 보일러플레이트(메뉴·푸터·이전글/다음글) 제거는 BE 수집 단계의 몫이다.
- 길이 상한 40,000자. 초과분은 이 서비스가 잘라내고 `data.truncated: true` 로 알린다.
- `images` — 본문 안의 이미지. **없으면 빈 배열.** 쓰임은 §1.5.

### 1.2 응답

```json
{
  "ok": true,
  "data": {
    "postingId": 101,
    "status": "ok",
    "truncated": false,
    "type": "recruit",
    "organization": "카카오",
    "dueDate": "2026-05-25",
    "dueDateRaw": "5월 25일(월) 23:59까지",
    "keywords": ["Spring", "Kotlin", "Redis"],
    "qualifications": { "year": "2학년 이상", "gpa": null, "major": null },
    "preferences": ["Java/Kotlin 백엔드 경험", "RDB 1년+"],
    "workType": "인턴",
    "formQuestions": [
      { "order": 1, "question": "지원 동기를 작성해 주세요", "maxChars": 500, "required": true }
    ],
    "meta": {
      "provider": "hcx",
      "model": "HCX-DASH-002",
      "promptVersion": "parse@1",
      "latencyMs": 4820,
      "costKrw": 1.4
    }
  }
}
```

필드는 BE `API_SPEC` §5 `GET /postings/{id}` 의 `parsed` 블록에 그대로 들어가게 맞췄다.

- `type` — `recruit` / `scholarship` / `contest` / `activity`. **초안 생성 버튼 노출을 BE 가 이 값으로 판단한다**(명세서 3.1: 장학금·채용만 노출).
- `dueDate` — ISO 날짜. **못 읽으면 `null`.** 추측해서 채우지 않는다(#8).
- `dueDateRaw` — 원문 표현. 화면에 그대로 보여주거나 사람이 검증할 때 쓴다.
- `qualifications` 의 개별 값도 없으면 `null`. **없는 조건은 「충족」으로 간주**하는 것이 명세서 규칙이고, 그 판단은 §2 에서 한다.
- `formQuestions` — 양식이 없으면 빈 배열. `maxChars` 를 못 찾으면 `null`.

### 1.3 처리 상태 3종

**BE #27 · FE 가 막혀 있는 계약이 이것이다.**

| `status` | 뜻 | BE 가 할 일 |
| --- | --- | --- |
| `ok` | 키워드 3개 이상 + 유형 판별됨 | 정상 저장. 적합도 산출로 진행 |
| `partial` | 일부만 뽑힘 (키워드는 있으나 마감일·양식 없음 등) | 저장하되 화면에 「일부 정보 없음」 표시 |
| `failed` | 키워드 3개 미만 | **`ok: false` + `PARSING_FAILED` 로 내려간다.** 원문만 보관 |

`partial` 일 때 `data.missing` 에 빠진 필드명을 배열로 준다: `["dueDate", "formQuestions"]`.

`failed` 일 때는 `error.detail.reason` 으로 사유를 구분한다. **BE 가 사용자에게 다른 말을 해야 하기 때문이다.**

| `reason` | 뜻 | 사용자에게 |
| --- | --- | --- |
| `NOT_A_POSTING` | 지원할 수 있는 공고가 아님 (아래) | **목록에 넣지 않는다** |
| `NO_KEYWORDS` | 텍스트는 있으나 구조를 못 뽑음 | 「분석하지 못했습니다」 |
| `IMAGE_ONLY` | 본문이 이미지뿐 (§1.5) | 「원문에서 확인해 주세요」 + 링크 |
| `EMPTY` | 본문이 사실상 비어 있음 (「붙임 파일 참조」 등) | 「원문에서 확인해 주세요」 + 링크 |

**`NOT_A_POSTING` 은 다른 셋과 성질이 다르다.** 나머지는 「공고이긴 한데 못 읽었다」이고, 이것은 「애초에 공고가 아니다」다. BE 는 이것만 **목록에서 제외**한다.

사용자는 게시판 URL 을 통째로 등록한다(F2-1). 학교 학사공지를 등록하면 「2026-1학기 수강신청 안내」·「졸업요건 변경 공지」·「등록금 납부 기간」 같은 것이 함께 수집된다. **이것들은 지원 대상이 아니다.** 판별 기준은 다음 셋을 모두 만족하는가다.

1. 개인이 **지원·신청할 수 있는** 대상인가
2. 모집 대상이나 자격이 명시되어 있는가
3. 마감일 또는 접수 기간이 있는가

셋 중 둘 이상이 아니면 `NOT_A_POSTING` 이다. 애매하면 **공고로 본다** — 놓치는 것보다 잘못 넣는 편이 사용자에게 낫다(잘못 들어온 것은 눈으로 넘기면 되지만, 안 들어온 것은 존재를 모른다).

### 1.4 실패와 재시도

- 프로바이더 일시 장애 → 이 서비스가 지수 백오프로 **3회까지 자체 재시도**한다. 그래도 안 되면 `LLM_UNAVAILABLE`.
- 스키마 위반 → **1회 재요청**. 그래도 안 되면 `PARSING_FAILED`.
- BE 는 `LLM_UNAVAILABLE` 만 나중에 다시 부른다. `PARSING_FAILED` 는 다시 불러도 같은 결과다.

### 1.5 본문이 이미지인 공고

공공기관 채용공고와 학교 장학 공지는 **본문이 이미지 한 장인 경우가 흔하다.** 명세서와 보고서 4.4.3 은 「비정형 텍스트로 수집된 공고 본문」만 전제하고 이 경우를 다루지 않는다. 여기서 정한다.

**BE 는 이미지를 버리지 않는다.** 본문에 `<img>` 가 있으면 Object Storage 에 저장하고 §1.1 의 `images` 에 실어 보낸다. 텍스트가 충분해 보여도 보낸다 — 판단은 이 서비스가 한다.

분기 규칙:

| 조건 | 처리 |
| --- | --- |
| `rawContent` 가 200자 이상 | 텍스트 경로. `images` 는 무시 |
| `rawContent` 가 200자 미만 **이고** `images` 가 있음 | **이미지 경로**(아래) |
| 둘 다 빈약 | `PARSING_FAILED`, `reason: "IMAGE_ONLY"` 또는 `"EMPTY"` |

**이미지 경로는 OCR 을 쓰지 않는다.** 멀티모달 LLM 에 이미지를 그대로 넣어 §1.2 의 구조화 필드를 바로 받는다.

- 공고는 **공개 정보**라 D4 의 PII 제약이 걸리지 않는다. 자소서 OCR 을 로컬 라이브러리로 묶어 둔 이유(보고서 4.4.1)가 여기엔 적용되지 않는다.
- PaddleOCR·Tesseract 로 텍스트를 먼저 뽑지 않는다. 공고 이미지는 표·다단 레이아웃이라 **OCR 이 읽는 순서를 뒤엉키게** 만든다. 자격요건 열과 우대사항 열이 섞이면 파싱은 그 뒤로 회복하지 못한다.
- 인리전에서 쓸 수 있는 두 모델(D3) 모두 이미지 입력을 지원한다. 이미지 1장은 대략 1,500~2,500 토큰이라 텍스트 공고와 비용 차이가 크지 않다(Haiku 약 1.7원 / Sonnet 약 20원, 추정).
- 이미지 4장까지만 본다. 그 이상은 앞 4장만 쓰고 `data.truncated: true`.

**구현 순서:** #7 에서는 이미지 경로를 만들지 않고 `IMAGE_ONLY` 로 실패시킨다. 텍스트 경로가 선 뒤에 별도 이슈로 붙인다. 계약에 자리를 지금 잡아 두는 것은 **BE 가 그때까지 이미지를 저장해 두게 하기 위해서다.**

---

## 2. 적합도 산출

`POST /v1/score` — F3-2 · F3-3 · 이슈 #11 #12 #13 #14

### 2.1 요청

D2 에 따라 **BE 가 프로필과 경험 카드를 실어 보낸다.**

```json
{
  "postingId": 101,
  "parsed": {},
  "profile": {
    "department": "컴퓨터공학부",
    "gpa": 3.87,
    "gradYear": 2027,
    "jobInterests": [{ "code": "backend", "priority": 1 }],
    "tags": ["AI", "스타트업"]
  },
  "experiences": [
    {
      "id": 5,
      "type": "project",
      "title": "CareerCompass",
      "startDate": "2025-09-01",
      "endDate": null,
      "data": { "role": "백엔드", "techs": ["Spring", "Kotlin"], "summary": "..." }
    }
  ]
}
```

- `parsed` 는 §1 의 응답을 그대로 되돌려 보낸다. 이 서비스는 공고를 저장하지 않는다(D1).
- `profile` 에 **이름·학번·연락처·학교명은 넣지 않는다.** 적합도 계산에 쓰이지 않는다(D4·#6).

### 2.2 응답

```json
{
  "ok": true,
  "data": {
    "score": 88,
    "label": "very_suitable",
    "reweighted": true,
    "breakdown": [
      { "axis": "field_similarity", "score": 95, "weight": 40, "basis": "관심분야 backend ↔ 공고 키워드 Spring·Kotlin" },
      { "axis": "qualification", "score": 88, "weight": 30, "basis": "2학년 이상 충족, 학점 조건 없음" },
      { "axis": "preference", "score": 78, "weight": 20, "basis": "우대 2건 중 1건 일치 (경험 #5)" },
      { "axis": "competition", "score": null, "weight": 10, "basis": null }
    ],
    "strengthComment": "Spring·Kotlin 백엔드 프로젝트 경험이 공고 우대 조건과 직접 맞습니다.",
    "weaknessComment": "RDB 1년 이상 경력을 확인할 수 있는 경험이 없습니다.",
    "citedExperienceIds": [5],
    "meta": {}
  }
}
```

- `label` — `very_suitable`(80+) / `suitable`(60~79) / `moderate`(40~59) / `low`(39 이하). 경계값은 보고서 4.4.4.
- **`score` 는 LLM 이 정하지 않는다.** 4축 가중합·정규화·자격 충족률은 코드로 계산한다(보고서 4.4.4). LLM 은 분야 유사도의 임베딩과 코멘트 문장에만 관여한다.
- **`competition` 축은 근거가 없으면 `null` 이다**(#12). 모집 규모가 공고에 없으면 추정하지 않는다. `null` 인 축은 가중치를 나머지 축에 비례 배분하고, 그 사실을 `reweighted: true` 로 알린다.
- **`citedExperienceIds` 에 없는 경험은 코멘트에 등장할 수 없다**(#14·#29). 계산에 쓴 근거만 인용한다.

### 2.3 산출 불가

관심분야·경험 카드가 **둘 다 비어 있으면** 점수를 내지 않는다(명세서 3.1).

```json
{
  "ok": false,
  "error": {
    "code": "PROFILE_INCOMPLETE",
    "message": "관심 분야 또는 경험 카드가 최소 1개 필요합니다",
    "detail": { "missing": ["jobInterests", "experiences"] }
  }
}
```

경험이 0개지만 관심분야가 있으면 **분야 유사도만으로 잠정 점수**를 내고 `data.provisional: true` 를 붙인다(보고서 4.4.4). FE 가 「프로필 보강 권장」을 띄우는 신호다.

---

## 3. 지원서 초안 생성

`POST /v1/generate-item` — F4-2 · F4-3 · 이슈 #16 #17 #18 #19 #20

**항목 1개당 1회 호출이다.** BE 가 항목 수만큼 병렬로 부르고, 끝나는 대로 FE 에 SSE 로 흘린다(D1). 재생성도 같은 엔드포인트다.

### 3.1 요청

```json
{
  "question": "지원 동기를 작성해 주세요",
  "maxChars": 500,
  "tone": "formal",
  "parsed": {},
  "profile": {},
  "experiences": [],
  "emphasizeExperienceIds": [5, 12],
  "pastApplicationSamples": ["...과거 자소서 본문 일부..."],
  "previousAnswer": null
}
```

- `tone` — `formal` / `casual` 둘뿐. **문체만 달라지고 사실관계는 같아야 한다**(#18).
- `emphasizeExperienceIds` — 재생성 시 사용자가 고른 카드. 비었으면 이 서비스가 우선순위 규칙으로 고른다(§3.3).
- `pastApplicationSamples` — 톤·문체 참조용. **최대 2건, 각 2,000자.** 개인정보가 가장 진한 필드라 필요한 만큼만 받는다(#6).
- `previousAnswer` — 재생성일 때 직전 답변. **이것과 눈에 띄게 다른 글을 만든다**(#19).

### 3.2 응답

```json
{
  "ok": true,
  "data": {
    "answer": "...",
    "charCount": 487,
    "usedExperienceIds": [5],
    "factCheck": { "passed": true, "unverified": [] },
    "meta": {}
  }
}
```

- **`charCount` 는 `maxChars` 를 넘지 않는다.** 프롬프트로 부탁하는 것이 아니라 생성 후 실측해서, 초과하면 재생성하거나 문장 단위로 줄인다(#16). `maxChars` 가 `null` 이면 400~600자로 만든다.
- `factCheck.unverified` — 생성물에 나왔는데 입력(경험 카드·공고)에서 확인되지 않은 고유명사·수치·기간(#29). **비어 있지 않아도 응답은 내보내되, FE 가 표시할 수 있게 전달한다.** 이 판정은 LLM 이 아니라 문자열 대조 규칙으로 한다.

### 3.3 경험 카드 우선순위

보고서 4.4.5 의 3단 규칙을 그대로 구현한다(#17).

1. 공고 핵심 키워드와의 유사도
2. 우대 조건 직접 매칭
3. 최신 카드

`emphasizeExperienceIds` 가 오면 그것을 1순위 앞에 놓는다.

---

## 4. 과거 지원서 항목 분류

`POST /v1/classify-document` — F1-4 · 이슈 #21 #22 #23

### 4.1 요청

```json
{ "pastApplicationId": 7, "text": "...추출된 전문...", "label": "2024 카카오 인턴 자소서" }
```

**파일이 아니라 텍스트를 받는다.** 파일에서 텍스트를 꺼내는 것은 §4.3 의 별도 엔드포인트다. BE 는 §4.3 을 먼저 부르고, 그 결과를 여기로 넘긴다.

둘로 나눈 이유는 **실패의 성질이 다르기 때문**이다. 텍스트 추출 실패는 파일 문제라 사용자가 다른 파일을 올리면 되고, 분류 실패는 모델 문제라 사용자가 할 수 있는 일이 없다. 한 엔드포인트로 묶으면 BE 가 둘을 구분할 수 없다.

### 4.2 응답

```json
{
  "ok": true,
  "data": {
    "items": [
      { "order": 1, "category": "motivation", "content": "...", "confident": true },
      { "order": 2, "category": "other", "content": "...", "confident": false }
    ],
    "meta": {}
  }
}
```

- `category` — `motivation`(지원 동기) / `background`(성장 배경) / `experience`(경험 기술) / `competency`(직무 역량) / `aspiration`(입사 후 포부) / `other`(기타). BE `API_SPEC` §4 의 값과 일치시킨다.
- **문단 단위로 자른다.** 개행·번호·질문 헤더 기준 **규칙 분할**이고 LLM 을 쓰지 않는다. 유료 문단 분리 API 도 쓰지 않는다.
- **애매하면 `other` + `confident: false`.** 억지로 분류하지 않는다. 사용자가 고친다(#23).

### 4.3 텍스트 추출

`POST /v1/extract-text` — `multipart/form-data` · 이슈 #21

BE 가 Object Storage 에서 파일을 받아 넘긴다. **외부로 나가는 호출이 없다** — PDF·DOCX·TXT 추출과 스캔 PDF 의 OCR 을 전부 로컬 라이브러리로 처리한다(보고서 4.4.1). 자소서는 개인정보가 가장 진한 데이터라 모델 호출 이전 단계에서 밖으로 내보내지 않는다.

요청 필드:

| 이름 | 값 |
| --- | --- |
| `file` | 바이너리. **10MB 이하**, PDF·DOCX·TXT (명세서 F1-4) |
| `pastApplicationId` | 정수 |

```json
{
  "ok": true,
  "data": {
    "pastApplicationId": 7,
    "format": "pdf",
    "text": "...추출된 전문...",
    "charCount": 4821,
    "ocrUsed": false,
    "meta": { }
  }
}
```

- `format` — `pdf` / `docx` / `txt`.
- `ocrUsed` — 텍스트 레이어가 없어 OCR 로 읽었으면 `true`. **품질이 낮을 수 있다는 신호**라 BE 가 화면에 표시할 수 있게 준다.
- **줄바꿈을 살린다.** 문단 구분이 사라지면 §4.2 가 항목 경계를 찾을 수 없다(#21).
- **빈 문자열을 성공으로 내보내지 않는다.** 그러면 분류가 「항목 없음」으로 조용히 넘어간다.

실패는 성질에 따라 둘로 갈린다.

| 상황 | 응답 |
| --- | --- |
| 지원하지 않는 형식 · 10MB 초과 | `INVALID_INPUT` (400) |
| 텍스트 레이어가 없고 OCR 도 실패 | `PARSING_FAILED` (422) · `reason: "SCANNED_PDF"` |
| 추출은 됐으나 내용이 사실상 없음 | `PARSING_FAILED` (422) · `reason: "EMPTY"` |

`SCANNED_PDF` 는 명세서 UC-04 의 E3(「추출 불가」 상태)에 대응한다. 사용자가 다른 파일을 올리면 해결되므로 BE 는 그렇게 안내한다.

---

## 5. 임베딩

`POST /v1/embed` — 이슈 #11 보조

D2 에 따라 MVP 에서는 쓰지 않는다. 인터페이스만 미리 고정해 둔다.

```json
{ "texts": ["...", "..."], "purpose": "experience" }
```

```json
{ "ok": true, "data": { "vectors": [[0.1]], "dim": 1024, "model": "clir-emb-dolphin" } }
```

`dim` 이 바뀌면 BE 의 pgvector 컬럼을 바꿔야 한다. **모델 교체는 반드시 이슈로 먼저 알린다.**

---

## 6. 헬스체크

`GET /health` → `{ "ok": true, "data": { "version": "0.1.0", "providers": { "hcx": "up" } } }`

프로바이더를 실제로 호출하지는 않는다(비용). 키 존재와 프로세스 상태만 본다.

---

## 7. 아직 안 정한 것

| 항목 | 이슈 | 막고 있는 것 |
| --- | --- | --- |
| 프로바이더 확정 (Bedrock 인리전 / HCX) 및 실측 비용 | #5 #28 | D3 표의 추정치 |
| 유사 공고 3건 산출을 누가 하나 | #15 | `GET /postings/{id}` 의 `similar` |
| For You 추천·로드맵·Export 요약 | #24 #25 #26 | `API_SPEC` §7 전체 |
| 마스킹 대상 필드 확정 | #6 | §3 의 `pastApplicationSamples` |
| 이미지 공고 멀티모달 경로 (§1.5) | #33 | 공공기관 채용·장학 공지 상당수 |
| 첨부 HWP·PDF 를 열어볼 것인가 | #34 | 「붙임 파일 참조」 공고 |

---

## 변경 이력

- v0.1 — 초안. BE `API_SPEC_v0.1` §2~§7 의 필드 모양에 맞춰 작성. D1~D4 미확정.
