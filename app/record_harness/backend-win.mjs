// Windows 本机桌面后端：本机 Chrome（单独的配置目录）、Windows Terminal 里的 WSL、真实的 Windows 文件对话框，
// 由 wininput.py 用 SendInput 驱动系统指针与键盘，ffmpeg gfxcapture（Windows.Graphics.Capture，指针由系统合成，
// 与系统自带录屏一致；ddagrab 画自定义指针会带白框）录主屏，winaudio.py 录系统声音。
// 执行器在 Windows 的 node 里运行；录屏目录、录屏脚本里的路径仍是 WSL 路径。
// 录制期间开系统勿扰（其他程序的通知不弹出），录完恢复原来的设置。
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

// 调 uia_find.ps1，返回最后一行 JSON，出错为 null
function runUia(params) {
  return new Promise(resolve => {
    execFile('powershell.exe', ['-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', path.join(HERE, 'uia_find.ps1'),
      ...params], { timeout: 15000, encoding: 'utf8' }, (err, stdout) => {
      try {
        resolve(JSON.parse(String(stdout).trim().split('\n').pop()));
      } catch {
        resolve(null);
      }
    });
  });
}

// 用 UI 自动化在窗口里按文件名找列表项，返回 {cell, row, index} 屏幕矩形，找不到为 null
async function uiaFind(hwnd, name) {
  const r = await runUia(['-Hwnd', String(hwnd), '-Name', name]);
  return r && r.found ? r : null;
}

// 一次找多个文件，返回 [{name, found, index, cell, row}]（index 是在列表里的顺序），出错为 null
async function uiaFindMany(hwnd, names) {
  const r = await runUia(['-Hwnd', String(hwnd), '-Names', names.join('|')]);
  return r && Array.isArray(r.items) ? r.items : null;
}

// 在某个进程弹出的右键菜单、子菜单里找名称以其中任一项开头的菜单项，返回屏幕矩形，找不到为 null
async function uiaMenu(pid, alts) {
  const r = await runUia(['-Kind', 'menu', '-ProcId', String(pid), '-Name', alts.join('|')]);
  return r && r.found ? r.rect : null;
}

