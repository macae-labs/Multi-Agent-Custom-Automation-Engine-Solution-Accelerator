// Sonda E2E en Chrome real: valida el ÁRBOL (backend local :8000) contra el REPO del share de prod,
// con la identidad real de prod inyectada en /.auth/me (la UI la parsea como EasyAuth) para que
// cabeceras, WebSocket y el user_id que viaja a ca-mcp sean la misma identidad.
// Requisitos: UN solo backend vivo (:8000), Vite :3001, ~/.macae/workspaces/<OID>/<workspace> presente
// (chequeo local de "montado"), y en el share /data/workspaces/<OID>/<workspace> con el repo.
// Uso (desde la raíz del repo):
//   node docs/incidents/probes/chrome_plan_lane_identity_probe.mjs --oid <oid-de-prod> --lane Plan \
//     --shots /tmp/plan_probe --chrome /home/vscode/.cache/ms-playwright/chromium-1223/chrome-linux64/chrome
// Evidencia en $SHOTS: net.jsonl (POSTs y respuestas), ws_frames.jsonl (frames del socket), sse.txt,
// page_final.txt, page_after_refresh.txt y capturas 01..07. Escribe planes/sesiones bajo OID (write-shared).
// identidad real inyectada en /.auth/me (objectidentifier = oid). Sin relojes en las esperas de estado.
import { chromium } from '/home/vscode/.npm/_npx/e41f203b7505f1fb/node_modules/playwright/index.mjs';
import { appendFileSync, writeFileSync } from 'node:fs';
// argumentos: --oid <oid> --lane Plan|Chat --shots <dir> --chrome <binario>  (o las variables OID/LANE/SHOTS/CHROME)
const argv = process.argv.slice(2); const arg = (k) => { const i = argv.indexOf('--' + k); return i >= 0 ? argv[i + 1] : undefined; };
const S = arg('shots') || process.env.SHOTS, BASE = 'http://127.0.0.1:3001', OID = arg('oid') || process.env.OID;
const LANE_ARG = arg('lane') || process.env.LANE || 'Chat', CHROME_BIN = arg('chrome') || process.env.CHROME || undefined;
if (!S || !OID) { console.error('faltan --shots y --oid'); process.exit(2); }
const WS_NAME = 'multi-agent-custom-automation-engine-solution-accelerator';
const MSG = `Validación integral y NO DESTRUCTIVA del proyecto montado en el workspace. Objetivos: (1) Confirmar la rama actual y el último commit usando comandos git de solo lectura (git status, git branch --show-current, git log -1). Si aparece el error de "dubious ownership", documentarlo y usar la ruta segura de lectura disponible sin modificar configuración persistente. (2) Leer e interpretar src/backend/pyproject.toml para identificar dependencias, herramientas y comandos de validación disponibles (lint, type-check, tests) — y localizar el archivo si no está en esa ruta exacta. (3) Leer e interpretar src/frontend/package.json para identificar scripts de validación disponibles (lint, type-check, build, test). (4) Ejecutar ÚNICAMENTE las validaciones no destructivas que existan (linters, type-checkers, colección/lectura de tests con --collect-only, comprobaciones de formato en modo --check, validación de esquemas), sin modificar archivos, sin instalar/actualizar dependencias de forma persistente, sin migraciones ni builds destructivos. (5) Consolidar hallazgos en un reporte claro con el estado de cada validación (rama/commit, backend, frontend), evidencia (comandos ejecutados y salidas), advertencias, errores y recomendaciones. Si algo es ambiguo o falta un archivo/herramienta, preguntar antes de continuar.`;
const t0 = Date.now(); const log = (m) => console.log(`[${((Date.now()-t0)/1000).toFixed(1)}s] ${m}`);
const shot = async (page, name) => { await page.screenshot({ path: `${S}/${name}.png`, fullPage: true }); log(`shot ${name}`); };
const jl = (f, o) => appendFileSync(`${S}/${f}`, JSON.stringify(o) + '\n');

const browser = await chromium.launch({ headless: true, executablePath: CHROME_BIN });
const ctx = await browser.newContext({ viewport: { width: 1500, height: 1000 } });
const page = await ctx.newPage();

// identidad real: la UI parsea /.auth/me (EasyAuth). En localhost no existe; se sirve aquí, con el oid en el claim objectidentifier.
await page.route('**/.auth/me', (route) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify([{
  access_token: '', expires_on: '', id_token: '', provider_name: 'aad', user_id: 'winston@local',
  user_claims: [ { typ: 'http://schemas.microsoft.com/identity/claims/objectidentifier', val: OID }, { typ: 'name', val: 'Winston' } ],
}]) }));

