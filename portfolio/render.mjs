// 포트폴리오를 PDF로 인쇄한다 (Vigie-portfolio.pdf, 1280×720 쪽 17장). 쪽 밖으로 넘친 요소가 있으면 쪽 번호를 알린다
import { launch } from './cdp.mjs';
const here = new URL('./', import.meta.url).pathname;
const c = await launch(9334);
await c.goto('file://' + here + 'index.html', 4000);
await c.eval(`document.fonts.ready.then(() => document.fonts.check('16px "Pretendard Variable"'))`).then((v) => console.log('pretendard', v));
// 넘치는 쪽 찾기: 쪽 안 요소가 쪽 아래(발 위)로 나가면 알린다
const over = await c.eval(`[...document.querySelectorAll('.page')].map((p, i) => { const pb = p.getBoundingClientRect(); const worst = Math.max(...[...p.querySelectorAll('*')].filter(e => !e.closest('.foot')).map(e => e.getBoundingClientRect().bottom - pb.top)); return [i + 1, Math.round(worst)]; }).filter(([, b]) => b > 690)`);
console.log('overflow', JSON.stringify(over));
await c.pdf(here + 'Vigie-portfolio.pdf');
await c.close();
