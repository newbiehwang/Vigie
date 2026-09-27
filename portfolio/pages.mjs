// 쪽마다 PNG로 찍는다 (pages/p01.png …). 쪽 모양을 한눈에 훑어볼 때 쓴다
import { launch } from './cdp.mjs';
import { mkdirSync, writeFileSync } from 'node:fs';
const here = new URL('./', import.meta.url).pathname;
mkdirSync(here + 'pages', { recursive: true });
const c = await launch(9335);
await c.viewport(1400, 900, 1);
await c.goto('file://' + here + 'index.html', 4000);
await c.eval(`document.fonts.ready.then(() => 'ok')`);
const rects = await c.eval(`[...document.querySelectorAll('.page')].map(p => { const r = p.getBoundingClientRect(); return { x: r.left + scrollX, y: r.top + scrollY, width: r.width, height: r.height }; })`);
for (const [i, r] of rects.entries()) {
  const res = await c.send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: true, clip: { ...r, scale: 1 } });
  writeFileSync(`${here}pages/p${String(i + 1).padStart(2, '0')}.png`, Buffer.from(res.data, 'base64'));
}
await c.close();
console.log(rects.length);
