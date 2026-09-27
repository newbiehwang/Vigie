# Vigie 로고

| 파일 | 용도 |
|---|---|
| `vigie-logo.svg` | 밝은 바탕용 (#0A1628, Midnight Ink의 글자색) |
| `vigie-logo-white.svg` | 어두운 바탕용 (#FFFFFF) |
| `vigie-mark.svg` | 첫 글자 V만 떼어 낸 정사각형 마크 (대화 아바타). `vigie-logo.svg`의 윤곽선 11개 중 앞의 두 개(V 본체·안쪽 고리) |

브라우저 탭 아이콘은 같은 V를 바탕 없이 쓴 `frontend/public/vigie-icon.svg`다 (밝은 탭에는 남색, 다크 모드에서는 흰색).

글자는 [Parisienne](https://fonts.google.com/specimen/Parisienne)(Astigmatic, SIL Open Font License 1.1)로 쓴 "Vigie"를 윤곽선으로 바꾼 것이다. 폰트를 불러오지 않아도 어디서나 같게 보인다.

저장소에서도 쓴다: 루트 `README.md` 맨 위 로고(GitHub 다크 모드에서는 흰색), 링크를 공유할 때 보이는 미리보기 이미지 `images/social-preview.png`(1280×640, 워드마크 + "AWS 클라우드 운영 AI 에이전트"). 미리보기 이미지는 GitHub 저장소 Settings → General → Social preview에서 올린다.

`login-background.svg`: 로그인 배경(왼쪽 위 파랑·오른쪽 아래 남색 번짐). 앱의 로그인 전환 화면(`LoginSplash.tsx`)과 Cognito 로그인 페이지 배경(`base.yaml`의 `PAGE_BACKGROUND`)이 같이 써서, 앱 → Cognito → 앱으로 오가는 동안 바탕이 이어져 보인다. Cognito가 받는 SVG 요소(그라디언트·rect)만 쓴다.
