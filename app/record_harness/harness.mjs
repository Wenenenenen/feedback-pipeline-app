// 自动录屏执行器：执行 qa-record 生成的录屏脚本，按人手的方式驱动真实的系统指针与键盘。
// 两个平台后端：
//   x11（backend-x11.mjs）：WSL 里的 Xvfb 虚拟屏，chromium 与 xterm，xdotool 驱动；qa-record 试跑用，也可正式录制。
//   win（backend-win.mjs）：Windows 本机桌面，本机 Chrome、Windows Terminal、真实的文件对话框，SendInput 驱动，
//                         ddagrab 录主屏、WASAPI 回环录系统声音；正式录制用，执行器在 Windows 的 node 里运行。
//
// 用法：node harness.mjs --script <录屏脚本.mjs> --cwd <录屏目录（WSL 路径）> --work <工作目录>
//         [--platform x11|win] [--out <输出.mp4>] [--dry-run] [--max-sec 90] [--audio <x11 的 pulse 音源 | win 的 1>]
//         x11：[--size 1920x1080] [--work-size 1920x1032] [--pw <playwright 模块路径>]
//         win：--pw <playwright-core 路径> --python <python.exe> --ffmpeg <ffmpeg.exe> --ffprobe <ffprobe.exe>
//              --chrome <chrome.exe> --root <Windows 侧运行目录> --distro <WSL 发行版>
// x11 另用环境变量：DISPLAY、FB_PLAYWRIGHT、FB_TERM_FONT、XCURSOR_PATH / XCURSOR_THEME、FONTCONFIG_FILE、PULSE_SERVER
//
// 录屏脚本是一个 ES module：
//   export async function prepare(ctx) {}      可选，开录前执行（如先打开产物页面），不录
//   export default async function run(ctx) {}  正式录制的部分
// 标准输出每行一个 JSON 事件，供后端写入运行日志。检测到真人操作电脑（win）时中止并以退出码 3 结束。
import { execFile } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';
import { clamp, gauss, humanPath, rand, sleep, typingDelays } from './human.mjs';

const require = createRequire(import.meta.url);
const EXIT_USER_INPUT = 3;

function parseArgs(argv) {
  const a = { dryRun: false, maxSec: 90, audio: '', platform: 'x11' };
  const size = s => { const [W, H] = String(s).split('x').map(Number); return { W, H }; };
  for (let i = 0; i < argv.length; i++) {
    const k = argv[i];
    const v = () => argv[++i];
    if (k === '--dry-run') a.dryRun = true;
    else if (k === '--max-sec') a.maxSec = Number(v());
    else if (k === '--size') a.size = size(v());
    else if (k === '--work-size') a.workSize = size(v());
    else if (k.startsWith('--')) a[k.slice(2).replace(/-(\w)/g, (_, c) => c.toUpperCase())] = v();
  }
  if (!a.script || !a.cwd || !a.work) throw new Error('缺少 --script、--cwd 或 --work 参数');
  if (!a.dryRun && !a.out) throw new Error('正式录制需要 --out');
  a.size ||= { W: 1920, H: 1080 };
  a.workSize ||= { W: a.size.W, H: a.size.H - 48 };
  a.pw ||= process.env.FB_PLAYWRIGHT || 'playwright';
  if (a.audio === '0') a.audio = '';
  return a;
}

const t0 = Date.now();
function emit(event, data = {}) {
  process.stdout.write(JSON.stringify({ event, t: +((Date.now() - t0) / 1000).toFixed(1), ...data }) + '\n');
}

function run(cmd, args, opts = {}) {
  return new Promise((resolve, reject) => {
    execFile(cmd, args, { timeout: opts.timeout ?? 30000 }, (err, stdout, stderr) => {
      if (err) reject(new Error(`${path.basename(cmd)} ${args.slice(0, 3).join(' ')} 失败：${stderr || err.message}`));
      else resolve(String(stdout || ''));
    });
  });
}

