// Linux 虚拟屏后端：Xvfb 上的有界面 chromium 与 xterm，xdotool 驱动系统指针与键盘，ffmpeg x11grab 录制。
// 主要给 qa-record 试跑用（不打扰本机桌面），也可以在配置里选它做正式录制。
import { execFile, execFileSync, spawn } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { rand, sleep } from './human.mjs';

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
// Chromium 工具栏上的刷新按钮与地址栏（相对浏览器窗口左上角）
const CHROME_RELOAD = { x: 95, y: 63 };
const CHROME_OMNIBOX = { x: 420, y: 63 };
// 原生文件选择框摆放的大小
const DIALOG_W = 880;
const DIALOG_H = 560;

async function findWindow(args, tries = 50) {
  for (let i = 0; i < tries; i++) {
    const out = await run('xdotool', ['search', '--onlyvisible', ...args], { allowFail: true });
    const ids = out.split('\n').map(s => s.trim()).filter(Boolean);
    if (ids.length) return ids[ids.length - 1];
    await sleep(200);
  }
  throw new Error(`找不到窗口：${args.join(' ')}`);
}

async function visibleWindows() {
  const out = await run('xdotool', ['search', '--onlyvisible', '--name', '.'], { allowFail: true });
  return out.split('\n').map(s => s.trim()).filter(Boolean);
}

async function windowSize(win) {
  const out = await run('xdotool', ['getwindowgeometry', '--shell', win], { allowFail: true });
  const get = k => Number((out.match(new RegExp(`^${k}=(\\d+)`, 'm')) || [])[1] || 0);
  return { w: get('WIDTH'), h: get('HEIGHT') };
}

// 有中文 UTF-8 区域设置时让浏览器界面与原生对话框用中文
function chineseLocale() {
  try {
    const all = execFileSync('locale', ['-a'], { encoding: 'utf8' });
    return all.split('\n').find(l => /^zh_CN\.utf-?8$/i.test(l.trim()))?.trim() || '';
  } catch {
    return '';
  }
}

