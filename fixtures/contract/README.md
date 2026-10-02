# 계약 표본 — 양쪽이 같은 JSON 을 본다 (#30)

계약(`docs/AI_CONTRACT_v0.2.md`)을 **문서가 아니라 파일로** 고정한 것이다. 한쪽이 모양을 바꾸면 양쪽 CI 가 깨지는 것이 목적이다.

지금은 이쪽만 쓴다 — `tests/test_contract.py` 가 이 디렉터리를 읽어 표본마다 요청을 보내고 `expect` 를 확인한다. **BE PR #51 이 머지되면 같은 파일을 BE 의 `HttpLlmGatewayTest` 에 붙인다.** 그래서 Java 에서도 읽기 쉬운 평평한 모양으로 뒀다.

## 모양

```json
{
  "name": "parse-fail-empty",
  "note": "왜 이 표본이 있는가 — 깨졌을 때 읽을 사람을 위해",
  "endpoint": "POST /v1/parse-posting",
  "headers": { "X-Prompt-Version": "v1" },
  "request": { "title": "[stub:EMPTY] 공고", "rawContent": "..." },
  "expect": {
    "status": 200,
    "equals":   { "parsingFailed": true, "reasonCode": "EMPTY" },
    "required": ["parsingFailed", "reason", "reasonCode", "usage"],
    "absent":   ["ok", "data", "error"],
    "types":    { "keywords": "list" },
    "notes":    ["사람이 읽는 설명 — 자동 검증 대상이 아니다"]
  }
}
```

| 키 | 뜻 |
| --- | --- |
| `equals` | 값이 정확히 같아야 한다 |
| `required` | 최상위에 **있어야** 하는 키 |
| `absent` | **없어야** 하는 키 — 봉투(`ok`·`data`)와 중첩 자격(`qualifications`)이 여기 걸린다 |
| `types` | `list` · `object` · `string` · `number` · `boolean` |
| `notes` | 검증하지 않는 설명. 숫자로 못 박기 어려운 규약을 남긴다 |

`request` 가 `null` 인 표본은 **HTTP 로 재현하지 않는다** — 프로바이더 장애를 일부러 만들 수 없어서, 본문 모양만 고정하고 `contract.error_body` 로 확인한다.

## 지금 있는 것 — 14건

| 표본 | 무엇을 못 박는가 |
| --- | --- |
| `parse-success` | 봉투 없음 · 자격 평평 · `usage.totalTokens` |
| `parse-success-partial` | 마감일 `null`·양식 `[]` 이 **정상**인 경우 (#8 #10) |
| `parse-fail-{empty,image-only,not-a-posting,no-keywords}` | 파싱 실패는 **200 + 플래그**. 4xx 로 내면 BE 가 서버 장애로 둔갑시킨다 |
| `parse-truncated` | 40,000자 상한과 `truncated` |
| `parse-invalid-request` | 계약과 다른 요청은 **400** — BE 는 재시도하지 않는다 |
| `comments-success` | 두 문장, 받은 근거 안에서만 |
| `comments-null-without-grounds` | 근거가 비면 **`null`**, 모델 호출 0 |
| `draft-success` | `charCount == len(answer)` · `usedIndexes` · `factCheck` |
| `draft-unlimited` | **`maxChars: 0` = 제한 없음** (BE 가 null→0) |
| `draft-tone-confident` | **tone 은 셋** · 「과거 자소서 발췌:」는 경험이 아니다 |
| `error-llm-unavailable` | 503 `{error, code}` · 429 는 `RATE_LIMITED` |

## 바꿀 때

**조용히 바꾸지 않는다** (#30). 이 디렉터리를 고치면 계약 문서와 BE·FE 저장소 이슈를 같이 고친다 — 지금까지 BE #64·#65·#66·#67 이 그 기록이다.
