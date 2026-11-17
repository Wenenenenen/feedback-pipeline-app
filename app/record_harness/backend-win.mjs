// Windows 本机桌面后端：本机 Chrome（单独的配置目录）、Windows Terminal 里的 WSL、真实的 Windows 文件对话框，
// 由 wininput.py 用 SendInput 驱动系统指针与键盘，ffmpeg gfxcapture（Windows.Graphics.Capture，指针由系统合成，
// 与系统自带录屏一致；ddagrab 画自定义指针会带白框）录主屏，winaudio.py 录系统声音。
// 执行器在 Windows 的 node 里运行；录屏目录、录屏脚本里的路径仍是 WSL 路径。
import { execFile, spawn } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import readline from 'node:readline';
import { fileURLToPath } from 'node:url';
import { rand, sleep } from './human.mjs';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const shq = s => `'${String(s).replace(/'/g, `'\\''`)}'`;
const TERM_CLASS = 'CASCADIA_HOSTING_WINDOW_CLASS';
// 声音前移的秒数（补偿 Chrome 出声与系统回环的延迟）
const AUDIO_LEAD = 0.06;

// Windows 路径转 WSL 挂载路径，如 C:\Users\a → /mnt/c/Users/a
const toWsl = p => `/mnt/${p[0].toLowerCase()}/${p.slice(3).replace(/\\/g, '/')}`;

// 常驻的输入助手：一问一答，另外推送 user-input 事件
function startHelper(python, emit) {
  const proc = spawn(python, [path.join(HERE, 'wininput.py'), 'serve'], { stdio: ['pipe', 'pipe', 'pipe'] });
  const waiting = new Map();
  let seq = 0;
  let onInput = () => {};
  proc.stderr.on('data', d => emit('helper-stderr', { text: d.toString().slice(0, 300) }));
  readline.createInterface({ input: proc.stdout }).on('line', line => {
    let msg;
    try { msg = JSON.parse(line); } catch { return; }
    if (msg.event === 'user-input') { onInput(msg); return; }
    const w = waiting.get(msg.id);
    if (!w) return;
    waiting.delete(msg.id);
    if (msg.ok) w.resolve(msg);
    else w.reject(new Error(`输入助手 ${w.op} 失败：${msg.error}`));
  });
  proc.on('exit', code => {
    for (const w of waiting.values()) w.reject(new Error(`输入助手已退出（${code}）`));
    waiting.clear();
  });
  return {
    req(op, params = {}) {
      return new Promise((resolve, reject) => {
        const id = ++seq;
        waiting.set(id, { resolve, reject, op });
        proc.stdin.write(JSON.stringify({ id, op, ...params }) + '\n');
      });
    },
    onUserInput(cb) { onInput = cb; },
    stop() { try { proc.stdin.end(); } catch { /* 已经退出 */ } },
  };
}

function readJson(file) {
  try { return JSON.parse(fs.readFileSync(file, 'utf8')); } catch { return {}; }
}

// 用 UI 自动化在窗口里按文件名找列表项，返回 {cell, row} 屏幕矩形，找不到为 null
function uiaFind(hwnd, name) {
  return new Promise(resolve => {
    execFile('powershell.exe', ['-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', path.join(HERE, 'uia_find.ps1'),
      '-Hwnd', String(hwnd), '-Name', name], { timeout: 15000, encoding: 'utf8' }, (err, stdout) => {
      try {
        const r = JSON.parse(String(stdout).trim().split('\n').pop());
        resolve(r.found ? r : null);
      } catch {
        resolve(null);
      }
    });
  });
}

// 合并写入 Chrome 配置：关掉翻译，文件对话框默认打开暂存目录，上次正常退出（不弹恢复提示）
function patchProfile(profile, staging) {
  const file = path.join(profile, 'Default', 'Preferences');
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const p = readJson(file);
  p.translate = { ...(p.translate || {}), enabled: false };
  p.selectfile = { ...(p.selectfile || {}), last_directory: staging };
  p.download = { ...(p.download || {}), prompt_for_download: false };
  p.profile = { ...(p.profile || {}), exit_type: 'Normal', exited_cleanly: true };
  p.browser = { ...(p.browser || {}), has_seen_welcome_page: true, check_default_browser: false };
  // 不提示登录 Chrome（工具栏上不出现「登录 Chrome?」「请采取行动」）
  p.signin = { ...(p.signin || {}), allowed: false };
  p.intl = { ...(p.intl || {}), accept_languages: 'zh-CN,zh' };
  fs.writeFileSync(file, JSON.stringify(p));
  const local = path.join(profile, 'Local State');
  const ls = readJson(local);
  ls.browser = { ...(ls.browser || {}), has_seen_welcome_page: true };
  fs.writeFileSync(local, JSON.stringify(ls));
}

export async function createBackend(args, emit) {
  const helper = startHelper(args.python, emit);
  const scr = await helper.req('screen');
  const W = scr.width;
  const H = scr.height;
  const b = { W, H, name: 'win' };
  const staging = path.join(process.env.USERPROFILE || args.root, 'Downloads', path.posix.basename(args.cwd));
  let chromeProc = null;
  let browser = null;
  let page = null;
  let chromeWin = 0;
  let termWin = 0;
  let front = '';
  let prev = '';
  // 复制到暂存目录的文件，录完删掉
  const staged = new Set();

  const windows = async () => (await helper.req('windows')).windows;

  async function waitNewWindow(before, match, timeoutMs = 15000) {
    const end = Date.now() + timeoutMs;
    while (Date.now() < end) {
      const w = (await windows()).find(x => !before.has(x.hwnd) && match(x));
      if (w) return w;
      await sleep(150);
    }
    return null;
  }

  async function startTerminal() {
    const status = path.join(args.work, 'term.status');
    fs.writeFileSync(status, '');
    const rc = path.join(args.work, 'term.bashrc');
    // 正常加载 ~/.bashrc（提示符与新开 WSL 终端一致），每条命令结束把退出码追加到状态文件；不写入命令历史
    fs.writeFileSync(rc, [
      '[ -f ~/.bashrc ] && . ~/.bashrc',
      'unset HISTFILE',
      `__fb_status() { local c=$?; echo "$c" >> ${shq(toWsl(status))}; return $c; }`,
      'PROMPT_COMMAND="__fb_status${PROMPT_COMMAND:+;$PROMPT_COMMAND}"',
      `cd ${shq(args.cwd)}`,
      'clear',
      '',
    ].join('\n'));
    const before = new Set((await windows()).map(w => w.hwnd));
    spawn('wt.exe', ['-w', 'new', '--maximized', 'new-tab', 'wsl.exe', '-d', args.distro, '--cd', args.cwd, '--',
      'bash', '--rcfile', toWsl(rc), '-i'], { detached: true, stdio: 'ignore' }).unref();
    const w = await waitNewWindow(before, x => x.class === TERM_CLASS);
    if (!w) throw new Error('Windows Terminal 窗口没有出现');
    for (let i = 0; i < 100 && !fs.readFileSync(status, 'utf8').trim(); i++) await sleep(100);
    return { win: w.hwnd, status };
  }

  b.init = async ({ chromium }) => {
    // 屏保在运行就先退出（在开始监听真人操作之前，唤醒用的注入输入不算打扰）
    const wk = await helper.req('wake');
    if (wk.was_running) emit('screensaver', { stopped: !wk.running });
    if (wk.running) throw new Error('屏保没有退出');
    if (wk.locked) throw new Error('桌面已锁屏（屏保设了恢复时显示登录屏幕也会这样），锁屏时录不到画面');
    await helper.req('awake', { on: true });
    await helper.req('watch');
    fs.mkdirSync(staging, { recursive: true });
    const term = await startTerminal();
    termWin = term.win;
    b.termStatus = term.status;
    // 本机 Chrome 正常启动（只多一个调试端口，不带自动化提示），执行器连上去读页面状态
    const profile = path.join(args.root, 'chrome-profile');
    patchProfile(profile, staging);
    const port = 9300 + Math.floor(Math.random() * 500);
    const before = new Set((await windows()).map(w => w.hwnd));
    chromeProc = spawn(args.chrome, [`--user-data-dir=${profile}`, `--remote-debugging-port=${port}`,
      '--no-first-run', '--no-default-browser-check', '--start-maximized', '--window-position=0,0', 'about:blank'],
    { detached: true, stdio: 'ignore' });
    chromeProc.unref();
    for (let i = 0; i < 60 && !browser; i++) {
      await sleep(250);
      browser = await chromium.connectOverCDP(`http://127.0.0.1:${port}`).catch(() => null);
    }
    if (!browser) throw new Error('连接本机 Chrome 失败');
    const context = browser.contexts()[0];
    page = context.pages()[0] || await context.newPage();
    const w = await waitNewWindow(before, x => x.class === 'Chrome_WidgetWin_1' && x.pid === chromeProc.pid)
      || await waitNewWindow(before, x => x.class === 'Chrome_WidgetWin_1');
    if (!w) throw new Error('Chrome 窗口没有出现');
    chromeWin = w.hwnd;
    await helper.req('foreground', { hwnd: chromeWin, maximize: true });
    front = 'browser';
    prev = 'term';
    const cur = await helper.req('cursor');
    return { page, context, browser, mouse: { x: cur.x, y: cur.y } };
  };

  // 切窗口：两个窗口之间用 Alt+Tab 切（与人一样），不成再直接切到前台
  b.raise = async which => {
    const hwnd = which === 'term' ? termWin : chromeWin;
    const cur = (await helper.req('front')).hwnd;
    if (cur === hwnd) { front = which; return; }
    const other = which === 'term' ? chromeWin : termWin;
    if (cur === other && prev === which) {
      await helper.req('key', { combo: 'alt+tab', hold: rand(0.05, 0.1) });
      await sleep(350);
    }
    if ((await helper.req('front')).hwnd !== hwnd) await helper.req('foreground', { hwnd, maximize: true });
    prev = front;
    front = which;
    await sleep(rand(200, 350));
  };
  b.front = () => front;

  b.movePath = pts => (pts.length ? helper.req('path', { pts }) : Promise.resolve());
  b.button = (btn, down) => helper.req('button', { btn, down });
  b.wheel = async notches => {
    for (let i = 0; i < Math.abs(notches); i++) {
      await helper.req('wheel', { notches: Math.sign(notches) });
      await sleep(rand(70, 160));
    }
  };
  b.key = combo => helper.req('key', { combo: String(combo).trim(), hold: rand(0.05, 0.11) });
  b.typeText = (text, delays) => helper.req('text', { text: String(text), delays: delays.map(d => d / 1000) });
  b.typeInPage = b.typeText;

  // 页面视口左上角的屏幕坐标与缩放：Chrome 的网页内容子窗口
  b.viewport = async () => {
    const dpr = await page.evaluate(() => window.devicePixelRatio || 1);
    const r = (await helper.req('child_rect', { hwnd: chromeWin, class: 'Chrome_RenderWidgetHostHWND' })).rect;
    if (r) return { x: r[0], y: r[1], dpr };
    const o = await page.evaluate(() => ({ x: window.screenX + (window.outerWidth - window.innerWidth) / 2,
      y: window.screenY + window.outerHeight - window.innerHeight - (window.outerWidth - window.innerWidth) / 2 }));
    return { x: Math.max(0, o.x), y: o.y, dpr };
  };
  // 工具栏在网页内容区正上方：刷新按钮在左侧第三个，地址栏在其右。工具栏按系统 DPI 缩放，
  // 不跟网页的 devicePixelRatio（含文字缩放、页面缩放）走
  b.chromePoints = async () => {
    const v = await b.viewport();
    const k = (scr.dpi || 96) / 96;
    const y = v.y - Math.round(29 * k);
    return { reload: { x: v.x + Math.round(114 * k), y }, omnibox: { x: v.x + Math.round(440 * k), y } };
  };

  // 上传：文件先放到「下载」下的暂存目录（对话框默认打开这里），点击后在对话框的文件名框里输入文件名回车
  b.beforeUpload = async abs => {
    const name = path.posix.basename(abs);
    // /mnt/<盘符> 下的文件直接按盘符路径读，其余经 \\wsl.localhost 读；copyFile 读 WSL 共享会报 EPERM，改为读出再写入
    const m = abs.match(/^\/mnt\/([a-z])\/(.*)$/);
    const src = m ? `${m[1].toUpperCase()}:\\${m[2].replace(/\//g, '\\')}`
      : `\\\\wsl.localhost\\${args.distro}${abs.replace(/\//g, '\\')}`;
    fs.writeFileSync(path.join(staging, name), fs.readFileSync(src));
    staged.add(path.join(staging, name));
    return new Set((await windows()).map(w => w.hwnd));
  };
  b.chooseFile = async (before, abs, human) => {
    const dlg = await waitNewWindow(before, x => x.class === '#32770', 6000);
    if (!dlg) return false;
    const name = path.posix.basename(abs);
    // 对话框打开在暂存目录（Chrome 配置里的上次目录）；一边「看」对话框，一边用 UI 自动化找文件在列表里的位置
    const found = uiaFind(dlg.hwnd, name);
    await sleep(rand(600, 1000));
    const [l, t, r, bt] = dlg.rect;
    await human.moveTo(l + (r - l) * rand(0.45, 0.65), t + (bt - t) * rand(0.35, 0.55));
    await sleep(rand(300, 600));
    const pos = await found;
    if (pos) {
      // 指针移到文件名上停一下，再双击
      const [cl, ct, cr, cb] = pos.cell;
      const x = cl + Math.min(cr - cl, 160) * rand(0.25, 0.7);
      const y = ct + (cb - ct) * rand(0.35, 0.65);
      await human.moveTo(x, y);
      await sleep(rand(250, 550));
      await human.click('left', 2);
    } else {
      // 找不到列表项时退回到在文件名框里输入文件名回车（文件名框默认有焦点）
      emit('upload-fallback', { name });
      await human.type(name, 55);
      await sleep(rand(400, 700));
      await helper.req('key', { combo: 'return' });
    }
    for (let i = 0; i < 60; i++) {
      await sleep(100);
      if (!(await windows()).some(x => x.hwnd === dlg.hwnd)) break;
      if (i === 59) throw new Error(`文件对话框没有关闭，可能没在暂存目录 ${staging} 里找到 ${name}`);
    }
    front = 'browser';
    return true;
  };

  b.exec = (cmd, timeoutSec = 30) => new Promise(resolve => {
    execFile('wsl.exe', ['-d', args.distro, '--cd', args.cwd, '--', 'bash', '-lc', cmd],
      { timeout: timeoutSec * 1000, encoding: 'utf8' },
      (err, stdout, stderr) => resolve({ code: err ? (err.code ?? 1) : 0, output: String(stdout) + String(stderr) }));
  });

  const grabber = `gfxcapture=hmonitor=${scr.hmonitor}:max_framerate=30:capture_cursor=1,hwdownload,format=bgra`;
  b.grab = file => new Promise((resolve, reject) => {
    execFile(args.ffmpeg, ['-loglevel', 'error', '-y', '-f', 'lavfi', '-i', grabber, '-frames:v', '1', file],
      { timeout: 15000 }, err => (err ? reject(err) : resolve(file)));
  });

  // 录制：画面用 gfxcapture（墙钟时间戳），声音用 WASAPI 回环（报告第一帧的墙钟时刻），录完对齐合成
  b.startCapture = () => {
    const files = { video: path.join(args.work, 'video.mkv'), audio: path.join(args.work, 'audio.wav') };
    let audio = null;
    let audioWall = null;
    if (args.audio) {
      audio = spawn(args.python, [path.join(HERE, 'winaudio.py'), files.audio], { stdio: ['pipe', 'pipe', 'pipe'] });
      readline.createInterface({ input: audio.stdout }).on('line', line => {
        try {
          const m = JSON.parse(line);
          if (m.event === 'audio-start') audioWall = m.wall;
        } catch { /* 忽略非 JSON 行 */ }
      });
      audio.stderr.on('data', d => emit('audio-stderr', { text: d.toString().slice(0, 300) }));
    }
    const video = spawn(args.ffmpeg, ['-y', '-loglevel', 'error', '-use_wallclock_as_timestamps', '1', '-copyts',
      '-f', 'lavfi', '-t', String(args.maxSec), '-i', grabber,
      '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '16', '-pix_fmt', 'yuv420p', files.video],
    { stdio: ['pipe', 'ignore', 'pipe'] });
    video.stderr.on('data', d => emit('ffmpeg', { name: 'video', text: d.toString().slice(0, 300) }));
    const quit = p => new Promise(r => {
      if (!p || p.exitCode !== null) { r(); return; }
      p.on('close', r);
      setTimeout(r, 15000);
      try { p.stdin.write('q\n'); p.stdin.end(); } catch { r(); }
    });
    return {
      ffmpeg: args.ffmpeg,
      ffprobe: args.ffprobe,
      files,
      async stop() {
        await quit(video);
        await quit(audio);
      },
      async audioStart() {
        if (!audio) return null;
        for (let i = 0; i < 50 && audioWall === null; i++) await sleep(100);
        // 实测网页出声经 Chrome 与系统回环约晚 110ms：补回 60ms，留下真人录屏里也有的几十毫秒出声延迟
        return audioWall === null ? null : audioWall - AUDIO_LEAD;
      },
    };
  };

  // 真人碰了鼠标键盘时回调（其他程序的弹窗、通知照常录，不中止）
  b.onUserInput = cb => helper.onUserInput(cb);
  b.close = async () => {
    // connectOverCDP 连上的浏览器 close() 只断开连接，要发 Browser.close 才会关掉 Chrome
    if (browser) {
      const s = await browser.newBrowserCDPSession().catch(() => null);
      await s?.send('Browser.close').catch(() => {});
      await browser.close().catch(() => {});
    }
    if (chromeWin) {
      await sleep(800);
      if ((await windows().catch(() => [])).some(w => w.hwnd === chromeWin)) {
        await helper.req('close', { hwnd: chromeWin }).catch(() => {});
      }
    }
    if (termWin) await helper.req('close', { hwnd: termWin }).catch(() => {});
    // 删掉复制进「下载」的文件，暂存目录空了就一起删
    for (const f of staged) fs.rmSync(f, { force: true });
    try { fs.rmdirSync(staging); } catch { /* 目录里还有别的文件，保留 */ }
    await helper.req('awake', { on: false }).catch(() => {});
    helper.stop();
  };
  return b;
}