export async function createBackend(args, emit) {
  const { W, H } = args.size;
  // 窗口按本机桌面的工作区大小摆（去掉任务栏），试跑的页面布局与正式录制一致
  const WW = args.workSize.W;
  const WH = args.workSize.H;
  const display = process.env.DISPLAY;
  let page = null;
  let context = null;
  let term = null;
  let browserWin = null;
  let front = 'browser';
  const b = { W, H, name: 'x11' };

  async function startTerminal() {
    const status = path.join(args.work, 'term.status');
    fs.writeFileSync(status, '');
    const rc = path.join(args.work, 'term.bashrc');
    // 正常加载 ~/.bashrc（提示符与新开 WSL 终端一致），每条命令结束把退出码追加到状态文件；不写入命令历史
    fs.writeFileSync(rc, [
      '[ -f ~/.bashrc ] && . ~/.bashrc',
      'unset HISTFILE',
      `__fb_status() { local c=$?; echo "$c" >> ${shq(status)}; return $c; }`,
      'PROMPT_COMMAND="__fb_status${PROMPT_COMMAND:+;$PROMPT_COMMAND}"',
      `cd ${shq(args.cwd)}`,
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
    const title = `${user}: ${home && args.cwd.startsWith(home) ? '~' + args.cwd.slice(home.length) : args.cwd}`;
    const proc = spawn('xterm', [...res.flatMap(r => ['-xrm', r]), '-geometry', '150x40+0+0', '-T', title,
      '-e', 'bash', '--rcfile', rc, '-i'], { cwd: args.cwd, env: process.env, stdio: 'ignore' });
    const win = await findWindow(['--pid', proc.pid]);
    await xdo('windowmove', win, 0, 0);
    await xdo('windowsize', win, WW, WH);
    // 等提示符出现（rc 里 clear 之后第一次显示提示符时会写一行状态）
    for (let i = 0; i < 50 && !fs.readFileSync(status, 'utf8').trim(); i++) await sleep(100);
    return { proc, win, status };
  }

  b.init = async ({ chromium }) => {
    term = await startTerminal();
    b.termStatus = term.status;
    // Chromium 的指针主题、界面字体与原生对话框样式取自 GTK 设置：只给浏览器一份单独的配置目录
    const gtkHome = path.join(args.work, 'gtk-config');
    for (const v of ['gtk-3.0', 'gtk-4.0']) {
      fs.mkdirSync(path.join(gtkHome, v), { recursive: true });
      fs.writeFileSync(path.join(gtkHome, v, 'settings.ini'), ['[Settings]',
        `gtk-cursor-theme-name=${process.env.XCURSOR_THEME || 'default'}`,
        `gtk-cursor-theme-size=${process.env.XCURSOR_SIZE || 32}`, 'gtk-font-name=Microsoft YaHei 9',
        // 对话框自带标题栏（虚拟屏没有窗口管理器，不画系统标题栏）
        'gtk-dialogs-use-header=true', ''].join('\n'));
    }
    // 浏览器配置目录：关掉翻译，文件选择框默认打开录屏目录，界面语言中文
    const profile = path.join(args.work, 'chrome-profile');
    fs.rmSync(profile, { recursive: true, force: true });
    fs.mkdirSync(path.join(profile, 'Default'), { recursive: true });
    fs.writeFileSync(path.join(profile, 'Default', 'Preferences'), JSON.stringify({
      translate: { enabled: false }, translate_blocked_languages: ['en', 'zh-CN', 'zh'],
      selectfile: { last_directory: args.cwd }, intl: { accept_languages: 'zh-CN,zh' },
      browser: { has_seen_welcome_page: true },
    }));
    const zh = chineseLocale();
    const env = { ...process.env, XDG_CONFIG_HOME: gtkHome, LANGUAGE: 'zh_CN:zh' };
    if (zh) Object.assign(env, { LANG: zh, LC_ALL: zh });
    context = await chromium.launchPersistentContext(profile, {
      headless: false, env, viewport: null, locale: 'zh-CN', acceptDownloads: true,
      ignoreDefaultArgs: ['--enable-automation'],
      args: ['--window-position=0,0', `--window-size=${WW},${WH}`, '--no-first-run', '--no-default-browser-check',
        '--force-device-scale-factor=1', '--autoplay-policy=no-user-gesture-required', '--use-angle=swiftshader',
        '--enable-unsafe-swiftshader', '--ignore-gpu-blocklist', '--enable-unsafe-webgpu',
        '--disable-features=Translate,TranslateUI', '--lang=zh-CN'],
    });
    page = context.pages()[0] || await context.newPage();
    browserWin = await findWindow(['--class', 'chrom']);
    await xdo('mousemove', Math.round(W / 2), Math.round(H * 0.6));
    return { page, context, browser: context.browser(), mouse: { x: Math.round(W / 2), y: Math.round(H * 0.6) } };
  };

  b.raise = async which => {
    const win = which === 'term' ? term.win : browserWin;
    await xdo('windowraise', win);
    await xdo('windowfocus', win).catch(() => {});
    front = which;
    await sleep(150);
  };
  b.front = () => front;

  b.movePath = async pts => {
    const chain = [];
    for (const [x, y, ms] of pts) chain.push('mousemove', x, y, 'sleep', (ms / 1000).toFixed(3));
    if (chain.length) await xdo(...chain);
  };
  b.button = (btn, down) => xdo(down ? 'mousedown' : 'mouseup', { left: 1, middle: 2, right: 3 }[btn]);
  b.wheel = async notches => {
    for (let i = 0; i < Math.abs(notches); i++) {
      await xdo('click', notches > 0 ? 5 : 4);
      await sleep(rand(70, 160));
    }
  };
  b.key = combo => xdo('key', '--delay', Math.round(rand(60, 120)), ...String(combo).split(/\s+/).filter(Boolean));
  // 系统键盘逐字输入（终端、地址栏、文件选择框）
  b.typeText = async (text, delays) => {
    const chars = [...String(text)];
    for (let i = 0; i < chars.length; i++) {
      await xdo('type', '--delay', 0, '--', chars[i]);
      await sleep(delays[i] ?? 60);
    }
  };
  // 网页输入框：xdotool 打中文不稳，走浏览器键盘事件，画面上一样
  b.typeInPage = async (text, delays) => {
    const chars = [...String(text)];
    for (let i = 0; i < chars.length; i++) {
      await page.keyboard.type(chars[i]);
      await sleep(delays[i] ?? 60);
    }
  };

  // 页面视口左上角的屏幕坐标与缩放
  b.viewport = async () => page.evaluate(() => ({ x: window.screenX + (window.outerWidth - window.innerWidth),
    y: window.screenY + (window.outerHeight - window.innerHeight), dpr: 1 }));
  b.chromePoints = async () => ({ reload: CHROME_RELOAD, omnibox: CHROME_OMNIBOX });

  // 点击之后等原生文件选择框：摆到屏幕中间，输入路径回车
  b.beforeUpload = async () => new Set(await visibleWindows());
  b.chooseFile = async (before, abs, human) => {
    let dlg = null;
    for (let i = 0; i < 60 && !dlg; i++) {
      await sleep(100);
      for (const w of await visibleWindows()) {
        if (before.has(w) || w === browserWin || w === term.win) continue;
        const s = await windowSize(w);
        if (s.w >= 300 && s.h >= 200) { dlg = w; break; }
      }
    }
    if (!dlg) return false;
    const dx = Math.round((W - DIALOG_W) / 2);
    const dy = Math.round((H - DIALOG_H) / 2);
    await xdo('windowsize', dlg, DIALOG_W, DIALOG_H);
    await xdo('windowmove', dlg, dx, dy);
    await xdo('windowraise', dlg);
    await xdo('windowfocus', dlg).catch(() => {});
    await sleep(rand(500, 900));
    // 指针移进对话框里，像在看文件列表
    await human.moveTo(dx + DIALOG_W * rand(0.4, 0.6), dy + DIALOG_H * rand(0.35, 0.55));
    await sleep(rand(250, 500));
    // 对话框打开时在录屏目录（浏览器配置里的上次目录），录屏目录下的文件只打相对路径
    const rel = abs.startsWith(args.cwd + '/') ? abs.slice(args.cwd.length + 1) : abs;
    await xdo('key', 'ctrl+l');
    await sleep(rand(200, 400));
    await human.type(rel, 55);
    await sleep(rand(400, 700));
    // 去掉输入框自动补全选中的多余部分再回车
    await xdo('key', 'Delete');
    await xdo('key', 'Return');
    for (let i = 0; i < 60; i++) {
      await sleep(100);
      if (!(await visibleWindows()).includes(dlg)) break;
      if (i === 59) throw new Error(`文件选择框没有关闭，可能路径不对：${rel}`);
    }
    front = 'browser';
    return true;
  };

  b.exec = (cmd, timeoutSec = 30) => new Promise(resolve => {
    execFile('bash', ['-c', cmd], { cwd: args.cwd, env: process.env, timeout: timeoutSec * 1000 },
      (err, stdout, stderr) => resolve({ code: err ? (err.code ?? 1) : 0, output: String(stdout) + String(stderr) }));
  });

  b.grab = async file => {
    await run('ffmpeg', ['-loglevel', 'error', '-y', '-f', 'x11grab', '-draw_mouse', '1', '-video_size', `${W}x${H}`,
      '-i', `${display}.0`, '-frames:v', '1', file], { timeout: 15000 });
    return file;
  };

  // 录制：画面与声音分两个 ffmpeg 进程录（同一进程读 pulse 会拖住 x11grab，画面一卡一卡的），
  // 两路都保留墙钟时间戳，录完按起始时间对齐后合成
  b.startCapture = () => {
    const procs = [];
    const files = { video: path.join(args.work, 'video.mkv'), audio: path.join(args.work, 'audio.mkv') };
    const spawnFF = (ff, name) => {
      const p = spawn('ffmpeg', ff, { stdio: ['pipe', 'ignore', 'pipe'] });
      p.stderr.on('data', d => emit('ffmpeg', { name, text: d.toString().slice(0, 300) }));
      procs.push(p);
    };
    // -copyts 下输出端的 -t 按墙钟时间算会一帧都不录，时长上限放在输入端
    if (args.audio) {
      spawnFF(['-y', '-loglevel', 'error', '-copyts', '-f', 'pulse', '-t', String(args.maxSec + 10), '-i', args.audio,
        '-c:a', 'pcm_s16le', files.audio], 'audio');
    }
    // 录制时用 ultrafast 高质量，CPU 占用低、不掉帧；最终压缩在合成时做
    spawnFF(['-y', '-loglevel', 'error', '-copyts', '-thread_queue_size', '512', '-f', 'x11grab', '-draw_mouse', '1',
      '-framerate', '30', '-video_size', `${W}x${H}`, '-t', String(args.maxSec), '-i', `${display}.0`,
      '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '16', '-pix_fmt', 'yuv420p', files.video], 'video');
    return {
      ffmpeg: 'ffmpeg',
      ffprobe: 'ffprobe',
      files,
      async stop() {
        // 先停画面再停声音，保证声音覆盖整段画面
        for (const p of [...procs].reverse()) {
          if (p.exitCode === null) {
            p.stdin.write('q');
            p.stdin.end();
          }
          await new Promise(r => { if (p.exitCode !== null) r(); else { p.on('close', r); setTimeout(r, 15000); } });
        }
      },
      // 声音起始时刻（墙钟秒数），没录声音时为 null
      async audioStart(probe) {
        return args.audio && fs.existsSync(files.audio) ? probe(files.audio, 'stream=start_time') : null;
      },
    };
  };

  b.onUserInput = () => {};
  b.close = async () => {
    await context?.close().catch(() => {});
    term?.proc.kill();
  };
  return b;
}
