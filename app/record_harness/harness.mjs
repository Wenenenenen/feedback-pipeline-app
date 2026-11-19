// 自动录屏执行器：执行 qa-record 生成的录屏脚本，按人手的方式驱动真实的系统指针与键盘。
// 两个平台后端：
//   x11（backend-x11.mjs）：WSL 里的 Xvfb 虚拟屏，chromium 与 xterm，xdotool 驱动；qa-record 试跑用，也可正式录制。
//   win（backend-win.mjs）：Windows 本机桌面，本机 Chrome、Windows Terminal、真实的文件对话框，SendInput 驱动，
//                         ddagrab 录主屏、WASAPI 回环录系统声音；正式录制用，执行器在 Windows 的 node 里运行。
//
// 用法：node harness.mjs --script <录屏脚本.mjs> --cwd <录屏目录（WSL 路径）> --work <工作目录>
//         [--platform x11|win] [--out <输出.mp4>] [--dry-run] [--max-sec 90] [--audio <x11 的 pulse 音源 | win 的 1>]
//         [--out-height 720]（输出视频的高度，屏幕更高时按比例缩小）
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
import { clamp, gauss, GOTO_RETRY_SEC, humanPath, isLongText, isRetryableGoto, rand, revealShift, sleep, typingDelays } from './human.mjs';
import { tailPad } from './muxutil.mjs';

const require = createRequire(import.meta.url);
const EXIT_USER_INPUT = 3;

