# Vigie 포트폴리오

Vigie를 소개하는 21쪽짜리 PDF의 원본입니다. 쪽 하나가 1280×720인 HTML 한 장(`index.html`)이고, 헤드리스 Chrome으로 인쇄해 PDF를 만듭니다.

| 파일 | 내용 |
|:--|:--|
| `index.html` | 포트폴리오 본문과 스타일. 색은 웹과 같은 Midnight Ink(`frontend/src/styles.css`), 글꼴은 Pretendard(jsDelivr에서 불러옴), 로고는 `frontend/src/assets/brand/vigie-logo.svg` |
| `shots/` | 쪽에 넣는 화면 캡처. `n-*` · `home` · `landing` · `diag-alb`는 mock 모드 앱, `local-*` · `inj-*`는 로컬 장애 재현(`local/`)을 찍은 것 |
| `shoot.mjs` | `shots/`를 다시 찍는다 |
| `render.mjs` | PDF로 인쇄한다 (`Vigie-portfolio.pdf`, 저장소에는 올리지 않음) |
| `pages.mjs` | 쪽마다 PNG로 찍는다 (`pages/`, 저장소에는 올리지 않음) |
| `cdp.mjs` | 위 스크립트들이 쓰는 헤드리스 Chrome 조종 도구 (DevTools 프로토콜) |

## 다시 만들기

Node 22 이상과 Google Chrome이 필요합니다. `cdp.mjs`는 macOS의 Chrome 경로를 씁니다. 다른 운영체제에서는 `CHROME` 값을 바꿔 주세요.

화면 캡처는 웹 화면이 바뀌었을 때만 다시 찍습니다. 먼저 mock 모드 개발 서버를 5174 포트에 띄웁니다.

```bash
npm --prefix frontend run dev:mock -- --port 5174 --strictPort
```

다른 터미널에서 캡처를 찍습니다. 다 찍으면 개발 서버를 끕니다.

```bash
node portfolio/shoot.mjs
```

PDF로 인쇄합니다. 쪽 밖으로 넘친 요소가 있으면 쪽 번호가 나옵니다(`overflow`). 1쪽(표지)은 오른쪽 화면이 쪽 끝까지 닿도록 둔 것이라 괜찮습니다.

```bash
node portfolio/render.mjs
```

## 쓸 때 지킬 것

- 관리형 서비스가 나오기 전에 직접 만든 토이 프로젝트로 소개합니다. 기능이 독창적이라고 주장하지 않습니다(20쪽 "이제는 관리형 서비스가 있지만…").
- 강조색은 주 색(`--accent`, #1E5AA8) 하나만 씁니다.
