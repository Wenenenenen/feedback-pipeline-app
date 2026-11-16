// 自动录屏执行器：在虚拟屏上打开有界面的 chromium（产物页面）与 xterm 终端（按 Windows Terminal 的配色与字体），
// 用 xdotool 驱动真实的系统指针与键盘，执行 qa-record 生成的录屏脚本；正式录制时 ffmpeg 录下整块虚拟屏、
// 系统指针（Windows 样式光标主题）与虚拟声卡。
//
// 用法：node harness.mjs --script <录屏脚本.mjs> --cwd <录屏目录> --work <工作目录>
//         [--out <输出.mp4>] [--dry-run] [--max-sec 90] [--audio <pulse 音源>]
// 环境变量：DISPLAY（虚拟屏）、FB_PLAYWRIGHT（playwright 模块的绝对路径）、FB_TERM_FONT（终端字体）、
//           XCURSOR_PATH / XCURSOR_THEME（指针主题）、FONTCONFIG_FILE（字体）、PULSE_SERVER（可选）
//
// 录屏脚本是一个 ES module：
//   export async function prepare(ctx) {}      可选，开录前执行（如先打开产物页面），不录
//   export default async function run(ctx) {}  正式录制的部分
// 标准输出每行一个 JSON 事件，供后端写入运行日志。
import { execFile, spawn } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';

const require = createRequire(import.meta.url);
const W = 1280;
const H = 720;

function parseArgs(argv) {
  const a = { dryRun: false, maxSec: 90, audio: '' };
  for (let i = 0; i < argv.length; i++) {
    const k = argv[i];
    const v = () => argv[++i];
    if (k === '--script') a.script = v();
    else if (k === '--cwd') a.cwd = v();
    else if (k === '--work') a.work = v();
    else if (k === '--out') a.out = v();
    else if (k === '--dry-run') a.dryRun = true;
    else if (k === '--max-sec') a.maxSec = Number(v());
    else if (k === '--audio') a.audio = v();
  }
  if (!a.script || !a.cwd || !a.work) throw new Error('缺少 --script、--cwd 或 --work 参数');
  if (!a.dryRun && !a.out) throw new Error('正式录制需要 --out');
  return a;
}