// 合成：两路都保留墙钟时间戳，按起始时刻对齐，声音晚开始就在前面补静音、早开始就裁掉多出的开头
async function mux(capture, out) {
  const probe = async (file, entries) => Number((await run(capture.ffprobe, ['-v', 'error', '-show_entries', entries,
    '-of', 'default=nw=1:nk=1', file])).trim().split('\n')[0]);
  const { files } = capture;
  // 保留墙钟时间戳时，容器的 duration 是结束时刻，减去开始时刻才是时长
  const vStart = await probe(files.video, 'stream=start_time');
  const vDur = (await probe(files.video, 'format=duration')) - vStart;
  const ff = ['-y', '-loglevel', 'error', '-i', files.video];
  const aStart = await capture.audioStart(probe);
  if (aStart !== null && fs.existsSync(files.audio)) {
    const offset = aStart - vStart;
    emit('mux', { audio_offset: +offset.toFixed(3) });
    if (offset < 0) ff.push('-ss', (-offset).toFixed(3));
    ff.push('-i', files.audio, '-map', '0:v', '-map', '1:a');
    if (offset > 0) ff.push('-af', `adelay=${Math.round(offset * 1000)}:all=1`);
    ff.push('-c:a', 'aac', '-b:a', '128k');
  }
  ff.push('-c:v', 'libx264', '-preset', 'veryfast', '-crf', '23', '-pix_fmt', 'yuv420p', '-r', '30',
    '-t', vDur.toFixed(3), '-movflags', '+faststart', out);
  await run(capture.ffmpeg, ff, { timeout: 600000 });
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  fs.mkdirSync(args.work, { recursive: true });
  const shotDir = path.join(args.work, 'shots');
  fs.mkdirSync(shotDir, { recursive: true });
  const pw = require(args.pw);
  const { createBackend } = await import(args.platform === 'win' ? './backend-win.mjs' : './backend-x11.mjs');
  const b = await createBackend(args, emit);

  // 真人碰了鼠标键盘就中止（只有本机桌面后端会触发）
  let interrupted = null;
  const userInput = new Promise((_, reject) => {
    b.onUserInput(m => {
      if (interrupted) return;
      interrupted = m;
      emit('user-input', { kind: m.kind });
      reject(new Error(`录屏中检测到有人操作电脑（${m.kind}），已中止`));
    });
  });
  userInput.catch(() => {});

  let inited;
  try {
    inited = await b.init(pw);
  } catch (e) {
    // 初始化到一半出错（如已打开终端、浏览器）也要把窗口关掉，不留在桌面上
    await b.close().catch(() => {});
    throw e;
  }
  const { page, context, browser, mouse: m0 } = inited;
  page.on('console', m => { if (m.type() === 'error') emit('console', { text: m.text().slice(0, 300) }); });
  page.on('pageerror', e => emit('pageerror', { text: String(e.message || e).slice(0, 300) }));
  let mouse = { ...m0 };

  const human = {
    async moveTo(x, y, ms) {
      const pts = humanPath(mouse, { x, y }, { ms, W: b.W, H: b.H });
      if (!pts.length) return;
      await b.movePath(pts);
      const last = pts[pts.length - 1];
      mouse = { x: last[0], y: last[1] };
    },
    async click(btn = 'left', count = 1) {
      for (let i = 0; i < count; i++) {
        if (i) await sleep(rand(70, 130));
        await b.button(btn, true);
        await sleep(rand(55, 120));
        await b.button(btn, false);
      }
    },
    type: (text, base = 70) => b.typeText(text, typingDelays(text, base)),
  };

  // 页面坐标（CSS 像素）转屏幕坐标
  async function toScreen(px, py) {
    const v = await b.viewport();
    return { x: v.x + px * v.dpr, y: v.y + py * v.dpr };
  }

  async function grab(file) {
    await b.grab(file);
    emit('shot', { path: file });
    return file;
  }

  // 等页面的 load 事件（导航由真实的按键或点击触发）
  async function navigated(trigger, opts = {}) {
    const loaded = page.waitForEvent('load', { timeout: (opts.timeout ?? 30) * 1000 });
    await trigger();
    await loaded;
    if (opts.waitUntil === 'networkidle') await page.waitForLoadState('networkidle').catch(() => {});
    await sleep(rand(300, 600));
  }

  const ensureBrowser = async () => { if (b.front() !== 'browser') await b.raise('browser'); };
  const target = t => (typeof t === 'string' ? page.locator(t).first() : t);
  const isPoint = t => t && typeof t === 'object' && typeof t.x === 'number' && typeof t.y === 'number';
  const ctx = {
    page, context, browser, cwd: args.cwd, dryRun: args.dryRun, sleep,
    screen: { width: b.W, height: b.H },
    step(n, desc) { emit('step', { n, desc }); },
    // 整块屏幕截图（看得到终端与指针），试跑时用来核对画面；正式录制时不截，避免抢占录制
    shot: name => (args.dryRun ? grab(path.join(shotDir, `${String(name).replace(/[^\w.-]/g, '_')}.png`))
      : Promise.resolve(emit('shot-skip', { name }))),
    showBrowser: () => b.raise('browser'),
    // 指针移到元素上（落点在中心附近随机，或页面坐标 {x, y} 精确落点），悬停效果与指针形状随之变化
    async moveTo(t, opts = {}) {
      await ensureBrowser();
      let p;
      if (isPoint(t)) p = await toScreen(t.x, t.y);
      else {
        const loc = target(t);
        await loc.scrollIntoViewIfNeeded();
        const box = await loc.boundingBox();
        if (!box) throw new Error(`找不到元素：${t}`);
        const exact = opts.dx !== undefined || opts.dy !== undefined;
        const jx = exact ? 0 : clamp(gauss() * box.width * 0.12, -box.width * 0.3, box.width * 0.3);
        const jy = exact ? 0 : clamp(gauss() * box.height * 0.12, -box.height * 0.3, box.height * 0.3);
        p = await toScreen(box.x + box.width / 2 + (opts.dx || 0) + jx, box.y + box.height / 2 + (opts.dy || 0) + jy);
      }
      await human.moveTo(p.x, p.y, opts.ms);
    },
    // 真实的系统点击：指针移过去、稍作停顿再按下
    async click(t, opts = {}) {
      await ctx.moveTo(t, opts);
      await sleep(opts.hover ?? rand(120, 300));
      await human.click(opts.button || 'left', opts.clickCount || 1);
      await sleep(rand(100, 220));
    },
    // 点中输入框后逐字输入，字与字之间的间隔有快有慢
    async type(t, text, opts = {}) {
      await ctx.click(t);
      await sleep(rand(150, 350));
      await b.typeInPage(String(text), typingDelays(text, opts.delay ?? 85));
    },
    // 系统按键（xdotool 写法，如 ctrl+a、Return、F5），发给当前窗口
    async key(combo, opts = {}) {
      await b.key(combo);
      await sleep(opts.pause ?? rand(150, 300));
    },
    // 滚轮滚动：正数向下，每格之间的间隔不完全一样
    async scroll(notches = 3) {
      await ensureBrowser();
      await b.wheel(notches);
    },
    // 点浏览器的刷新按钮并等页面加载完
    async reload(opts = {}) {
      await ensureBrowser();
      const { reload } = await b.chromePoints();
      await navigated(async () => {
        await human.moveTo(reload.x + gauss(), reload.y + gauss());
        await sleep(rand(150, 300));
        await human.click();
      }, opts);
    },
    // 点地址栏、打出网址回车并等页面加载完
    async goto(url, opts = {}) {
      await ensureBrowser();
      const { omnibox } = await b.chromePoints();
      await navigated(async () => {
        await human.moveTo(omnibox.x + gauss() * 20, omnibox.y + gauss());
        await sleep(rand(150, 300));
        await human.click();
        await sleep(rand(200, 400));
        await b.key('ctrl+a');
        await human.type(url, 55);
        await sleep(rand(250, 500));
        await b.key('Return');
      }, opts);
    },
    // 点上传按钮，在弹出的系统文件选择框里选文件
    async upload(t, file, opts = {}) {
      const files = Array.isArray(file) ? file : [file];
      if (files.length !== 1) throw new Error('ctx.upload 一次只能选一个文件');
      const abs = path.posix.resolve(args.cwd, files[0]);
      const before = await b.beforeUpload(abs);
      await ctx.click(t, opts);
      if (!(await b.chooseFile(before, abs, human))) {
        throw new Error('点击后没有弹出文件选择框：目标要是 <input type=file> 或会打开它的按钮、label，'
          + '且脚本里不能再监听 filechooser（会被 Playwright 拦截，不弹框）');
      }
      await sleep(rand(300, 600));
    },
    // 后台执行命令（不出现在画面上），用于在脚本里核对状态
    exec: (cmd, opts = {}) => b.exec(cmd, opts.timeout ?? 30),
    term: {
      show: () => b.raise('term'),
      // 切到终端，逐字打出命令并回车，等命令结束返回退出码；命令失败不抛错，由脚本决定
      async run(cmd, opts = {}) {
        if (b.front() !== 'term') await b.raise('term');
        const lines = () => fs.readFileSync(b.termStatus, 'utf8').split('\n').filter(Boolean);
        const before = lines().length;
        await sleep(rand(200, 500));
        await human.type(cmd, clamp(Math.round(2800 / Math.max(1, cmd.length)), 25, 80));
        await sleep(rand(250, 500));
        await b.key('Return');
        emit('term', { cmd });
        const limit = Date.now() + (opts.timeout ?? 30) * 1000;
        let code = null;
        while (Date.now() < limit) {
          const now = lines();
          if (now.length > before) { code = Number(now[now.length - 1]); break; }
          await sleep(100);
        }
        if (code === null) {
          await b.key('ctrl+c');
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

  let capture = null;
  let failed = null;
  try {
    await b.raise('browser');
    if (typeof mod.prepare === 'function') {
      emit('prepare');
      await Promise.race([mod.prepare(ctx), userInput]);
    }
    await sleep(500);
    if (!args.dryRun) {
      capture = b.startCapture();
      await sleep(1000);
      emit('record-start');
    }
    const limit = (args.maxSec - 3) * 1000;
    const started = Date.now();
    await Promise.race([
      mod.default(ctx),
      userInput,
      sleep(limit).then(() => { throw new Error(`录屏脚本超过 ${args.maxSec - 3} 秒仍未结束`); }),
    ]);
    emit('run-done', { seconds: +((Date.now() - started) / 1000).toFixed(1) });
    await sleep(1200);
  } catch (e) {
    failed = e;
    emit('error', { message: String(e.message || e).slice(0, 1500) });
    if (!interrupted) await grab(path.join(shotDir, 'error.png')).catch(() => {});
  } finally {
    if (capture) {
      await capture.stop();
      emit('record-stop');
    }
    await b.close();
  }
  if (interrupted) process.exit(EXIT_USER_INPUT);
  if (failed) process.exit(1);
  if (capture) {
    await mux(capture, args.out);
    emit('muxed', { out: args.out });
  }
  emit('done', { ok: true });
  process.exit(0);
}

main().catch(e => { emit('error', { message: String(e.stack || e).slice(0, 1500) }); process.exit(1); });
