# CareerCompass-AI

건국대학교 졸업 프로젝트 **CareerCompass** 의 AI/NLP 서비스.

공고 본문을 구조화하고, 사용자 프로필과 대조해 적합도를 산출하고, 경험 카드를 근거로 자기소개서 초안을 만드는 부분이 여기 있다.

담당은 **서성덕 (@kislevite)** — 2026-05-18 제안서 「팀원 정보」의 역할이 AI/NLP 이고, 발표자료 v1 슬라이드 12 는 `LLM 연동 · KoBERT 파인튜닝` 으로 적고 있다.

> 2026-03-06 제안서(`… – CamBridge`)는 **폐기된 주제의 문서**다. 거기 적힌 역할 분담은 현행이 아니다.

## 현재 상태

**골격까지 섰다.** 계약([`docs/AI_CONTRACT_v0.1.md`](docs/AI_CONTRACT_v0.1.md))대로의 엔드포인트 6개가 **더미 응답**을 낸다. 모델은 아직 부르지 않는다.

해야 할 일은 이슈로 등록해 뒀다. 총 **31건**, 마일스톤 7개(A0~A6).

### 띄우기

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"   # Windows: .venv\Scripts\pip
uvicorn app.main:app --reload
curl localhost:8000/health
```

또는 `docker compose up --build`.

호출에는 `X-Internal-Token` 헤더가 필요하다(`/health` 는 예외). 기본값은 `.env.example` 참고.

```bash
curl -X POST localhost:8000/v1/parse-posting \
  -H 'Content-Type: application/json' -H 'X-Internal-Token: dev-token' \
  -d '{"postingId":101,"title":"테스트","rawContent":"..."}'
```

### 더미 응답을 고르는 법

고정 응답만 내려주면 BE 가 실패 화면을 짤 수 없다. `postingId` 로 시나리오를 고른다.

| postingId | 결과 |
| --- | --- |
| 999001 | `status: partial` — 마감일·양식 없음 |
| 999002 | `PARSING_FAILED` / `NO_KEYWORDS` |
| 999003 | `PARSING_FAILED` / `IMAGE_ONLY` |
| 999004 | `PARSING_FAILED` / `NOT_A_POSTING` |
| 999005 | `PARSING_FAILED` / `EMPTY` |
| 그 밖 | `status: ok` |

타임아웃 처리를 시험하려면 `?delay=15000` (밀리초).

## 이 서비스가 맡는 것

| 기능 | 명세 | 이슈 |
| --- | --- | --- |
| 공고 구조화 파싱 — 키워드·자격·우대·마감일·지원서 양식 | F3-1 · F4-1 | #7 ~ #10 |
| 적합도 4축 산출과 강점·약점 코멘트 | F3-2 · F3-3 | #11 ~ #15 |
| 지원서 초안 생성·재생성·톤 | F4-2 · F4-3 | #16 ~ #20 |
| 과거 지원서 텍스트 추출과 항목 분류 | F1-4 | #21 ~ #23 |
| For You 추천 · 커리어 로드맵 · 강점 요약 | API_SPEC §7 | #24 ~ #26 |

## 기술 선택이 아직 열려 있는 것

**#22 — 과거 지원서 항목 분류를 LLM API 로 할지 KoBERT 파인튜닝으로 할지.** 명세서 F1-4 가 두 갈래를 남겨 두었고(「llm api 혹은 오픈소스 BERT 모델 파인튜닝하여 사용, llm api 사용시 개인정보 문제 고려 필요」), 아직 정해지지 않았다. 학습 데이터를 구할 수 있는지가 실질적인 갈림길이다.

**#1 — 서비스 형태.** 파이썬 서비스로 띄우고 BE 가 HTTP 로 부르는 구조를 기본안으로 잡았다. KoBERT 파인튜닝이 들어오면 Java/Spring 안에서는 감당이 안 되기 때문이다.

## 다른 저장소와의 관계

| 저장소 | 담당 | 상태 |
| --- | --- | --- |
| [CareerCompass-FE](https://github.com/Team-CareerCompass/CareerCompass-FE) | 정일혁 (@1hyok) · 이준혁 (@Sadturtleman) | Android. §1~§5 구현됨 |
| [CareerCompass-BE](https://github.com/Team-CareerCompass/CareerCompass-BE) | 조영탁 (@Tak002) | 서버. 착수 전, 이슈 50건 |

**BE 가 이 서비스를 호출한다.** 저장·인증·API 노출은 BE 가 하고, 여기는 모델과 프롬프트만 다룬다. 경계는 #1 에서 확정한다.

`blocks:BE` · `blocks:FE` 라벨은 **다른 사람이 그것 때문에 못 만드는 것**이다.

## 먼저 할 것

**A0 없이는 아무것도 시작할 수 없다.** 그중에서도 #1(골격·경계)과 #6(LLM 전송 범위·마스킹)이 먼저다 — 무엇을 외부로 보내도 되는지 정하지 않고 만들면 나중에 전부 뜯는다.

그다음은 **#7 공고 파싱**이다. 파싱 결과가 적합도와 지원서 생성의 입력이라, 여기가 서면 나머지가 병렬로 갈라진다.

## 규약

- 스펙과 다르게 구현하기로 정했으면 `docs/API_SPEC_v0.1.md`(BE 저장소)를 고치고 해당 저장소에 이슈로 알린다. 조용히 다르게 만들지 않는다.
- 프롬프트를 고치면 평가셋을 돌린다(#5). 눈으로 두세 개 보고 넘어가지 않는다.
