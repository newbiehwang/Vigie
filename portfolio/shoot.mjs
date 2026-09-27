// 포트폴리오에 넣는 화면 캡처 8장을 다시 찍는다 (shots/). 웹 화면이 바뀌면 이것부터 다시 돌린다.
// 먼저 mock 모드 개발 서버를 5174에 띄운다 (.claude/launch.json의 vigie-frontend-mock-5174와 같다):
//   npm --prefix frontend run dev:mock -- --port 5174 --strictPort
// 그다음: node portfolio/shoot.mjs
// - 쪽 안에서 글자가 읽히게 대화 화면은 좁은 창으로 찍고, 대화 칸만 잘라 낸다
// - 승인 카드·일반 사용자 답이 잘리지 않게 창 높이를 장면마다 다르게 둔다
// - 모두 2배 해상도 (cdp.mjs의 viewport 기본값)
import { launch } from './cdp.mjs';

const B = 'http://localhost:5174';
const OUT = new URL('./shots/', import.meta.url).pathname;
const c = await launch(9336);

// 질문을 보내고, 보내기 버튼이 돌아온 뒤(답이 끝난 뒤) 조금 더 기다린다
const ASK = `window.__ask = async (q) => {
  const ta = document.querySelector('textarea.composer-input');
  Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set.call(ta, q);
  ta.dispatchEvent(new Event('input', { bubbles: true }));
  await new Promise(r => setTimeout(r, 150));
  document.querySelector('button.composer-send').click();
  await new Promise(r => setTimeout(r, 1500));
  const t0 = Date.now();
  while (Date.now() - t0 < 45000) { if (document.querySelector('button.composer-send')?.getAttribute('aria-label') === '보내기') break; await new Promise(r => setTimeout(r, 400)); }
  await new Promise(r => setTimeout(r, 5000));
  return 'ok';
}; 'ok'`;
// 대화 칸(입력칸이 든 패널)만 잘라 찍는다
const panelShot = async (name) => {
    const r = await c.eval(`(() => { const p = document.querySelector('textarea.composer-input').closest('section, .plan-panel, main > div'); const b = (p || document.body).getBoundingClientRect(); return { x: b.left, y: b.top, width: b.width, height: b.height }; })()`);
    await c.shot(OUT + name, r);
};
const newChat = `[...document.querySelectorAll('button')].find(b => b.textContent.trim() === '+ 새 대화').click(); 'ok'`;

// 로그인 전 안내 화면 (새 브라우저라 로그인 기록이 없을 때 먼저 찍는다)
await c.viewport(1440, 900);
await c.goto(`${B}/`, 3000);
await c.shot(OUT + 'landing.png');

// 대화: 예시 대화(보안 이벤트) · 승인 카드 · 도구 실패가 보이는 사고 과정
await c.viewport(1000, 860);
await c.goto(`${B}/chat?mock-auth=signed-in`, 3500);
await panelShot('n-chat-security.png');
await c.eval(ASK);
await c.eval(newChat);
await c.sleep(800);
await c.eval(`window.__ask('frothlywebcode 퍼블릭 액세스 차단 켜 줘')`);
await panelShot('n-chat-approval.png');
await c.eval(newChat);
await c.sleep(800);
await c.eval(`window.__ask('Lambda 오류 원인은?')`);
await c.eval(`[...document.querySelectorAll('button')].filter(b => /사고 과정/.test(b.textContent)).pop()?.click(); 'ok'`);
await c.sleep(800);
await c.viewport(1000, 1000);
await c.sleep(600);
await panelShot('n-chat-toolfail.png');

// 일반 사용자: 관리자 전용 도구 없이 답한다
await c.viewport(900, 640);
await c.goto(`${B}/chat?mock-auth=signed-in&mock-role=member`, 3500);
await c.eval(ASK);
await c.eval(newChat);
await c.sleep(800);
await c.eval(`window.__ask('최근 보안 이벤트를 심각도 순으로 정리해줘')`);
await panelShot('n-chat-member.png');

// 홈 (쪽 안에 넣는 좁은 것과 표지용 넓은 것)
await c.viewport(1200, 780);
await c.goto(`${B}/?mock-auth=signed-in`, 4500);
await c.shot(OUT + 'n-home.png');
await c.viewport(1440, 900);
await c.goto(`${B}/?mock-auth=signed-in`, 4500);
await c.shot(OUT + 'home.png');

// 감사 로그: 의심 뒤 요청 기록의 7계층 판정
await c.goto(`${B}/audit?mock-auth=signed-in&q=%22%EC%9D%98%EC%8B%AC%20%EB%92%A4%20%EC%9A%94%EC%B2%AD%22`, 4000);
await c.eval(`document.querySelector('.audit-row-button')?.click(); 'ok'`);
await c.sleep(3000);
await c.shot(OUT + 'audit-trace.png');

await c.close();
console.log('done');