function parseArgs(argv) {
  const a = { dryRun: false, maxSec: 90, audio: '', platform: 'x11', outHeight: 720 };
  const size = s => { const [W, H] = String(s).split('x').map(Number); return { W, H }; };
  for (let i = 0; i < argv.length; i++) {
    const k = argv[i];
    const v = () => argv[++i];
    if (k === '--dry-run') a.dryRun = true;
    else if (k === '--max-sec') a.maxSec = Number(v());
    else if (k === '--out-height') a.outHeight = Number(v());
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

// 合成：两路都保留墙钟时间戳，按起始时刻对齐，声音晚开始就在前面补静音、早开始就裁掉多出的开头；
// 画面按屏幕原尺寸录，合成时缩到 outHeight（作业要求 720p），宽度按比例取偶数
async function mux(capture, out, outHeight, stopWall = null) {
  const probe = async (file, entries) => Number((await run(capture.ffprobe, ['-v', 'error', '-show_entries', entries,
    '-of', 'default=nw=1:nk=1', file])).trim().split('\n')[0]);
  const { files } = capture;
  // 保留墙钟时间戳时，容器的 duration 是结束时刻，减去开始时刻才是时长
  const vStart = await probe(files.video, 'stream=start_time');
  let vDur = (await probe(files.video, 'format=duration')) - vStart;
  const tail = capture.wallclock ? tailPad(stopWall, vStart, vDur) : 0;
  if (tail) emit('tail-pad', { seconds: tail });
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
  const vH = await probe(files.video, 'stream=height');
  const vf = [];
  if (outHeight && vH > outHeight) vf.push(`scale=-2:${outHeight}:flags=lanczos`);
  // 结尾静止的停留用最后一帧补齐，声音本来就录到了停止时刻
  if (tail) {
    vf.push(`tpad=stop_mode=clone:stop_duration=${tail}`);
    vDur += tail;
  }
  if (vf.length) ff.push('-vf', vf.join(','));
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
  // 本机桌面的 Chrome 经 WSL 的 localhost 转发访问录屏前准备启动的服务：同一端口刚关掉又重开（试跑收尾后重新准备）时，
  // 转发偶尔要过几秒才恢复，期间连接被拒。脚本里的 page.goto 遇到这类连接错误时等一会再试，最多等 GOTO_RETRY_SEC 秒
  const rawGoto = page.goto.bind(page);
  page.goto = async (url, opts) => {
    const end = Date.now() + GOTO_RETRY_SEC * 1000;
    for (let n = 1; ; n++) {
      try {
        return await rawGoto(url, opts);
      } catch (e) {
        if (!isRetryableGoto(e) || Date.now() >= end) throw e;
        emit('goto-retry', { n, url: String(url).slice(0, 200), error: String(e.message || e).split('\n')[0].slice(0, 160) });
        await sleep(1000);
      }
    }
  };
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
    // 系统键盘输入（终端、地址栏、文件名框）：逐字打；超过 200 字的单行文本改为粘贴
    //（多行文本粘贴进 Windows Terminal 会弹多行粘贴确认框，仍逐字打）
    async type(text, base = 70) {
      if (isLongText(text) && !String(text).includes('\n')) {
        emit('paste', { chars: [...String(text)].length });
        await b.pasteText(String(text));
        return;
      }
      await b.typeText(text, typingDelays(text, base));
    },
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

  // 还按着的键与鼠标键：脚本出错或被打断时在收尾统一松开，不能让按键卡在按下状态
  const held = { keys: new Set(), buttons: new Set() };
  async function releaseHeld() {
    for (const k of [...held.keys].reverse()) {
      await b.keyUp(k).catch(() => {});
      held.keys.delete(k);
    }
    for (const btn of [...held.buttons]) {
      await b.button(btn, false).catch(() => {});
      held.buttons.delete(btn);
    }
  }

  // 回到浏览器：打开着下载的文件（看图、记事本）时先关掉，与人看完关窗口一样
  const ensureBrowser = async () => {
    if (b.viewerOpen?.()) await b.closeFile();
    if (b.front() !== 'browser') await b.raise('browser');
  };
  const target = t => (typeof t === 'string' ? page.locator(t).first() : t);
  const isPoint = t => t && typeof t === 'object' && typeof t.x === 'number' && typeof t.y === 'number';
  // 页面坐标点（脚本按 boundingBox 算出的 {x, y}）在可视区外时先把页面滚过去，返回滚动后对应的新坐标；
  // 与 moveTo 元素时的 scrollIntoViewIfNeeded 一样，避免指针落到任务栏或可视区外
  async function revealPoints(pts) {
    if (!pts.length) return pts;
    const view = await page.evaluate(() => ({ w: window.innerWidth, h: window.innerHeight }));
    const want = revealShift(pts, view);
    if (!want.dx && !want.dy) return pts;
    const got = await page.evaluate(([dx, dy]) => {
      const x0 = window.scrollX;
      const y0 = window.scrollY;
      window.scrollBy({ left: dx, top: dy, behavior: 'instant' });
      return { dx: window.scrollX - x0, dy: window.scrollY - y0 };
    }, [want.dx, want.dy]);
    emit('reveal', { want, got });
    if (got.dx || got.dy) await sleep(rand(250, 450));
    return pts.map(p => ({ ...p, x: p.x - got.dx, y: p.y - got.dy }));
  }
  const ctx = {
    page, context, browser, cwd: args.cwd, dryRun: args.dryRun, sleep,
    screen: { width: b.W, height: b.H },
    step(n, desc) { emit('step', { n, desc }); },
    // 整块屏幕截图（看得到终端与指针），试跑时用来核对画面；正式录制时不截，避免抢占录制
    shot: name => (args.dryRun ? grab(path.join(shotDir, `${String(name).replace(/[^\w.-]/g, '_')}.png`))
      : Promise.resolve(emit('shot-skip', { name }))),
    showBrowser: () => ensureBrowser(),
    // 指针移到元素上（落点在中心附近随机，或页面坐标 {x, y} 精确落点），悬停效果与指针形状随之变化
    async moveTo(t, opts = {}) {
      await ensureBrowser();
      let p;
      if (isPoint(t)) {
        const [q] = opts.noReveal ? [t] : await revealPoints([t]);
        p = await toScreen(q.x, q.y);
      } else {
        const loc = target(t);
        await loc.scrollIntoViewIfNeeded();
        const box = await loc.boundingBox();
        if (!box) throw new Error(`找不到元素：${t}`);
        const exact = opts.dx !== undefined || opts.dy !== undefined;
        let jx = exact ? 0 : clamp(gauss() * box.width * 0.12, -box.width * 0.3, box.width * 0.3);
        // 数字输入框右侧有增减箭头，落点偏到中心偏左，避免点到箭头把值加减 1
        if (!exact && await loc.evaluate(el => el.tagName === 'INPUT' && el.type === 'number').catch(() => false)) {
          jx = -box.width * 0.15 + clamp(gauss() * box.width * 0.06, -box.width * 0.12, box.width * 0.12);
        }
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
    // 点中输入框后逐字输入，字与字之间的间隔有快有慢；超过 200 字的长文本改为粘贴
    async type(t, text, opts = {}) {
      await ctx.click(t);
      await sleep(rand(150, 350));
      if (isLongText(text)) {
        // 超过 200 字的长文本与人一样粘贴，不逐字打
        emit('paste', { chars: [...String(text)].length });
        await b.pasteInPage(String(text));
        await sleep(rand(250, 500));
      } else {
        await b.typeInPage(String(text), typingDelays(text, opts.delay ?? 85));
      }
    },
    // 系统按键（xdotool 写法，如 ctrl+a、Return、F5），发给当前窗口
    async key(combo, opts = {}) {
      await b.key(combo);
      await sleep(opts.pause ?? rand(150, 300));
    },
    // 按下不放（写法同 key，组合键按顺序依次按下），之后要用 keyUp 松开；用于按住方向键的同时再按别的键
    async keyDown(combo) {
      await ensureBrowser();
      await b.keyDown(combo);
      held.keys.add(combo);
      emit('key-down', { combo });
    },
    async keyUp(combo) {
      await b.keyUp(combo);
      held.keys.delete(combo);
      emit('key-up', { combo });
    },
    // 按住按键一段时间再松开：第二个参数是毫秒数，或者一个等待函数（等页面上出现某个现象就松开，超过 timeout 秒报错）
    async hold(combo, msOrUntil, opts = {}) {
      await ctx.keyDown(combo);
      try {
        if (typeof msOrUntil === 'function') {
          const sec = opts.timeout ?? 10;
          await Promise.race([msOrUntil(), sleep(sec * 1000).then(() => {
            throw new Error(`按住 ${combo} 等待的现象 ${sec} 秒内没有出现`);
          })]);
        } else {
          await sleep(Number(msOrUntil) || 0);
        }
      } finally {
        await ctx.keyUp(combo);
      }
      await sleep(opts.pause ?? rand(150, 300));
    },
    // 拖动：指针移到起点、按下、沿人手的轨迹移到终点（可以给多个途经点）、松开。
    // 起点、终点的写法同 moveTo；终点也可以写 {dx, dy}，表示从当前位置按页面像素偏移（如在画布上拖动旋转视角）
    async drag(from, to, opts = {}) {
      await ensureBrowser();
      const btn = opts.button || 'left';
      let stops = Array.isArray(to) ? to : [to];
      if (!stops.length) throw new Error('ctx.drag 至少要给一个终点');
      // 起点与各途经点里的页面坐标一起滚进可视区，同一次滚动后整体换算，保持彼此的相对位置
      const pointIdx = [from, ...stops].map((s, i) => (isPoint(s) ? i : -1)).filter(i => i >= 0);
      if (pointIdx.length) {
        const all = [from, ...stops];
        const moved = await revealPoints(pointIdx.map(i => all[i]));
        pointIdx.forEach((i, k) => { all[i] = moved[k]; });
        [from, ...stops] = all;
      }
      await ctx.moveTo(from, { ...(opts.from || {}), noReveal: true });
      await sleep(opts.hover ?? rand(150, 300));
      await b.button(btn, true);
      held.buttons.add(btn);
      emit('drag', { button: btn, stops: stops.length });
      try {
        await sleep(rand(90, 160));
        for (const s of stops) {
          const ms = opts.ms ?? rand(500, 900);
          if (s && typeof s === 'object' && !isPoint(s) && (s.dx !== undefined || s.dy !== undefined)
              && typeof s.locator !== 'function' && typeof s.boundingBox !== 'function') {
            const v = await b.viewport();
            await human.moveTo(mouse.x + (s.dx || 0) * v.dpr, mouse.y + (s.dy || 0) * v.dpr, ms);
          } else {
            // 按下鼠标后不能再滚动页面，途经点已在上面统一换算过
            await ctx.moveTo(s, { ms, noReveal: true });
          }
          await sleep(opts.pause ?? rand(60, 140));
        }
      } finally {
        await b.button(btn, false);
        held.buttons.delete(btn);
      }
      await sleep(rand(150, 300));
    },
    // 在浏览器开发者工具的控制台里执行一行表达式，画面上看得到打开控制台、逐字输入和返回值：
    // 只用于录屏步骤要求读取精确像素等页面上没有入口的数值；默认看完结果后关掉开发者工具
    async console(expr, opts = {}) {
      await ensureBrowser();
      const text = String(expr).replace(/\s*\n\s*/g, ' ').trim();
      await b.key('ctrl+shift+j');
      // 等开发者工具打开并把焦点放到控制台输入行
      await sleep(opts.openWait ?? rand(1500, 2200));
      emit('console-run', { expr: text.slice(0, 200) });
      await b.typeText(text, typingDelays(text, opts.delay ?? 40));
      await sleep(rand(300, 600));
      await b.key('Return');
      await sleep(opts.pause ?? 2500);
      if (opts.close !== false) {
        await b.key('F12');
        await sleep(rand(400, 700));
      }
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
        // 长网址（如本地文件路径）与人一样粘贴，不逐字打；短网址按正常速度打完整（地址栏的自动补全会被覆盖）
        if ([...String(url)].length > 30) {
          emit('paste', { chars: [...String(url)].length });
          await b.pasteText(String(url));
        } else {
          await human.type(url, 110);
        }
        await sleep(rand(250, 500));
        await b.key('Return');
      }, opts);
    },
    // 原生下拉框（<select>）选一项：点开下拉框，指针移到列表里的目标项上再点，与人一样；
    // 找不到弹出列表（虚拟屏）或列表长到要滚动时，改用方向键一格一格移过去再回车，不会一下跳到目标项。
    // option 是选项的 value 或显示文字，也可以写 {value}、{label}、{index}
    async select(t, option, opts = {}) {
      await ensureBrowser();
      const loc = target(t);
      await loc.scrollIntoViewIfNeeded();
      const info = () => loc.evaluate((el, want) => {
        if (el.tagName !== 'SELECT') return { error: '目标不是 <select> 元素' };
        const opts = [...el.options];
        const shown = o => !o.hidden && getComputedStyle(o).display !== 'none';
        let hit;
        if (want && typeof want === 'object') {
          if (want.index !== undefined) hit = opts[want.index];
          else if (want.value !== undefined) hit = opts.find(o => o.value === String(want.value));
          else hit = opts.find(o => o.label.trim() === String(want.label).trim());
        } else {
          hit = opts.find(o => o.value === String(want)) || opts.find(o => o.label.trim() === String(want).trim());
        }
        if (!hit) return { error: `下拉框里没有选项 ${JSON.stringify(want)}` };
        // 弹出列表里的每一行：分组标题也占一行，隐藏的选项不显示
        const rows = [];
        for (const c of el.children) {
          if (c.tagName === 'OPTGROUP') {
            if (getComputedStyle(c).display === 'none') continue;
            rows.push(null);
            for (const o of c.children) if (o.tagName === 'OPTION' && shown(o)) rows.push(o);
          } else if (c.tagName === 'OPTION' && shown(c)) rows.push(c);
        }
        const enabled = o => o && !o.disabled && !(o.parentElement.tagName === 'OPTGROUP' && o.parentElement.disabled);
        if (!enabled(hit)) return { error: `选项 ${JSON.stringify(want)} 是禁用的` };
        const cur = el.options[el.selectedIndex];
        const from = rows.indexOf(cur);
        const to = rows.indexOf(hit);
        // 方向键要按几下：两行之间（含目标）可选的选项数，向上为负
        let steps = 0;
        if (from >= 0) {
          const [a, b] = from < to ? [from + 1, to] : [to, from - 1];
          for (let i = a; i <= b; i++) if (enabled(rows[i])) steps += 1;
          if (from > to) steps = -steps;
        } else steps = rows.slice(0, to + 1).filter(enabled).length;
        return { listbox: el.multiple || el.size > 1, index: hit.index, row: to, rows: rows.length, steps };
      }, option);
      let s = await info();
      if (s.error) throw new Error(s.error);
      if (s.listbox) {
        // 列表框（size>1 或多选）的选项直接画在页面里，像按钮一样点
        await ctx.click(loc.locator('option').nth(s.index), opts);
        return;
      }
      // 记下选中的是哪一项：窗口上的捕获监听先于页面自己的处理执行（有的页面选完立刻把下拉框改回第一项，
      // 事后读 selectedIndex 会误判）；选的就是当前项时不触发事件，按 selectedIndex 判断
      const watchPick = () => loc.evaluate(el => {
        el.__fbPicked = null;
        if (!el.__fbWatch) {
          el.__fbWatch = true;
          window.addEventListener('input', e => { if (e.target === el) el.__fbPicked = el.selectedIndex; }, true);
        }
      });
      const picked = () => loc.evaluate(el => el.__fbPicked ?? el.selectedIndex);
      await watchPick();
      const before = b.beforePopup ? await b.beforePopup() : null;
      await ctx.click(loc, opts);
      await sleep(rand(250, 450));
      const rect = before ? await b.popupRect(before) : null;
      let done = false;
      if (rect && s.rows <= 20) {
        // 列表最多显示 20 行，不滚动时各行等高，按行号算出目标项的位置
        const [l, tp, r, bt] = rect;
        const rowH = (bt - tp) / s.rows;
        if (rowH >= 12) {
          const y = tp + rowH * (s.row + 0.5 + clamp(gauss() * 0.12, -0.25, 0.25));
          await human.moveTo(l + (r - l) * rand(0.2, 0.55), y);
          await sleep(rand(250, 500));
          await human.click();
          await sleep(rand(250, 400));
          done = (await picked()) === s.index;
          if (!done) {
            emit('select-retry', { option });
            // 点偏了：列表还开着就先关掉，再用方向键选
            if (await b.popupRect(before, 200)) await b.key('Escape');
            await sleep(rand(200, 350));
            s = await info();
            await watchPick();
            await ctx.click(loc, opts);
            await sleep(rand(250, 450));
          }
        }
      }
      if (!done) {
        // 方向键一格一格移到目标项，再回车确认
        for (let i = 0; i < Math.abs(s.steps); i++) {
          await b.key(s.steps > 0 ? 'Down' : 'Up');
          await sleep(rand(160, 300));
        }
        await sleep(rand(150, 300));
        await b.key('Return');
        await sleep(rand(200, 350));
      }
      if ((await picked()) !== s.index) {
        throw new Error(`下拉框没有选中 ${JSON.stringify(option)}`);
      }
    },
    // 点上传按钮，在弹出的系统文件选择框里选文件；file 为数组时一次多选（文件名不能重复，输入框要支持 multiple）
    async upload(t, file, opts = {}) {
      const files = Array.isArray(file) ? file : [file];
      if (!files.length) throw new Error('ctx.upload 至少要给一个文件');
      const abs = files.map(f => path.posix.resolve(args.cwd, f));
      const names = abs.map(f => path.posix.basename(f));
      if (new Set(names).size !== names.length) throw new Error(`ctx.upload 多选的文件名不能重复：${names.join(', ')}`);
      const one = abs.length === 1 ? abs[0] : abs;
      const before = await b.beforeUpload(one);
      // 后端不能在系统文件框里多选时（虚拟屏的 GTK 文件框），由它在点击前接管选择框，点击后直接设置文件
      const chooser = abs.length > 1 && b.multiUploadVia === 'filechooser'
        ? page.waitForEvent('filechooser', { timeout: 6000 }) : null;
      await ctx.click(t, opts);
      if (chooser) {
        const fc = await chooser.catch(() => null);
        if (!fc) throw new Error('点击后没有弹出文件选择框：目标要是 <input type=file> 或会打开它的按钮、label');
        if (!fc.isMultiple()) throw new Error('这个文件输入框不支持多选（没有 multiple），改成一次选一个文件');
        emit('upload-multi', { names, via: 'filechooser' });
        await fc.setFiles(abs);
        await sleep(rand(300, 600));
        return;
      }
      if (!(await b.chooseFile(before, one, human))) {
        throw new Error('点击后没有弹出文件选择框：目标要是 <input type=file> 或会打开它的按钮、label，'
          + '且脚本里不能再监听 filechooser（会被 Playwright 拦截，不弹框）');
      }
      await sleep(rand(300, 600));
    },
    // 点下载按钮并等下载完成，返回 {name, path}：本机桌面上由 Chrome 按原文件名存进「下载」下以分支命名的文件夹
    // （下载气泡显示文件名与对应格式的图标）；path 可以直接传给 ctx.upload 导回、传给 ctx.openFile 查看
    async download(t, opts = {}) {
      await ensureBrowser();
      const d = await b.download(() => ctx.click(t, opts), opts.timeout ?? 60);
      await sleep(rand(600, 1000));
      return d;
    },
    // 用系统程序查看文件（不要在浏览器地址栏里打开）：本机桌面上刚下载的文件直接点下载气泡里那一行，其他文件
    // Win+E 打开资源管理器粘贴文件夹路径后双击，都用默认程序打开；file 是 ctx.download 的返回值或文件路径。
    // 看完调用 ctx.closeFile()，或下一个浏览器操作前自动关掉。试跑（虚拟屏）时在浏览器新标签页里打开
    async openFile(file, opts = {}) {
      const abs = file && typeof file === 'object' ? file.path : path.posix.resolve(args.cwd, String(file));
      if (b.viewerOpen?.()) await b.closeFile();
      await b.openFile(abs, human, opts);
      await sleep(opts.pause ?? 2500);
    },
    closeFile: () => b.closeFile(),
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
  let stopWall = null;
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
    await releaseHeld();
    if (capture) {
      // 停止录制的墙钟时刻（秒），合成时据此补足结尾静止的画面
      stopWall = Date.now() / 1000;
      await capture.stop();
      emit('record-stop');
      // 画面采集中途退出：视频不完整，判这次录制失败（流水线会重录），不合成半截视频
      const died = capture.died?.();
      if (died && !failed) {
        failed = new Error(died);
        emit('error', { message: died });
      }
    }
    await b.close();
  }
  if (interrupted) process.exit(EXIT_USER_INPUT);
  if (failed) process.exit(1);
  if (capture) {
    await mux(capture, args.out, args.outHeight, stopWall);
    emit('muxed', { out: args.out });
  }
  emit('done', { ok: true });
  process.exit(0);
}

main().catch(e => { emit('error', { message: String(e.stack || e).slice(0, 1500) }); process.exit(1); });