const t0 = Date.now();
function emit(event, data = {}) {
  process.stdout.write(JSON.stringify({ event, t: +((Date.now() - t0) / 1000).toFixed(1), ...data }) + '\n');
}
const sleep = ms => new Promise(r => setTimeout(r, ms));
const shq = s => `'${String(s).replace(/'/g, `'\\''`)}'`;

function run(cmd, args, opts = {}) {
  return new Promise((resolve, reject) => {
    execFile(cmd, args, { timeout: opts.timeout ?? 30000, env: process.env }, (err, stdout, stderr) => {
      if (err && !opts.allowFail) reject(new Error(`${cmd} ${args.slice(0, 3).join(' ')} 失败：${stderr || err.message}`));
      else resolve(String(stdout || ''));
    });
  });
}
const xdo = (...args) => run('xdotool', args.map(String));

// Windows Terminal 默认的 Campbell 配色
const CAMPBELL = ['#0C0C0C', '#C50F1F', '#13A10E', '#C19C00', '#0037DA', '#881798', '#3A96DD', '#CCCCCC',
  '#767676', '#E74856', '#16C60C', '#F9F1A5', '#3B78FF', '#B4009E', '#61D6D6', '#F2F2F2'];

async function findWindow(args, tries = 50) {
  for (let i = 0; i < tries; i++) {
    const out = await run('xdotool', ['search', '--onlyvisible', ...args], { allowFail: true });
    const ids = out.split('\n').map(s => s.trim()).filter(Boolean);
    if (ids.length) return ids[ids.length - 1];
    await sleep(200);
  }
  throw new Error(`找不到窗口：${args.join(' ')}`);
}

async function startTerminal(cwd, work) {
  const status = path.join(work, 'term.status');
  fs.writeFileSync(status, '');
  const rc = path.join(work, 'term.bashrc');
  // 正常加载 ~/.bashrc（提示符与新开 WSL 终端一致），每条命令结束把退出码追加到状态文件；不写入命令历史
  fs.writeFileSync(rc, [
    '[ -f ~/.bashrc ] && . ~/.bashrc',
    'unset HISTFILE',
    `__fb_status() { local c=$?; echo "$c" >> ${shq(status)}; return $c; }`,
    'PROMPT_COMMAND="__fb_status${PROMPT_COMMAND:+;$PROMPT_COMMAND}"',
    `cd ${shq(cwd)}`,
    'clear',
    '',
  ].join('\n'));
  const font = process.env.FB_TERM_FONT || 'DejaVu Sans Mono';
  const res = [
    // 字体列表里带上微软雅黑，等宽字体缺的中文字形由它补上
    `XTerm*faceName: ${font},Microsoft YaHei,Noto Sans Mono CJK SC`, 'XTerm*faceSize: 12',
    'XTerm*renderFont: true', 'XTerm*utf8: 1', 'XTerm*termName: xterm-256color',
    'XTerm*background: #0C0C0C', 'XTerm*foreground: #CCCCCC', 'XTerm*cursorColor: #CCCCCC',
    'XTerm*scrollBar: false', 'XTerm*internalBorder: 12', 'XTerm*borderWidth: 0', 'XTerm*saveLines: 2000',
    ...CAMPBELL.map((c, i) => `XTerm*color${i}: ${c}`),
  ];
  const user = process.env.USER || 'user';
  const home = process.env.HOME || '';
  const title = `${user}: ${home && cwd.startsWith(home) ? '~' + cwd.slice(home.length) : cwd}`;
  const proc = spawn('xterm', [...res.flatMap(r => ['-xrm', r]), '-geometry', '150x40+0+0', '-T', title,
    '-e', 'bash', '--rcfile', rc, '-i'], { cwd, env: process.env, stdio: 'ignore' });
  const win = await findWindow(['--pid', proc.pid]);
  await xdo('windowmove', win, 0, 0);
  await xdo('windowsize', win, W, H);
  // 等提示符出现（rc 里 clear 之后第一次显示提示符时会写一行状态）
  for (let i = 0; i < 50 && !fs.readFileSync(status, 'utf8').trim(); i++) await sleep(100);
  return { proc, win, status };
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const { chromium } = require(process.env.FB_PLAYWRIGHT || 'playwright');
  fs.mkdirSync(args.work, { recursive: true });
  const shotDir = path.join(args.work, 'shots');
  fs.mkdirSync(shotDir, { recursive: true });
  const display = process.env.DISPLAY;

  // 先开终端再开浏览器：浏览器在上层，开录时停在产物页面
  const term = await startTerminal(args.cwd, args.work);
  // Chromium 的指针主题与界面字体取自 GTK 设置：只给浏览器一份单独的配置目录，指定 Windows 样式指针与 Segoe UI
  const gtkHome = path.join(args.work, 'gtk-config');
  for (const v of ['gtk-3.0', 'gtk-4.0']) {
    fs.mkdirSync(path.join(gtkHome, v), { recursive: true });
    fs.writeFileSync(path.join(gtkHome, v, 'settings.ini'), ['[Settings]',
      `gtk-cursor-theme-name=${process.env.XCURSOR_THEME || 'default'}`,
      `gtk-cursor-theme-size=${process.env.XCURSOR_SIZE || 32}`, 'gtk-font-name=Segoe UI 9', ''].join('\n'));
  }
  const browser = await chromium.launch({
    headless: false,
    env: { ...process.env, XDG_CONFIG_HOME: gtkHome },
    ignoreDefaultArgs: ['--enable-automation'],
    args: [`--window-position=0,0`, `--window-size=${W},${H}`, '--no-first-run', '--no-default-browser-check',
      '--force-device-scale-factor=1', '--autoplay-policy=no-user-gesture-required', '--use-angle=swiftshader',
      '--enable-unsafe-swiftshader', '--ignore-gpu-blocklist', '--enable-unsafe-webgpu',
      '--disable-features=Translate,TranslateUI', '--lang=zh-CN'],
  });
  const context = await browser.newContext({ viewport: null, locale: 'zh-CN', acceptDownloads: true });
  const page = await context.newPage();
  page.on('console', m => { if (m.type() === 'error') emit('console', { text: m.text().slice(0, 300) }); });
  page.on('pageerror', e => emit('pageerror', { text: String(e.message || e).slice(0, 300) }));
  const browserWin = await findWindow(['--class', 'chrom']);

  let front = 'browser';
  let mouse = { x: Math.round(W / 2), y: Math.round(H * 0.6) };
  await xdo('mousemove', mouse.x, mouse.y);

  async function raise(which) {
    const win = which === 'term' ? term.win : browserWin;
    await xdo('windowraise', win);
    await xdo('windowfocus', win).catch(() => {});
    front = which;
    await sleep(150);
  }

  // 系统指针平滑移动（先快后慢），录屏里与真人移动鼠标相近
  async function moveTo(x, y, ms) {
    x = Math.max(0, Math.min(W - 1, Math.round(x)));
    y = Math.max(0, Math.min(H - 1, Math.round(y)));
    const dist = Math.hypot(x - mouse.x, y - mouse.y);
    if (dist < 2) return;
    const dur = ms ?? Math.min(900, 250 + dist * 0.6);
    const steps = Math.max(8, Math.min(40, Math.round(dist / 20)));
    const chain = [];
    for (let i = 1; i <= steps; i++) {
      const t = i / steps;
      const e = 1 - Math.pow(1 - t, 3);
      chain.push('mousemove', Math.round(mouse.x + (x - mouse.x) * e), Math.round(mouse.y + (y - mouse.y) * e),
        'sleep', (dur / steps / 1000).toFixed(3));
    }
    await xdo(...chain);
    mouse = { x, y };
  }

  // 页面坐标转屏幕坐标：浏览器窗口在 (0,0)，内容区在标签栏与地址栏下方
  async function toScreen(px, py) {
    const o = await page.evaluate(() => ({ x: window.screenX + (window.outerWidth - window.innerWidth),
      y: window.screenY + (window.outerHeight - window.innerHeight) }));
    return { x: o.x + px, y: o.y + py };
  }

  async function grab(file) {
    await run('ffmpeg', ['-loglevel', 'error', '-y', '-f', 'x11grab', '-draw_mouse', '1', '-video_size', `${W}x${H}`,
      '-i', `${display}.0`, '-frames:v', '1', file], { timeout: 15000 });
    emit('shot', { path: file });
    return file;
  }

  const target = t => (typeof t === 'string' ? page.locator(t).first() : t);
  const isPoint = t => t && typeof t === 'object' && typeof t.x === 'number' && typeof t.y === 'number';
  const ctx = {
    page, context, browser, cwd: args.cwd, dryRun: args.dryRun, sleep,
    step(n, desc) { emit('step', { n, desc }); },
    // 整块虚拟屏截图（看得到终端与指针），试跑时用来核对画面
    shot: name => grab(path.join(shotDir, `${String(name).replace(/[^\w.-]/g, '_')}.png`)),
    showBrowser: () => raise('browser'),
    // 指针移到元素中心（或页面坐标 {x, y}），悬停效果与指针形状随之变化
    async moveTo(t, opts = {}) {
      if (front !== 'browser') await raise('browser');
      let p;
      if (isPoint(t)) p = await toScreen(t.x, t.y);
      else {
        const loc = target(t);
        await loc.scrollIntoViewIfNeeded();
        const box = await loc.boundingBox();
        if (!box) throw new Error(`找不到元素：${t}`);
        p = await toScreen(box.x + box.width / 2 + (opts.dx || 0), box.y + box.height / 2 + (opts.dy || 0));
      }
      await moveTo(p.x, p.y, opts.ms);
    },
    // 真实的系统点击：指针移过去再按下
    async click(t, opts = {}) {
      await ctx.moveTo(t, opts);
      await sleep(opts.hover ?? 180);
      const btn = { left: 1, middle: 2, right: 3 }[opts.button || 'left'];
      await xdo('click', '--repeat', opts.clickCount || 1, '--delay', 90, btn);
      await sleep(120);
    },
    async type(t, text, opts = {}) {
      await ctx.click(t);
      await page.keyboard.type(String(text), { delay: opts.delay ?? 60 });
    },
    // 滚轮滚动：正数向下
    async scroll(notches = 3) {
      if (front !== 'browser') await raise('browser');
      await xdo('click', '--repeat', Math.abs(notches), '--delay', 120, notches > 0 ? 5 : 4);
    },
    // 后台执行命令（不出现在画面上），用于在脚本里核对状态
    exec(cmd, opts = {}) {
      return new Promise(resolve => {
        execFile('bash', ['-c', cmd], { cwd: args.cwd, env: process.env, timeout: (opts.timeout ?? 30) * 1000 },
          (err, stdout, stderr) => resolve({ code: err ? (err.code ?? 1) : 0, output: String(stdout) + String(stderr) }));
      });
    },
    term: {
      show: () => raise('term'),
      // 切到终端，逐字打出命令并回车，等命令结束返回退出码；命令失败不抛错，由脚本决定
      async run(cmd, opts = {}) {
        if (front !== 'term') await raise('term');
        const lines = () => fs.readFileSync(term.status, 'utf8').split('\n').filter(Boolean);
        const before = lines().length;
        const delay = Math.max(15, Math.min(70, Math.round(2500 / Math.max(1, cmd.length))));
        await xdo('type', '--delay', delay, '--', cmd);
        await sleep(300);
        await xdo('key', 'Return');
        emit('term', { cmd });
        const limit = Date.now() + (opts.timeout ?? 30) * 1000;
        let code = null;
        while (Date.now() < limit) {
          const now = lines();
          if (now.length > before) { code = Number(now[now.length - 1]); break; }
          await sleep(100);
        }
        if (code === null) {
          await xdo('key', 'ctrl+c');
          code = 124;
          emit('term-timeout', { cmd });
        }
        emit('term-exit', { code });
        await sleep(opts.pause ?? 900);
        return { code };
      },
    },
    get mouse() { return { ...mouse }; },
  };

  const mod = await import(pathToFileURL(path.resolve(args.script)).href);
  if (typeof mod.default !== 'function') throw new Error('录屏脚本需要 export default async function run(ctx)');

  let ffmpeg = null;
  let failed = null;
  try {
    await raise('browser');
    if (typeof mod.prepare === 'function') {
      emit('prepare');
      await mod.prepare(ctx);
    }
    await sleep(500);
    if (!args.dryRun) {
      const ff = ['-y', '-loglevel', 'error', '-f', 'x11grab', '-draw_mouse', '1', '-video_size', `${W}x${H}`,
        '-framerate', '30', '-i', `${display}.0`];
      if (args.audio) ff.push('-f', 'pulse', '-i', args.audio);
      ff.push('-t', String(args.maxSec), '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '23', '-pix_fmt', 'yuv420p');
      if (args.audio) ff.push('-c:a', 'aac', '-b:a', '128k');
      ff.push('-movflags', '+faststart', args.out);
      ffmpeg = spawn('ffmpeg', ff, { stdio: ['pipe', 'ignore', 'pipe'] });
      ffmpeg.stderr.on('data', b => emit('ffmpeg', { text: b.toString().slice(0, 300) }));
      await sleep(800);
      emit('record-start');
    }
    const limit = (args.maxSec - 3) * 1000;
    const started = Date.now();
    await Promise.race([
      mod.default(ctx),
      sleep(limit).then(() => { throw new Error(`录屏脚本超过 ${args.maxSec - 3} 秒仍未结束`); }),
    ]);
    emit('run-done', { seconds: +((Date.now() - started) / 1000).toFixed(1) });
    await sleep(1200);
  } catch (e) {
    failed = e;
    emit('error', { message: String(e.message || e).slice(0, 1500) });
    await grab(path.join(shotDir, 'error.png')).catch(() => {});
  } finally {
    if (ffmpeg) {
      ffmpeg.stdin.write('q');
      ffmpeg.stdin.end();
      await new Promise(r => { ffmpeg.on('close', r); setTimeout(r, 15000); });
      emit('record-stop');
    }
    await browser.close().catch(() => {});
    term.proc.kill();
  }
  if (failed) process.exit(1);
  emit('done', { ok: true });
}

main().catch(e => { emit('error', { message: String(e.stack || e).slice(0, 1500) }); process.exit(1); });