// evidencia de red: identidad en cabeceras, y cada POST del carril
let firstHeaderLogged = false;
page.on('request', (r) => {
  const u = r.url(); if (!u.includes('/api/')) return;
  const h = r.headers()['x-ms-client-principal-id'];
  if (!firstHeaderLogged && h) { firstHeaderLogged = true; log(`cabecera x-ms-client-principal-id=${h}`); }
  if (r.method() === 'POST' && /process_request|resume_plan|plan_approval|user_clarification|chat\/message/.test(u)) {
    log(`POST ${new URL(u).pathname}`); jl('net.jsonl', { t: Date.now()-t0, m: 'POST', path: new URL(u).pathname, principal: h, body: (r.postData()||'').slice(0, 400) });
  }
});
page.on('response', async (resp) => {
  const u = resp.url(); if (!u.includes('/api/')) return;
  const p = new URL(u).pathname;
  if (/process_request|resume_plan|plan_approval|user_clarification/.test(p)) { const body = await resp.text().catch(()=> ''); log(`  ← ${resp.status()} ${p} ${body.slice(0,160)}`); jl('net.jsonl', { t: Date.now()-t0, m: 'RESP', path: p, status: resp.status(), body: body.slice(0, 800) }); }
  if (p.endsWith('/chat/message/stream')) { resp.text().then((b) => { writeFileSync(`${S}/sse.txt`, b); const sess = (b.match(/session_id[^a-f0-9]*([a-f0-9-]{36})/)||[])[1]; log(`SSE terminado (${b.length} bytes) session=${sess||'?'}`); }).catch(()=>{}); }
});
page.on('websocket', (ws) => {
  log(`WS abierto ${new URL(ws.url()).pathname}${new URL(ws.url()).search}`);
  ws.on('framereceived', (f) => { try { const o = JSON.parse(f.payload); const ty = o.type || o.event || '?'; jl('ws_frames.jsonl', { t: Date.now()-t0, type: ty, data: o }); if (!/^(agent_message_streaming|ping|pong)$/i.test(String(ty))) log(`WS ← ${ty}${o.data && o.data.plan_id ? ' plan='+String(o.data.plan_id).slice(0,8) : ''}${o.data && o.data.agent ? ' agent='+o.data.agent : ''}`); } catch { /* no JSON */ } });
});
page.on('console', (m) => { if (/error/i.test(m.type()) ) log(`console.error: ${m.text().slice(0, 160)}`); });

await page.goto(BASE + '/', { waitUntil: 'domcontentloaded' });
await page.evaluate((ws) => localStorage.setItem('macae_active_workspace_id', ws), WS_NAME);
await page.reload({ waitUntil: 'domcontentloaded' });
await page.getByRole('textbox').first().waitFor({ timeout: 0 });
const uid = await page.evaluate(() => (window.userInfo && window.userInfo.user_id) || null);
log(`identidad en la UI: window.userInfo.user_id=${uid}`);
if (uid !== OID) { log('IDENTIDAD NO INYECTADA: abortando antes de escribir nada'); await browser.close(); process.exit(2); }
await shot(page, '01-home');

const lane = async () => (await page.getByText(/^(Chat|Plan)$/).first().textContent().catch(() => '')) || '';
log(`carril inicial: ${await lane()}`);
const WANT = LANE_ARG; if ((await lane()).trim() !== WANT) { await page.getByRole('switch').first().click(); log(`carril tras click: ${await lane()}`); }
const input = page.getByPlaceholder(/Describe your task|Describe the objective|Type your message/i).first();
await input.fill(MSG); await input.press('Enter'); log(`mensaje enviado por la UI (carril ${WANT})`);
await page.waitForURL(/planId=|\/plan\//, { timeout: 0 });
log(`url=${page.url()}`); await shot(page, '02-plan-created');

const approveBtn = () => page.getByRole('button', { name: /Approve Task Plan|Aprobar/i });
await approveBtn().first().waitFor({ timeout: 0 });
await shot(page, '03-plan-review');
log(`tarjeta: ${String(await page.getByText(/Proposed Plan for/).first().textContent().catch(() => '')).slice(0, 100)}`);
await approveBtn().first().click(); log('plan aprobado desde la UI');

let clar = 0, replans = 0;
for (;;) {
  const tb = page.getByPlaceholder(/Type your message|Describe your task|Describe the objective/i).first();
  const enabled = (await tb.count()) ? await tb.isEnabled() : false;
  const finalCard = await page.getByText(/Group Chat Manager|Group_Chat_Manager|Final result|Resultado final/i).count();
  const reviewAgain = await approveBtn().count();
  if (reviewAgain) { replans += 1; log(`plan_review de nuevo (#${replans}): apruebo`); await shot(page, `04-replan-${replans}`); await approveBtn().first().click(); await page.waitForTimeout(5000); }
  else if (enabled && !finalCard) { clar += 1; log(`clarificación #${clar}: textarea habilitado`); await shot(page, `05-clarification-${clar}`);
    await tb.fill('Los archivos están en el workspace: src/backend/pyproject.toml y src/frontend/package.json. Continúa con las validaciones no destructivas y reporta la evidencia exacta de cada comando.'); await tb.press('Enter'); log('clarificación respondida desde la UI'); await page.waitForTimeout(8000); }
  else if (finalCard) { log('final renderizado'); await shot(page, '06-final'); break; }
  else { await page.waitForTimeout(6000); }
}
writeFileSync(`${S}/page_final.txt`, await page.innerText('body'));
await page.reload({ waitUntil: 'domcontentloaded' }); await page.waitForTimeout(4000); await shot(page, '07-after-refresh');
writeFileSync(`${S}/page_after_refresh.txt`, await page.innerText('body'));
log(`tras refresh: url=${page.url()} replans=${replans} clarificaciones=${clar}`);
await browser.close();
