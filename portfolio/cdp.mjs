// 헤드리스 Chrome을 DevTools 프로토콜로 조종하는 작은 도구 (캡처와 PDF 인쇄에 쓴다)
import { spawn } from 'node:child_process';
import { writeFileSync, mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const CHROME = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

export async function launch(port = 9333) {
    const dir = mkdtempSync(join(tmpdir(), 'vigie-cdp-'));
    const proc = spawn(CHROME, ['--headless=new', `--remote-debugging-port=${port}`, `--user-data-dir=${dir}`, '--hide-scrollbars', '--no-first-run', '--no-default-browser-check', 'about:blank'], { stdio: 'ignore' });
    let targets;
    for (let i = 0; i < 50; i++) {
        try { targets = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json(); if (targets.length) break; } catch {}
        await sleep(200);
    }
    const page = targets.find((t) => t.type === 'page');
    const ws = new WebSocket(page.webSocketDebuggerUrl);
    await new Promise((r) => ws.addEventListener('open', r));
    let id = 0;
    const pending = new Map();
    ws.addEventListener('message', (e) => {
        const msg = JSON.parse(e.data);
        if (msg.id && pending.has(msg.id)) { const { resolve, reject } = pending.get(msg.id); pending.delete(msg.id); msg.error ? reject(new Error(msg.error.message)) : resolve(msg.result); }
    });
    const send = (method, params = {}) => new Promise((resolve, reject) => { const n = ++id; pending.set(n, { resolve, reject }); ws.send(JSON.stringify({ id: n, method, params })); });
    await send('Page.enable');
    await send('Runtime.enable');
    const api = {
        send,
        async viewport(width, height, scale = 2) { await send('Emulation.setDeviceMetricsOverride', { width, height, deviceScaleFactor: scale, mobile: false }); },
        async goto(url, wait = 2500) { await send('Page.navigate', { url }); await sleep(wait); },
        async eval(expression) { const r = await send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true }); if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description ?? 'eval failed'); return r.result.value; },
        async shot(path, clip) { const r = await send('Page.captureScreenshot', { format: 'png', ...(clip && { clip: { ...clip, scale: 1 } }) }); writeFileSync(path, Buffer.from(r.data, 'base64')); },
        async pdf(path, options) { const r = await send('Page.printToPDF', { printBackground: true, preferCSSPageSize: true, ...options }); writeFileSync(path, Buffer.from(r.data, 'base64')); },
        async close() { try { await send('Browser.close'); } catch {} proc.kill(); },
        sleep,
    };
    return api;
}