// 矩形里靠左的随机落点（文件名一格很宽，人一般点在文字上）
const cellPoint = ([l, t, r, b]) => ({ x: l + Math.min(r - l, 160) * rand(0.25, 0.7), y: t + (b - t) * rand(0.35, 0.65) });
const rectCenter = ([l, t, r, b]) => ({ x: l + (r - l) * rand(0.35, 0.6), y: t + (b - t) * rand(0.4, 0.6) });
// 指针移到某个点（{x, y}）
const goTo = (human, p) => human.moveTo(p.x, p.y);
const isExe = (w, exe) => String(w.exe || '').toLowerCase() === exe;

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
    // 开勿扰：其他程序的通知不弹出来挡住画面（进通知中心，录完恢复原来的设置）；开不了只记一笔，照常录
    try {
      const q = await helper.req('quiet', { on: true });
      emit('quiet', { was: q.was, now: q.now });
    } catch (e) {
      emit('quiet-failed', { message: String(e.message || e).slice(0, 300) });
    }
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
    await ownDownloads();
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
  // 按住与松开（组合键按顺序按下、倒序松开）；助手退出时会松开所有还按着的键
  b.keyDown = combo => helper.req('keydown', { combo: String(combo).trim() });
  b.keyUp = combo => helper.req('keyup', { combo: String(combo).trim() });
  b.typeText = (text, delays) => helper.req('text', { text: String(text), delays: delays.map(d => d / 1000) });
  b.typeInPage = b.typeText;
  // 粘贴（超长文本用）：文本放进系统剪贴板后按 Ctrl+V（网页输入框、地址栏、Windows Terminal 都支持）
  b.pasteText = text => helper.req('paste', { text: String(text) });
  b.pasteInPage = b.pasteText;

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

  // WSL 路径或 /mnt/<盘符> 路径对应的 Windows 读取路径
  const winSource = abs => {
    const m = abs.match(/^\/mnt\/([a-z])\/(.*)$/);
    return m ? `${m[1].toUpperCase()}:\\${m[2].replace(/\//g, '\\')}`
      : `\\\\wsl.localhost\\${args.distro}${abs.replace(/\//g, '\\')}`;
  };
  // 把文件放进暂存目录（已经在里面的，如刚下载的文件，原样不动），返回暂存目录里的 Windows 路径
  const stage = abs => {
    const name = path.posix.basename(abs);
    const dst = path.join(staging, name);
    const src = winSource(abs);
    // copyFile 读 WSL 共享会报 EPERM，改为读出再写入
    // 暂存目录里已有内容相同的文件时不重写：重写会刷新修改时间，打乱对话框里按日期排的顺序
    if (path.resolve(src).toLowerCase() !== path.resolve(dst).toLowerCase()) {
      const data = fs.readFileSync(src);
      let same = false;
      try { same = fs.readFileSync(dst).equals(data); } catch { /* 还没有这个文件 */ }
      if (!same) fs.writeFileSync(dst, data);
    }
    staged.add(dst);
    return dst;
  };
  const waitClosed = async (hwnd, msg) => {
    for (let i = 0; i < 60; i++) {
      await sleep(100);
      if (!(await windows()).some(x => x.hwnd === hwnd)) return;
    }
    throw new Error(msg);
  };

  // 上传：文件先放到「下载」下的暂存目录（对话框默认打开这里），点击后在对话框的文件列表里选中
  b.beforeUpload = async absOrList => {
    for (const abs of [].concat(absOrList)) stage(abs);
    return new Set((await windows()).map(w => w.hwnd));
  };
  b.chooseFile = async (before, absOrList, human) => {
    const dlg = await waitNewWindow(before, x => x.class === '#32770', 6000);
    if (!dlg) return false;
    const names = [].concat(absOrList).map(f => path.posix.basename(f));
    // 对话框弹出后先看一眼文件列表（指针不动，不去对话框正中间绕一圈），同时用 UI 自动化找文件在列表里的位置
    const found = names.length > 1 ? uiaFindMany(dlg.hwnd, names)
      : uiaFind(dlg.hwnd, names[0]).then(r => (r ? [{ ...r, name: names[0], found: true }] : null));
    await sleep(rand(700, 1200));
    const items = await found;
    const missing = items ? items.filter(i => !i.found).map(i => i.name) : names;
    if (!missing.length && items.length === 1) {
      // 单选：指针直接移到文件名上停一下，再双击
      await goTo(human, cellPoint(items[0].cell));
      await sleep(rand(250, 550));
      await human.click('left', 2);
    } else if (!missing.length) {
      // 多选：与人一样先点第一个，相邻的按住 Shift 点最后一个连选，不相邻的按住 Ctrl 逐个点，再回车打开
      const sorted = [...items].sort((a, c) => a.index - c.index);
      const contiguous = sorted.every((it, k) => k === 0 || it.index === sorted[k - 1].index + 1);
      emit('upload-multi', { names, via: contiguous ? 'shift-click' : 'ctrl-click' });
      await goTo(human, cellPoint(sorted[0].cell));
      await sleep(rand(250, 500));
      await human.click();
      const mod = contiguous ? 'shift' : 'ctrl';
      const rest = contiguous ? [sorted[sorted.length - 1]] : sorted.slice(1);
      await sleep(rand(250, 450));
      await helper.req('keydown', { combo: mod });
      try {
        for (const it of rest) {
          await sleep(rand(200, 400));
          await goTo(human, cellPoint(it.cell));
          await sleep(rand(200, 400));
          await human.click();
        }
        await sleep(rand(150, 300));
      } finally {
        await helper.req('keyup', { combo: mod });
      }
      await sleep(rand(400, 700));
      await helper.req('key', { combo: 'return' });
    } else if (names.length > 1) {
      // 列表里找不到（如被文件类型筛掉）时退回到在文件名框里输入带引号的文件名列表回车；Alt+N 先把焦点放到文件名框
      emit('upload-fallback', { names, missing });
      await helper.req('key', { combo: 'alt+n', hold: rand(0.05, 0.1) });
      await sleep(rand(200, 400));
      await human.type(names.map(n => `"${n}"`).join(' '), 90);
      await sleep(rand(400, 700));
      await helper.req('key', { combo: 'return' });
    } else {
      // 单个文件找不到列表项时退回到在文件名框里输入文件名回车（文件名框默认有焦点）
      emit('upload-fallback', { names, missing });
      await human.type(names[0], 90);
      await sleep(rand(400, 700));
      await helper.req('key', { combo: 'return' });
    }
    await waitClosed(dlg.hwnd, `文件对话框没有关闭，可能输入框不支持多选或暂存目录 ${staging} 里缺文件：${names.join(', ')}`);
    front = 'browser';
    return true;
  };

  // 下载：Playwright 连上 Chrome 后会把下载接管成临时目录里没有扩展名的文件（下载气泡里只显示通用图标），
  // 这里改回由 Chrome 按原文件名保存到暂存目录，下载气泡与真人下载一样显示文件名和对应格式的图标
  const downloads = new Map();
  let cdp = null;
  async function ownDownloads() {
    cdp = await browser.newBrowserCDPSession();
    cdp.on('Browser.downloadWillBegin', e => downloads.set(e.guid, { name: e.suggestedFilename, state: 'inProgress' }));
    cdp.on('Browser.downloadProgress', e => {
      const d = downloads.get(e.guid);
      if (d) d.state = e.state;
    });
    await cdp.send('Browser.setDownloadBehavior', { behavior: 'allow', downloadPath: staging, eventsEnabled: true });
  }
  // trigger 触发下载（点下载按钮）；等下载完成，返回 {name, path（WSL 路径）, winPath}
  b.download = async (trigger, timeoutSec = 60) => {
    const known = new Set(downloads.keys());
    const files0 = new Set(fs.readdirSync(staging));
    const t0 = Date.now();
    await trigger();
    const end = t0 + timeoutSec * 1000;
    let d = null;
    while (Date.now() < end) {
      d = [...downloads.entries()].find(([g]) => !known.has(g))?.[1];
      if (d && d.state !== 'inProgress') break;
      await sleep(150);
    }
    if (!d) throw new Error('点击后没有开始下载');
    if (d.state !== 'completed') throw new Error(`下载没有完成（${d.state}）：${d.name}`);
    // 同名文件已存在时 Chrome 会改名（如 a (1).png）：取这次新出现的文件，没有就按建议文件名
    let name = d.name;
    for (let i = 0; i < 20; i++) {
      const fresh = fs.readdirSync(staging).filter(f => !files0.has(f) && !f.endsWith('.crdownload'));
      if (fresh.length) { name = fresh.sort((a, c) => fs.statSync(path.join(staging, c)).mtimeMs
        - fs.statSync(path.join(staging, a)).mtimeMs)[0]; break; }
      if (fs.existsSync(path.join(staging, name)) && fs.statSync(path.join(staging, name)).mtimeMs >= t0 - 1000) break;
      await sleep(100);
    }
    const winPath = path.join(staging, name);
    staged.add(winPath);
    emit('download', { name });
    return { name, winPath, path: toWsl(winPath) };
  };

  // 用系统程序打开文件（看下载的图片、文本）：资源管理器里定位到文件双击，用默认程序打开（图片是系统看图，
  // 文本是本机的默认编辑器）；app 为 'notepad' 时改为右键「打开方式 → 记事本」。
  // 默认编辑器已经开着时文件会作为标签页开在原窗口里，按标题里的文件名认出这个窗口。
  // 某一步找不到控件时直接用默认程序（或记事本）打开。opened 记下要关的窗口，reused 表示是原来就开着的窗口
  let opened = [];
  b.openFile = async (abs, human, { app } = {}) => {
    const winPath = stage(abs);
    const name = path.basename(winPath);
    const kind = app === 'notepad' ? 'notepad' : 'default';
    const before = new Set((await windows()).map(w => w.hwnd));
    spawn('explorer.exe', [`/select,"${winPath}"`], { detached: true, stdio: 'ignore', windowsVerbatimArguments: true }).unref();
    const ex = await waitNewWindow(before, x => x.class === 'CabinetWClass', 10000);
    let viewer = null;
    const notViewer = x => x.hwnd === ex?.hwnd || x.class === 'CabinetWClass' || isExe(x, 'chrome.exe') || x.class === TERM_CLASS
      || (kind === 'notepad' && !isExe(x, 'notepad.exe'));
    // 新出现的程序窗口，或原来就开着、标题里出现了这个文件名的窗口（编辑器新开了标签页）
    const findViewer = async (timeoutMs) => {
      const end = Date.now() + timeoutMs;
      while (Date.now() < end) {
        const ws = (await windows()).filter(x => !notViewer(x));
        const fresh = ws.find(x => !before.has(x.hwnd));
        if (fresh) return { ...fresh, reused: false };
        const tab = ws.find(x => before.has(x.hwnd) && String(x.title || '').includes(name));
        if (tab) return { ...tab, reused: true };
        await sleep(200);
      }
      return null;
    };
    if (ex) {
      opened.push({ hwnd: ex.hwnd, reused: false });
      await helper.req('foreground', { hwnd: ex.hwnd, maximize: true });
      front = 'explorer';
      await sleep(rand(600, 900));
      const pos = await uiaFind(ex.hwnd, name);
      if (pos) {
        await goTo(human, cellPoint(pos.cell));
        await sleep(rand(300, 600));
        if (kind === 'default') {
          await human.click('left', 2);
          viewer = await findViewer(15000);
        } else {
          await human.click('right');
          const item = await uiaMenu(ex.pid, ['打开方式', 'Open with']);
          if (item) {
            await goTo(human, rectCenter(item));
            await sleep(rand(500, 800));
            await human.click();
            const np = await uiaMenu(ex.pid, ['记事本', 'Notepad']);
            if (np) {
              await goTo(human, rectCenter(np));
              await sleep(rand(250, 500));
              await human.click();
              viewer = await findViewer(15000);
            }
          }
          if (!viewer) { await helper.req('key', { combo: 'Escape' }); await helper.req('key', { combo: 'Escape' }); }
        }
      }
    }
    if (!viewer) {
      emit('open-file-fallback', { name, kind });
      if (kind === 'default') spawn('explorer.exe', [`"${winPath}"`], { detached: true, stdio: 'ignore', windowsVerbatimArguments: true }).unref();
      else spawn('notepad.exe', [winPath], { detached: true, stdio: 'ignore' }).unref();
      viewer = await findViewer(15000);
      if (!viewer) throw new Error(`没能打开 ${name}`);
    }
    opened.push({ hwnd: viewer.hwnd, reused: viewer.reused, name });
    await helper.req('foreground', { hwnd: viewer.hwnd, maximize: !viewer.reused });
    front = 'viewer';
    emit('open-file', { name, kind, exe: viewer.exe, reused: viewer.reused });
    await sleep(rand(500, 800));
    return true;
  };
  b.viewerOpen = () => opened.length > 0;
  // 关掉打开文件时出现的窗口（先关看图或编辑器，再关资源管理器），与人一样按 Alt+F4。
  // 原来就开着的编辑器窗口不能整个关掉：标题里还是这个文件（当前标签页是它）时按 Ctrl+W 只关这个标签页，否则不动
  b.closeFile = async () => {
    for (const w of opened.reverse()) {
      const now = (await windows()).find(x => x.hwnd === w.hwnd);
      if (!now) continue;
      if (w.reused && !String(now.title || '').includes(w.name)) continue;
      await helper.req('foreground', { hwnd: w.hwnd });
      await sleep(rand(250, 450));
      await helper.req('key', { combo: w.reused ? 'ctrl+w' : 'alt+f4', hold: rand(0.05, 0.1) });
      await sleep(rand(400, 700));
      if (!w.reused && (await windows()).some(x => x.hwnd === w.hwnd)) await helper.req('close', { hwnd: w.hwnd }).catch(() => {});
    }
    opened = [];
    front = '';
  };

  // 原生下拉框：Chrome 把展开的选项列表画在一个单独的无标题弹出窗口里。点开前记下已有窗口，
  // 点开后找新出现的 Chrome 窗口，返回它的屏幕矩形（找不到为 null，由执行器改用方向键逐项选）
  b.beforePopup = async () => new Set((await helper.req('windows', { untitled: true })).windows.map(w => w.hwnd));
  b.popupRect = async (before, timeoutMs = 1500) => {
    const end = Date.now() + timeoutMs;
    while (Date.now() < end) {
      const w = (await helper.req('windows', { untitled: true })).windows.find(x => !before.has(x.hwnd)
        && x.exe === 'chrome.exe' && !x.title && x.rect[2] - x.rect[0] > 20 && x.rect[3] - x.rect[1] > 10);
      if (w) return w.rect;
      await sleep(80);
    }
    return null;
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
      // 画面时间戳是墙钟时间（-use_wallclock_as_timestamps），且只在画面变化时出帧，合成时要补结尾静止
      wallclock: true,
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
    if (opened.length) await b.closeFile().catch(() => {});
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
    await helper.req('quiet', { on: false }).catch(() => {});
    await helper.req('awake', { on: false }).catch(() => {});
    helper.stop();
  };
  return b;
}
