# CareerCompass AI 서비스 — BE 계약 v0.2

BE 가 이 서비스를 호출할 때의 요청·응답 모양.

> **v0.1 은 폐기됐다.** 그 문서는 BE 가 아직 코드가 없던 시점에 이쪽에서 먼저 그린 계약이었는데, 같은 시기에 BE 도 [BE #51](https://github.com/Team-CareerCompass/CareerCompass-BE/pull/51)(백엔드 전체 구현, +15,172줄)에서 `HttpLlmGateway` 로 자기 쪽 계약을 그렸다. 서로 모르고 각자 그린 것이라 봉투·인증·실패 표현·엔드포인트 이름이 전부 어긋나 있었다.
>
> **BE 쪽에 맞춘다.** BE 는 구현과 통합 테스트 121개가 이미 서 있고 이쪽은 스텁뿐이라, 바꾸는 비용이 비교가 안 된다. 그리고 BE 설계가 실제로 더 튼튼하다 — AI 서버가 죽어도 온프레미스 휴리스틱으로 점수가 나온다.
>
> 이 문서의 정본 근거는 BE 저장소의 `analysis/gateway/HttpLlmGateway.java` 와 `LlmGateway.java` 다. **그쪽이 바뀌면 여기를 고친다.**

---

## 0. 경계

### 이 서비스가 하는 것

엔드포인트 **셋**뿐이다.

| 엔드포인트 | 무엇 | 이슈 |
| --- | --- | --- |
| `POST /v1/parse-posting` | 공고 본문 → 구조화 필드 + 지원서 양식 | #7 #8 #10 #33 |
| `POST /v1/comments` | 적합도 근거 → 강점·약점 두 문장 | #14 |
| `POST /v1/draft-answer` | 질문 + 경험 요약 → 항목 초안 하나 | #16 #18 #19 |

### BE 가 하는 것

v0.1 에서 이쪽이 하겠다고 적었으나 **BE #51 이 이미 구현한 것들**이다. 중복해서 만들지 않는다.

| 기능 | BE 구현 |
| --- | --- |
| 적합도 4축 가중 산출·레이블 | `SuitabilityCalculator` (설정형 가중치) |
| 경험 카드 우선순위 정렬 | 매칭도 순으로 정렬해 `experienceSummaries` 로 넘겨준다 |
| 과거 지원서 텍스트 추출 | pdfbox/poi 온프레미스. 응답에 `extractStatus`(done\|failed) |
| 자소서 항목 분류 6종 | BE 자체 구현. 어휘는 `background` 로 통일 |
| 유사 공고 3건 | 동일 유형 + 키워드 겹침 + 적합도 순 |
| For You · 로드맵 · 강점 Export | M7 전체 |
| 캐싱 · 일일 호출 예산 · 재시도 · 백오프 | `HttpLlmGateway` |
| 작업 큐 · 상태 · SSE · 버전 보관 | M4 `parseStatus`, M6 SSE·버전 3개 |
| 개인정보 마스킹 | `PrivacyMasker` — 이쪽으로 오기 전에 마스킹된다 |

**이 서비스는 상태를 갖지 않는다.** 요청 하나에 응답 하나. 큐도 캐시도 재시도도 BE 가 한다.

### 상태 축이 둘이라는 것

BE 의 `parseStatus`(`pending`\|`done`\|`failed`)는 **작업 진행** 상태고, 이 서비스가 내는 `parsingFailed` 는 **파싱 품질**이다. 축이 다르니 충돌이 아니라 매핑이다.

| 이 서비스 | BE `parseStatus` |
| --- | --- |
| 정상 응답 | `done` |
| `parsingFailed: true` | `failed` |
| (호출 전·진행 중) | `pending` — BE 큐 상태. 이쪽은 관여하지 않는다 |

적합도의 `scoreUnavailableReason`(`profile_incomplete`\|`parsing_failed`\|`analyzing`)도 **BE 가 판단해 데이터 필드로 내보낸다.** 이 서비스는 점수를 내지 않으므로 관여하지 않는다.

---

## 공통 규약

### 호출 방식

- 내부 네트워크 전용. 공개 인터넷에 노출하지 않는다.
- `Content-Type: application/json`, UTF-8.
- BE 가 `X-Prompt-Version` 헤더를 보낸다. **로그와 `usage` 에 그대로 남긴다** — BE 가 재파싱 판단에 쓴다.
- **인증 헤더는 요구하지 않는다.** BE 는 토큰을 보내지 않는다. `CC_REQUIRE_INTERNAL_TOKEN=true` 로 켤 수 있지만 기본값은 꺼짐이고, 운영에서는 **네트워크 경계로 막는다**(#31).

### 응답 봉투가 없다

**최상위에 필드를 그대로 놓는다.** BE 가 `node.path("keywords")` 로 바로 읽는다. `{ok, data}` 로 감싸면 BE 는 필드를 못 찾고 `LLM_UNAVAILABLE` 을 던진다.

### 실패를 표현하는 법

BE 의 `callWithRetry` 는 **429 와 5xx 만 재시도**하고, 그 밖의 오류 응답은 전부 `LLM_UNAVAILABLE`(503) 로 바꿔 버린다. 그래서 **정상적인 파싱 실패를 4xx 로 내면 서버 장애로 둔갑한다.**

| 상황 | 내는 것 |
| --- | --- |
| 파싱 실패 (공고가 아님·키워드 부족·이미지뿐) | **`200` + `parsingFailed: true`** |
| 호출 상한 초과 | `429` — BE 가 백오프 후 재시도 |
| 프로바이더 장애·타임아웃 | `503` — BE 가 백오프 후 재시도 |
| 요청이 계약과 다름 | `400` — BE 가 `LLM_UNAVAILABLE` 로 바꾼다(재시도 없음). 버그라는 뜻이다 |

오류 본문은 `{"error": "...", "code": "..."}` 로 낸다. BE 는 본문을 읽지 않고 상태 코드만 본다.

### `usage`

모든 성공 응답에 싣는다. BE 가 `usage.totalTokens` 를 로깅한다(`ai-usage` 로그).

```json
"usage": {
  "provider": "hcx",
  "model": "HCX-DASH-002",
  "promptVersion": "v1",
  "promptTokens": 3120,
  "completionTokens": 604,
  "totalTokens": 3724,
  "latencyMs": 4820,
  "costKrw": 1.4
}
```

BE 는 `totalTokens` 만 읽는다. 나머지는 이쪽의 비용 집계(#28)용이고 BE 가 무시해도 무해하다.

### BE 쪽 설정 — 어디에도 안 적혀 있다

BE #51 의 `application*.yml` 에는 `app.ai.*` 가 **없다.** `HttpLlmGateway` 는 `@ConditionalOnProperty("app.ai.base-url")` 라 **환경변수 `APP_AI_BASE_URL` 을 주어야 켜진다.** 안 주면 `HeuristicLlmGateway` 가 조용히 돈다 — 실서버에서 AI 가 안 붙은 채로 점수가 나오는 것이 이 때문일 수 있다.

| 환경변수 | 기본 (`AiProperties`) |
| --- | --- |
| `APP_AI_BASE_URL` | 없음 → 휴리스틱 |
| `APP_AI_TIMEOUT` | `20s` |
| `APP_AI_MAX_RETRIES` | `2` |
| `APP_AI_PROMPT_VERSION` | `v1` — 헤더로 오고 BE 파싱 캐시 키에 들어간다. 이쪽 프롬프트를 v2 로 올려도 BE 가 이 값을 안 바꾸면 BE 인메모리 캐시는 안 지워진다(재시작 전까지) |
| `APP_AI_DAILY_CALL_BUDGET` | `2000` 회 |

### 타임아웃

BE 의 기본 읽기 타임아웃이 **20초**(`app.ai.timeout`)이고 재시도는 2회다. 이 서비스는 그 안에 반드시 응답한다 — 실패 응답이라도.

| 엔드포인트 | 목표 p95 | 상한 |
| --- | --- | --- |
| `/v1/parse-posting` | 8초 | 18초 |
| `/v1/comments` | 3초 | 10초 |
| `/v1/draft-answer` | 10초 | 18초 |

목표치는 보고서 3.2 비기능 요구사항에서 가져왔다. **상한을 BE 타임아웃보다 낮게 잡는다** — 이쪽이 먼저 포기해야 BE 가 이유를 안다.

---

## 1. 공고 구조화 파싱

`POST /v1/parse-posting` — F3-1 · F4-1 · 이슈 #7 #8 #10

지원서 양식 인식(F4-1)도 여기서 함께 한다. 같은 본문을 두 번 읽으면 비용이 두 배다.

### 1.1 요청

```json
{
  "title": "2026 카카오 SW 인턴십",
  "rawContent": "...공고 본문 전문..."
}
```

BE 는 이 둘만 보낸다. 아래는 **선택 필드**로 받되 없어도 동작한다.

| 필드 | 쓰임 |
| --- | --- |
| `images` | 본문이 이미지인 공고 (§1.4). BE 가 아직 안 보낸다 — #33 |
| `collectedAt` | 마감일에 연도가 없을 때의 기준 (#8). 없으면 오늘 |
| `postingId` | 로그 상관관계용 |

- `rawContent` 는 HTML 이 아니라 **텍스트**다. HTML→텍스트 변환과 보일러플레이트 제거는 BE 수집 단계의 몫이다.
- **실제 모양은 Jsoup `body().text()` 다** (BE `CrawlService.fetchDetailText`) — 줄바꿈이 전부 공백으로 접히고 메뉴·푸터·게시판 목록까지 body 전체가 한 줄로 온다. 이쪽은 줄이 거의 없으면 불릿·번호·「라벨:」 앞에서 줄을 다시 세운다(`preprocess.resegment`). 평가는 `--shape flat` 으로 이 모양도 같이 잰다. 본문 영역만 추출하는 것(메뉴 제거)은 BE 몫으로 남아 있다.
- 길이 상한 40,000자. 초과분은 잘라내고 `truncated: true` 로 알린다.

### 1.2 성공 응답

```json
{
  "parsingFailed": false,
  "type": "recruit",
  "keywords": ["Spring", "Kotlin", "Redis"],
  "qualificationYear": "2학년 이상",
  "qualificationGpa": null,
  "qualificationMajor": null,
  "preferences": ["Java/Kotlin 백엔드 경험", "RDB 1년+"],
  "dueDate": "2026-05-25",
  "dueDateRaw": "5월 25일(월) 23:59까지",
  "formQuestions": [
    { "order": 1, "question": "지원 동기를 작성해 주세요", "maxChars": 500 }
  ],
  "truncated": false,
  "usage": { }
}
```

BE 의 `ParsedPosting` 레코드와 필드 이름이 1:1 이다. **자격 조건은 평평하게** 놓는다(`qualificationYear` / `qualificationGpa` / `qualificationMajor`) — 중첩 객체가 아니다.

- `type` — `recruit` / `scholarship` / `contest` / `activity` / `other`. **재분류에 실패하면 `null`** 이고, 그러면 BE 가 게시판 등록 시 지정한 유형을 유지한다.
- `keywords` — 최대 10개. **3개 미만이면 성공 응답이 아니라 §1.3 이다.** BE 도 3개 미만이면 실패로 다시 판정하므로 여기서 먼저 거른다.
- `dueDate` — `YYYY-MM-DD`. **못 읽으면 `null`.** 「상시 모집」도 `null` 이다. 추측해서 채우지 않는다(#8). BE 는 `LocalDate.parse` 를 재시도 밖에서 부른다 — ISO 가 아니면 BE 가 500 이다. 이쪽은 `date.isoformat()` 으로만 낸다.
- `dueDateRaw` — 원문 표현. BE 는 읽지 않지만 평가셋에서 사람이 검증할 때 쓴다.
- `formQuestions[].maxChars` — 못 찾으면 `null`. 양식이 없으면 배열이 비어 있다. **「없음」과 「못 찾음」을 가른다**(#10).

### 1.3 파싱 실패

**`200` 으로 낸다.** 4xx 로 내면 BE 가 서버 장애로 오인한다.

```json
{
  "parsingFailed": true,
  "reason": "핵심 키워드를 3개 이상 뽑지 못했습니다",
  "reasonCode": "NO_KEYWORDS",
  "usage": { }
}
```

BE 는 `reason` 을 사람이 읽는 문장으로 쓴다(`ParsingFailedException` 의 메시지). `reasonCode` 는 이쪽 평가셋과 로그용이고 BE 는 무시한다.

| `reasonCode` | 언제 |
| --- | --- |
| `NO_KEYWORDS` | 텍스트는 있으나 키워드 3개를 못 뽑음 |
| `IMAGE_ONLY` | 본문이 이미지뿐 (§1.4) |
| `EMPTY` | 본문이 사실상 비어 있음 (「붙임 파일 참조」 등) |
| `NOT_A_POSTING` | 지원할 수 있는 공고가 아님 (아래) |

#### `NOT_A_POSTING` — 남아 있는 구멍

사용자는 게시판 URL 을 통째로 등록한다(F2-1). 학교 학사공지를 등록하면 「2026-1학기 수강신청 안내」·「졸업요건 변경 공지」가 함께 수집된다. **지원 대상이 아니다.**

판별 기준 — 다음 셋 중 둘 이상을 만족하는가.

1. 개인이 **지원·신청할 수 있는** 대상인가
2. 모집 대상이나 자격이 명시되어 있는가
3. 마감일 또는 접수 기간이 있는가

애매하면 **공고로 본다.** 잘못 들어온 것은 눈으로 넘기면 되지만, 안 들어온 것은 존재를 모른다.

> **BE 에 이것을 제외할 경로가 없다.** `parsingFailed` 는 전부 `parseStatus: failed` 로 떨어져 목록에 남는다. 「분석 실패」로 보이는 것과 「애초에 공고가 아닌 것」이 같은 자리에 섞인다. 지금은 이대로 두고, 학사공지 표본이 쌓이면 BE 와 다시 이야기한다.

### 1.4 본문이 이미지인 공고

공공기관 채용공고와 학교 장학 공지는 본문이 이미지 한 장인 경우가 흔하다. 명세서와 보고서 4.4.3 은 「비정형 텍스트로 수집된 공고 본문」만 전제한다.

**지금은 `IMAGE_ONLY` 로 실패시킨다.** 멀티모달 경로는 #33 이고, 그때 BE 가 `images` 를 실어 보내야 한다.

붙일 때의 방침은 정해 뒀다 — **OCR 을 쓰지 않고 멀티모달 LLM 에 이미지를 그대로 넣는다.** 공고 이미지는 표·다단 레이아웃이라 OCR 이 읽는 순서를 뒤엉키게 만들고, 자격요건 열과 우대사항 열이 섞이면 그 뒤 파싱은 회복하지 못한다. 공고는 **공개 정보**라 자소서와 달리 외부 전송 제약도 없다.

---

## 2. 강점·약점 코멘트

`POST /v1/comments` — F3-3 · 이슈 #14

**점수는 BE 가 낸다.** 이 서비스는 BE 가 계산에 쓴 근거를 받아 두 문장으로 옮긴다.

### 2.1 요청

```json
{
  "matchedKeywords": ["Spring", "Kotlin"],
  "missingQualifications": ["RDB 1년 이상"],
  "matchedPreferences": ["Java/Kotlin 백엔드 경험"],
  "topExperienceTitle": "CareerCompass"
}
```

`topExperienceTitle` 은 없으면 빈 문자열로 온다.

### 2.2 응답

```json
{
  "strength": "Spring·Kotlin 백엔드 프로젝트 경험이 이 공고의 우대 조건과 직접 맞습니다.",
  "weakness": "RDB 1년 이상 경력을 확인할 수 있는 경험이 프로필에 없습니다.",
  "usage": { }
}
```

- **받은 근거 안에서만 쓴다.** 여기 없는 사실이 문장에 등장하면 점수와 설명이 따로 논다(#14). 입력에 없는 고유명사·수치가 나오면 다시 만든다.
- **1~3줄.** 명세서 F3-3 의 상한이다.
- 약점은 **행동으로 이어지게** 쓴다 — 프로필에 넣으면 되는 것인지, 아예 자격이 안 되는 것인지.
- 근거가 비어 있으면(`matchedKeywords` 도 `matchedPreferences` 도 빈 배열) **빈말을 만들지 않는다.** `null` 을 낸다 — BE 는 코멘트 실패 시 `null` 을 허용하고 점수는 정상으로 내보낸다(스펙 v0.2 §5).

---

## 3. 지원서 초안

`POST /v1/draft-answer` — F4-2 · F4-3 · 이슈 #16 #18 #19

**항목 하나에 호출 하나.** BE 가 항목 수만큼 병렬로 부르고, SSE 로 FE 에 흘리는 것도 BE 가 한다. 재생성도 같은 엔드포인트다.

### 3.1 요청

```json
{
  "question": "지원 동기를 작성해 주세요",
  "maxChars": 500,
  "tone": "formal",
  "postingTitle": "2026 카카오 SW 인턴십",
  "keywords": ["Spring", "Kotlin", "Redis"],
  "experienceSummaries": ["CareerCompass — 백엔드, Spring·Kotlin, 공고 분석 서비스", "..."]
}
```

- **`maxChars` 가 `0` 이면 제한 없음이다.** BE 가 `null` 을 `0` 으로 바꿔 보낸다. 그때는 400~600자로 만든다.
- `tone` — `formal` / `casual` / **`confident`**. BE `ApplicationService.TONES` 가 셋이다(FE 는 둘만 보낸다). 모르는 값은 `formal` 로 읽는다. **문체만 달라지고 사실은 같아야 한다**(#18).
- `experienceSummaries` — **매칭도 순으로 정렬되어 온다.** 우선순위 규칙은 BE 가 적용했다. 이쪽은 앞에서부터 쓴다. 각 항목은 BE `DraftContextBuilder.cardText` 가 만든 「제목 summary role company …」 한 줄이다.
- **마지막 항목이 `과거 자소서 발췌: …`(120자)일 수 있다** — BE `pastExcerpt` 가 질문 카테고리와 같은 과거 자소서 항목을 붙인다. 경험이 아니라 **문체 참고**다. 이쪽은 이 접두어를 보고 분리해서 사실 근거·안전 초안에서 뺀다. `usedIndexes` 는 원래 배열 인덱스 그대로다.
- **이름·연락처는 이미 마스킹되어 온다**(BE `PrivacyMasker`). 이쪽에서 다시 지우지 않는다.

### 3.2 응답

```json
{
  "answer": "...",
  "charCount": 487,
  "usedIndexes": [0, 2],
  "factCheck": { "passed": true, "unverified": [] },
  "usage": { }
}
```

BE 는 `answer` 만 읽는다. 나머지는 이쪽의 품질 추적용이고 BE 가 무시해도 무해하다.

- **`charCount` 는 `maxChars` 를 넘지 않는다.** 프롬프트로 부탁하는 것이 아니라 생성 후 실측해서, 초과하면 문장 단위로 줄이거나 다시 만든다(#16).
- `usedIndexes` — `experienceSummaries` 의 **몇 번째를 인용했는지.** BE 는 id 를 보내지 않으므로 인덱스로 답한다. BE 가 원하면 자기 배열로 되돌릴 수 있다.
- `factCheck.unverified` — 생성물에 나왔는데 입력(`experienceSummaries` · `postingTitle` · `question`)에서 확인되지 않은 수치·영문 토큰(#29). **LLM 이 아니라 문자열 대조 규칙으로 판정한다.** `keywords` 는 근거가 아니다 — 공고의 요구 기술을 「해 본 경험」으로 쓰면 날조다.
- **검증에 걸린 답은 나가지 않는다.** ① 1회 재요청 → ② 걸린 문장을 규칙으로 제거 → ③ 그래도 남으면 모델 출력을 버리고 **입력 문자열만으로 만든 안전 초안**을 낸다. 그때 `factCheck.fallback: true`. 모델이 형식을 두 번 어기거나 빈 답을 내도 같은 안전 초안이다 — 503 이 아니다. BE 는 `fallback` 을 무시해도 되지만, FE 가 「AI 가 쓴 초안이 아니라 출발점」이라고 표시하고 싶으면 이 값을 쓴다. **`experienceSummaries` 가 비었거나 「과거 자소서 발췌:」뿐이면 모델을 부르지 않고** 같은 안전 초안을 낸다 — 근거가 0 이면 검증할 것도 0 이라 모델은 지어낸다(09-16 실측). 그때도 `fallback: true`, `usage.totalTokens: 0`.
- `tone` — 문체는 문장 어미 비율로 실측해 80% 미만이면 1회 재요청한다(#18). 그래도 미달이면 첫 답을 낸다 — 문체는 사실이 아니라 503 도 fallback 도 아니다.
- 출력에 이메일·전화·주민번호 모양이 있으면 `[삭제]` 로 지운다.
- `answer` 가 비면 안 된다. BE 는 빈 문자열을 `LLM_UNAVAILABLE` 로 본다. 위 안전 초안 덕에 비는 경우가 없다.

---

## 4. 헬스체크

`GET /health` → `{ "status": "up", "version": "0.2.0", "stubMode": true }`

프로바이더를 실제로 호출하지는 않는다(비용). 키 존재와 프로세스 상태만 본다.

---

## 5. 아직 안 정한 것

| 항목 | 이슈 | 막고 있는 것 |
| --- | --- | --- |
| 프로바이더 확정 — Bedrock 인리전 / HyperCLOVA X | #5 #28 | 비용 추정치가 실측이 아니다 |
| 이미지 공고 멀티모달 (§1.4) | #33 | BE 가 `images` 를 보내야 한다 |
| 첨부 HWP·PDF | #34 | 「붙임 파일 참조」 공고 |
| `NOT_A_POSTING` 제외 경로 (§1.3) | — | BE 에 목록 제외 수단이 없다 |

---

## 변경 이력

- **v0.2** — BE #51 의 `HttpLlmGateway` 에 맞춰 전면 개정. 봉투 제거, 인증 선택, 파싱 실패를 `200` + 플래그로, 엔드포인트를 셋으로 축소(`/v1/comments` · `/v1/draft-answer`), 자격 조건 평평하게, `meta` → `usage`. 적합도 산출·문서 분류·텍스트 추출·임베딩·유사 공고·추천은 BE 가 구현했으므로 이쪽에서 뺀다.
- v0.1 (폐기) — BE 코드가 없던 시점의 초안. 봉투·422 실패·`/v1/score`·`/v1/generate-item`·`/v1/classify-document`·`/v1/extract-text`·`/v1/embed`.
