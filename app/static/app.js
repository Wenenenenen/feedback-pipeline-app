// 流水线管理页面：题目列表、详情操作、批量出题、配置、运行日志
'use strict';

const $ = (s, el = document) => el.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
let META = { statuses: [], domains: {} };
let current = null;      // 当前详情题目 id
let logRun = null;       // 正在查看的运行 ID
let logOffset = 0;
let logTimer = null;

// ---------- 通用 ----------
async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
    body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
  });
  if (!res.ok) {
    let msg = res.statusText;
    try { msg = (await res.json()).detail || msg; } catch (e) { /* 非 JSON 响应 */ }
    throw new Error(typeof msg === 'string' ? msg : JSON.stringify(msg));
  }
  return res.headers.get('content-type')?.includes('json') ? res.json() : res;
}

function toast(msg, err = false) {
  const t = $('#toast');
  t.textContent = msg;
  t.className = err ? 'err' : '';
  clearTimeout(t._h);
  t._h = setTimeout(() => t.classList.add('hidden'), err ? 8000 : 3000);
}

async function run(fn) {
  try { return await fn(); } catch (e) { toast(e.message, true); }
}

function copyText(text) {
  navigator.clipboard.writeText(text).then(() => toast('已复制'), () => toast('复制失败', true));
}

// 流水线主干阶段（不含「失败」），用于详情顶部的进度轨
const STAGES = ['待初始化', '待准备环境', '待做题', '待收集', '待验收', '待查验', '待录屏', '已完成'];
// 需要人工动手的阶段，用醒目颜色提示
const HUMAN_STAGES = ['待准备环境', '待做题', '待查验', '待录屏', '待返修'];
// 甲方质检之后的状态，接在主干阶段之后显示
const AFTER_STAGES = ['待返修'];

function statusClass(s) {
  if (s === '失败') return 'fail';
  if (s === '已完成') return 'ok';
  return HUMAN_STAGES.includes(s) ? 'human' : '';
}

// 时间只保留到分钟，列表里更紧凑
function shortTime(s) { return String(s ?? '').replace('T', ' ').slice(5, 16); }

function stepper(t) {
  const cur = t.status === '失败' ? t.failed_from : t.status;
  // 返修发生在已完成之后：主干全部标为完成，再追加一段
  const after = AFTER_STAGES.includes(cur);
  const idx = after ? STAGES.length : STAGES.indexOf(cur);
  const extra = after ? `<li class="${t.status === '失败' ? 'fail' : 'cur'}">${esc(cur.replace(/^待/, ''))}</li>` : '';
  return '<ol class="stepper">' + STAGES.map((s, i) => {
    const cls = i < idx ? 'done' : i === idx ? (t.status === '失败' ? 'fail' : (s === '已完成' ? 'done' : 'cur')) : '';
    return `<li class="${cls}">${esc(s.replace(/^待/, ''))}</li>`;
  }).join('') + extra + '</ol>';
}

// 统一字段行：左标签 右控件，控件占满剩余宽度
function field(label, ctl, hint = '') {
  return `<div class="field"><div class="lab">${esc(label)}${hint ? `<small>${esc(hint)}</small>` : ''}</div><div class="ctl">${ctl}</div></div>`;
}

function plain(v) {
  const s = String(v ?? '').trim();
  return `<div class="plain ${s ? '' : 'empty'}">${s ? esc(s) : '—'}</div>`;
}

// 代码块：右上角带一键复制按钮
function codeBlock(text) {
  if (!String(text ?? '').trim()) return plain('');
  return `<div class="code"><pre class="copy">${esc(text)}</pre><button type="button" class="code-copy" data-copy="${esc(text)}">复制</button></div>`;
}

// 验收自检：过程、产物、代码锚点、原分四项，旧记录只有 score_consistent
function selfCheckHtml(sc) {
  const items = [['has_process', '过程'], ['has_product', '产物'], ['has_code_anchor', '代码锚点'], ['score_consistent', '原分依据']]
    .filter(([k]) => k in sc);
  const bad = items.some(([k]) => !sc[k]);
  return `<div class="self-check ${bad ? 'bad' : ''}"><div class="chip-stack">${items.map(([k, n]) =>
    `<span class="chip ${sc[k] ? 'ok' : 'fail'}">${sc[k] ? '✓' : '✗'} ${n}</span>`).join('')}</div>
    <div style="margin-top:4px">${esc(sc.note)}</div></div>`;
}

// 次要的大段内容：默认折叠，摘要行给出概况
function fold(summary, body, open = false) {
  return `<details class="fold" ${open ? 'open' : ''}><summary>${summary}</summary><div class="fold-body">${body}</div></details>`;
}

// 检查清单较长时折叠，有不通过项时默认展开
function foldChecks(items) {
  if (!items || !items.length) return checkList(items);
  const bad = items.filter(i => !i.ok).length;
  const head = bad ? `<span class="fold-bad">${bad} 项不通过</span> 共 ${items.length} 项` : `${items.length} 项全部通过`;
  return items.length <= 3 ? checkList(items) : fold(head, checkList(items), bad > 0);
}

// 各阶段默认展开的卡片，其余卡片默认折叠
const PANEL_FOCUS = {
  '待初始化': ['info'], '待准备环境': ['info', 'materials', 'env'], '待做题': ['do'], '待收集': ['do', 'collect'],
  '待验收': ['collect'], '待查验': ['review'], '待录屏': ['review', 'record', 'inspect'], '已完成': ['record', 'inspect'],
  '待返修': ['inspect'],
};

// 卡片展开状态按「题目 + 状态」记在本地，刷新详情后保持
function panelKey(t, key) { return `panel:${t.id}:${t.status}:${key}`; }

function panelOpen(t, key) {
  let saved = null;
  try { saved = localStorage.getItem(panelKey(t, key)); } catch (e) { /* 本地存储不可用时按默认处理 */ }
  if (saved !== null) return saved === '1';
  const focus = PANEL_FOCUS[t.status === '失败' ? t.failed_from : t.status] || [];
  return focus.includes(key);
}

function panel(t, key, title, body, meta = '') {
  return `<details class="panel" data-panel="${key}" ${panelOpen(t, key) ? 'open' : ''}>
    <summary><h3>${esc(title)}${meta ? ` <span class="muted">${meta}</span>` : ''}</h3></summary>
    <div class="panel-body">${body}</div></details>`;
}

function link(url, empty = '未提交') {
  return url ? `<div class="plain"><a href="${esc(url)}" target="_blank">${esc(url)}</a></div>` : `<div class="plain empty">${esc(empty)}</div>`;
}

// ---------- 标签页 ----------
document.querySelectorAll('.tab').forEach(b => b.onclick = () => {
  document.querySelectorAll('.tab').forEach(x => x.classList.toggle('active', x === b));
  document.querySelectorAll('.tab-body').forEach(x => x.classList.toggle('hidden', x.id !== 'tab-' + b.dataset.tab));
  if (b.dataset.tab === 'gen') loadGenRuns();
  if (b.dataset.tab === 'config') loadConfig();
});

// ---------- 列表 ----------
async function loadList() {
  const st = $('#status-filter').value;
  const rows = await api('/api/tasks' + (st ? '?status=' + encodeURIComponent(st) : ''));
  const checked = new Set([...document.querySelectorAll('.row-check:checked')].map(x => +x.value));
  $('#task-count').textContent = `${rows.length} 题`;
  $('#task-table tbody').innerHTML = rows.map(t => `
    <tr data-id="${t.id}" class="${t.id === current ? 'sel' : ''}">
      <td><input type="checkbox" class="row-check" value="${t.id}" ${checked.has(t.id) ? 'checked' : ''}></td>
      <td class="t-seq">${t.seq ? '#' + esc(t.seq) : '—'}</td>
      <td><div class="t-branch">${esc(t.branch)}</div><div class="t-sub">${esc(t.subdomain)}</div></td>
      <td>${esc(t.difficulty)}</td>
      <td><div class="chip-stack"><span class="chip ${statusClass(t.status)}">${esc(t.status)}</span>
          ${t.busy ? '<span class="chip busy">运行中</span>' : ''}
          ${t.due_flag ? `<span class="chip due">${esc(t.due_flag)}</span>` : ''}</div></td>
      <td class="t-time">${esc(shortTime(t.update_time))}${t.done_time ? `<br>完成 ${esc(shortTime(t.done_time))}` : ''}</td>
    </tr>`).join('') || '<tr><td colspan="6" class="muted" style="text-align:center;padding:28px">暂无题目</td></tr>';
  document.querySelectorAll('#task-table tbody tr[data-id]').forEach(tr => tr.onclick = e => {
    if (e.target.classList.contains('row-check')) return;
    showTask(+tr.dataset.id);
  });
}
$('#refresh').onclick = () => run(loadList);
$('#status-filter').onchange = () => run(loadList);
$('#check-all').onchange = e => document.querySelectorAll('.row-check').forEach(x => x.checked = e.target.checked);

// ---------- 列表折叠 ----------
function setListCollapsed(on) {
  $('#tab-tasks').classList.toggle('list-collapsed', on);
  $('#list-toggle').textContent = on ? '»' : '«';
  $('#list-toggle').title = on ? '展开题目列表' : '向左折叠题目列表';
  try { localStorage.setItem('list-collapsed', on ? '1' : '0'); } catch (e) { /* 本地存储不可用时忽略 */ }
}
$('#list-toggle').onclick = () => setListCollapsed(!$('#tab-tasks').classList.contains('list-collapsed'));
try { setListCollapsed(localStorage.getItem('list-collapsed') === '1'); } catch (e) { setListCollapsed(false); }

// ---------- 提交表 ----------
// 粘贴到表格时，含换行、制表符或引号的单元格要用引号包起来
function tsvCell(v) {
  const s = String(v ?? '');
  return /[\t\n\r"]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
}

async function openSubmission() {
  const ids = [...document.querySelectorAll('.row-check:checked')].map(x => +x.value);
  const data = await api('/api/submission', { method: 'POST', body: { ids } });
  $('#sub-scope').textContent = ids.length ? `勾选的 ${data.rows.length} 题` : `全部已完成的 ${data.rows.length} 题`;
  // 每行可折叠：只有一题时默认展开，多题时默认折叠
  const openRow = data.rows.length <= 1;
  $('#sub-body').innerHTML = data.rows.map((r, ri) => {
    // 返修过的题：标题行标出轮次，最近一轮改过的格高亮，悬停显示原值
    const changed = r.rework?.changed || {};
    const nChanged = Object.keys(changed).length;
    const reworkChip = r.rework ? `<span class="chip human">第${esc(r.rework.round)}轮返修${r.rework.state === '已返修' ? '' : ' ' + esc(r.rework.state)}</span>
        <span class="muted">${nChanged ? `改了 ${nChanged} 格` : '提交表没有要改的格'}</span>` : '';
    return `
    <details class="sub-row" ${openRow ? 'open' : ''}>
      <summary class="sub-row-head">
        <span class="seq">${r.cells[0].value ? '#' + esc(r.cells[0].value) : '未编号'}</span>
        <b class="mono">${esc(r.branch)}</b><span class="chip ${statusClass(r.status)}">${esc(r.status)}</span>
        ${reworkChip}
        <span class="spacer"></span>
        ${nChanged ? `<button type="button" data-changed-copy="${ri}" title="每行一格 列名与新值以制表符分隔">复制改动格</button>` : ''}
        <button type="button" data-row-copy="${ri}">复制整行</button>
      </summary>
      <div class="sub-grid">${r.cells.map(c => {
        const isChanged = c.name in changed;
        const tip = isChanged ? `返修改过 原值：${changed[c.name] || '空'}\n点击复制新值` : '点击复制';
        return `
        <div class="sub-cell ${c.wide ? 'wide' : ''} ${c.value ? '' : 'empty'} ${isChanged ? 'changed' : ''}" data-cell="${esc(c.value)}" title="${esc(tip)}">
          <div class="sub-name">${esc(c.name)}${isChanged ? '<span class="changed-tag">已改</span>' : ''}</div>
          <div class="sub-val">${c.value ? esc(c.value) : '空'}</div>
        </div>`;
      }).join('')}</div>
    </details>`;
  }).join('') || '<div class="empty-state"><div><b>没有可展示的题目</b>勾选左侧题目，或等题目标记完成后再打开</div></div>';
  const B = $('#sub-body');
  B.querySelectorAll('[data-cell]').forEach(el => el.onclick = () => {
    copyText(el.dataset.cell);
    el.classList.add('copied');
    setTimeout(() => el.classList.remove('copied'), 900);
  });
  // 复制按钮在折叠标题行里，点击时不触发展开或折叠
  B.querySelectorAll('[data-row-copy]').forEach(b => b.onclick = e => {
    e.preventDefault();
    e.stopPropagation();
    copyText(data.rows[+b.dataset.rowCopy].cells.map(c => tsvCell(c.value)).join('\t'));
  });
  // 只复制最近一轮返修改过的格：每行「列名 制表符 新值」，按提交表列顺序
  B.querySelectorAll('[data-changed-copy]').forEach(b => b.onclick = e => {
    e.preventDefault();
    e.stopPropagation();
    const r = data.rows[+b.dataset.changedCopy];
    const changed = r.rework?.changed || {};
    copyText(r.cells.filter(c => c.name in changed).map(c => `${c.name}\t${tsvCell(c.value)}`).join('\n'));
  });
  $('#sub-expand').onclick = () => B.querySelectorAll('details.sub-row').forEach(d => { d.open = true; });
  $('#sub-collapse').onclick = () => B.querySelectorAll('details.sub-row').forEach(d => { d.open = false; });
  $('#sub-copy-head').onclick = () => copyText(data.columns.join('\t'));
  $('#sub-modal').classList.remove('hidden');
  document.body.classList.add('modal-open');
}

function closeSubmission() {
  $('#sub-modal').classList.add('hidden');
  document.body.classList.remove('modal-open');
}
$('#submission').onclick = () => run(openSubmission);
$('#sub-close').onclick = closeSubmission;
$('#sub-modal').onclick = e => { if (e.target.id === 'sub-modal') closeSubmission(); };
document.addEventListener('keydown', e => { if (e.key === 'Escape' && !$('#sub-modal').classList.contains('hidden')) closeSubmission(); });

// ---------- 录屏文件 ----------
async function loadRecordings(t) {
  const sel = $('#f-recording_link');
  const info = $('#rec-info');
  if (!sel) return;
  let files = [];
  try {
    files = await api('/api/recordings?limit=5');
  } catch (e) {
    info.innerHTML = `<div class="issues">${esc(e.message)}</div>`;
    return;
  }
  const keep = t.recording_link || '';
  const opts = files.map(f => f.path);
  if (keep && !opts.includes(keep)) opts.unshift(keep);
  sel.innerHTML = opts.length ? opts.map(p => `<option ${p === (keep || files[0]?.path) ? 'selected' : ''}>${esc(p)}</option>`).join('')
    : '<option value="">录屏目录里没有 mp4</option>';
  const show = () => {
    const f = files.find(x => x.path === sel.value);
    if (!f) { info.innerHTML = sel.value ? '<span class="muted">已保存的录屏文件 不在最新 5 个里</span>' : ''; return; }
    const warn = [];
    if (f.duration && f.duration > 90) warn.push(`时长 ${f.duration} 秒 超过 90 秒`);
    if (f.height && f.height !== 720) warn.push(`分辨率 ${f.width}x${f.height} 不是 720p`);
    if (files[0] && f.path !== files[0].path) warn.push('不是最新的录屏');
    info.innerHTML = `<div class="rec-meta"><span>修改于 ${esc(f.mtime)}</span><span>${f.size_mb} MB</span>
      <span>时长 ${f.duration ?? '未知'} 秒</span><span>${f.width && f.height ? `${f.width}x${f.height}` : '分辨率未知'}</span></div>
      ${warn.length ? `<div class="issues">${esc(warn.join(' '))}</div>` : '<span class="chip ok">时长与分辨率符合要求 请核对是否本题的录屏</span>'}`;
  };
  sel.onchange = show;
  show();
}

// ---------- 详情 ----------
function checkList(items) {
  if (!items || !items.length) return '<div class="plain empty">尚未检查</div>';
  return '<ul class="check-list">' + items.map(i =>
    `<li class="${i.ok ? 'good' : 'bad'}"><div>${esc(i.name)} <span class="muted">${esc(i.detail)}</span>${!i.ok && i.fix ? `<span class="fix">处理 ${esc(i.fix)}</span>` : ''}</div></li>`).join('') + '</ul>';
}

function issuesHtml(list) {
  if (!list || !list.length) return '<span class="badge ok">口吻检查通过</span>';
  return '<div class="issues">' + list.slice(0, 30).map(i => `${esc(i.rule)} 「${esc(i.excerpt)}」`).join('<br>') +
    (list.length > 30 ? `<br>共 ${list.length} 处` : '') + '</div>';
}

// ---------- 质检与返修 ----------
// 每轮质检卡片的展开状态记在本地：默认只展开最近一轮
function inspKey(t, i) { return `insp:${t.id}:${i.round}`; }

function inspOpen(t, i, isLast) {
  let saved = null;
  try { saved = localStorage.getItem(inspKey(t, i)); } catch (e) { /* 本地存储不可用时按默认处理 */ }
  return saved !== null ? saved === '1' : isLast;
}

// 这一轮在提交表里要改的列：只列列名，新旧内容到提交表弹窗里看
function changedColumnsHtml(i, isLast, S) {
  if (i.state === '返修中' && !i.summary) return '<div class="plain empty">AI 处理完后列出</div>';
  const cols = i.changed_columns || [];
  const recordHint = isLast && S === '待录屏' ? '<div class="muted hint">运行录屏一格要等重新录屏并标记完成后才会更新</div>' : '';
  return (cols.length ? `<div class="chip-stack">${cols.map(c => `<span class="chip human">${esc(c)}</span>`).join('')}</div>`
    : '<div class="plain empty">提交表没有要改的格</div>') +
    '<div class="muted hint">新旧内容在提交表弹窗里看 改过的格会高亮</div>' + recordHint;
}

function inspectPanel(t) {
  const S = t.status;
  const list = t.inspections || [];
  if (!list.length && S !== '已完成') return '';
  const last = list[list.length - 1];
  const rows = list.map(i => {
    const isLast = i === last;
    const cols = i.changed_columns || [];
    return `<details class="insp" data-insp="${esc(i.round)}" ${inspOpen(t, i, isLast) ? 'open' : ''}>
      <summary class="insp-head"><b>第${esc(i.round)}轮</b><span class="chip ${i.state === '已返修' ? 'ok' : 'busy'}">${esc(i.state)}</span>
        <span class="muted">${esc(i.inspect_time || '')}</span>
        ${cols.length ? `<span class="muted">改了 ${cols.length} 格</span>` : ''}</summary>
      <div class="insp-body">
        <div class="fields">${field('提交表要改的格', changedColumnsHtml(i, isLast, S))}</div>
        ${fold('质检原文', `<pre class="copy">${esc(i.raw_text)}</pre>`)}
        ${i.summary ? fold('AI 处理说明', `<pre class="copy">${esc(i.summary)}</pre>`) : ''}
        ${!i.summary && (i.rework_items || []).length ? fold('AI 处理说明', `<pre class="copy">${esc(i.rework_items.map(x =>
          `${x.handling} ${x.problem}\n${x.detail}`).join('\n\n'))}</pre>`) : ''}
      </div>
    </details>`;
  }).join('');
  let body = rows ? `<div class="insp-list">${rows}</div>` : '';
  if (S === '已完成') {
    body += `<div class="fields">${field('质检原文', `<textarea id="f-inspect_text" rows="6" placeholder="粘贴一条质检原文 开头为【AI质检｜待返修】"></textarea>
      <div class="muted hint">AI 按质检原文直接处理 改写 Feedback 等内容 需要时修改产物并推送新的产物快照 处理完列出提交表要改的格 质检不通过的数据直接放弃 不用粘贴</div>
      <div><button data-act="rework" class="primary">AI 返修</button></div>`)}</div>`;
  }
  if (S === '待返修') body += '<div class="muted hint">AI 正在按质检原文处理 完成后回到已完成 或在需要重新录屏时回到待录屏</div>';
  const meta = S === '待返修' ? `第${esc(last.round)}轮返修中` : list.length ? `${list.length} 次质检` : '';
  return panel(t, 'inspect', '质检与返修', body, meta);
}

function btn(label, action, cls = '', extra = '') {
  return `<button data-act="${action}" class="${cls}" ${extra}>${label}</button>`;
}

async function showTask(id) {
  current = id;
  const t = await api('/api/tasks/' + id);
  document.querySelectorAll('#task-table tbody tr').forEach(tr => tr.classList.toggle('sel', +tr.dataset.id === id));
  const S = t.status;
  const editableInfo = ['待初始化', '待准备环境'].includes(S);
  const actions = [];
  if (S === '待初始化') actions.push(btn('初始化做题目录', 'init', 'primary'));
  if (S === '待准备环境') actions.push(btn('自动下载素材', 'fetch_materials'), btn('执行环境检查', 'envcheck'),
    btn('确认显卡能力', 'confirm_gpu'), btn('完成准备并提交初始快照', 'finalize_env', 'primary'));
  if (S === '待做题') actions.push(btn('做题完成', 'mark_done', 'primary'), btn('重做', 'redo', 'danger'));
  if (S === '待收集') actions.push(btn('开始收集', 'collect', 'primary'), btn('重做', 'redo', 'danger'));
  if (S === '待验收') actions.push(btn('开始验收', 'review', 'primary'));
  if (S === '待查验') actions.push(btn('保存修改', 'save-review'), btn('通过', 'approve', 'primary'), btn('打回重新生成', 'reject', 'danger'));
  if (S === '待录屏') actions.push(btn('准备录屏环境', 'prepare_record'), btn('标记完成', 'complete', 'primary'));
  if (S === '失败') {
    actions.push(btn('重试', 'retry', 'primary'));
    if (['待做题', '待收集'].includes(t.failed_from)) actions.push(btn('重做', 'redo', 'danger'));
  }
  actions.push(btn('删除题目', 'delete', 'danger push'));

  const mats = (t.materials || []).map((m, i) => `<tr>
      <td><span class="mono">${esc(m.path)}</span><br><span class="muted">${esc(m.purpose)}</span></td>
      <td>${esc(m.source)}${m.url ? `<br><a href="${esc(m.url)}" target="_blank">${esc(m.url)}</a>` : ''}</td>
      <td>${esc(m.state)} <span class="muted">${esc(m.note || '')}</span><br><span class="muted">${esc(m.license || '')}</span></td>
      <td>${m.script ? fold('查看脚本', codeBlock(m.script)) : ''}
        <div class="chip-stack">${S === '待准备环境' && m.script ? `<button data-act="run_script" data-idx="${i}">执行脚本</button>` : ''}
        ${S === '待准备环境' ? `<button data-act="confirm_material" data-idx="${i}">确认已就位</button>` : ''}</div></td></tr>`).join('');

  const summary = t.traj_summary;
  const reviewEditable = S === '待查验';
  const ro = on => on ? '' : 'readonly';
  const evidence = (t.evidence || []).map(e => `<tr><td>${esc(e.phenomenon)}</td><td>${codeBlock(e.command)}</td>
      <td>${esc(e.output_excerpt)}</td><td>${e.recordable ? '可录' : '不可录'}${e.needs_recording_confirm ? '<br><span class="chip human">需录屏确认</span>' : ''}</td></tr>`).join('');
  const steps = (t.record_steps || []).map(s => `<li><div>${esc(s.action)}${codeBlock(s.command)}<span class="muted">预期 ${esc(s.expect)}</span></div></li>`).join('');
  const levels = META.repro_levels || [];
  const repro = t.reproducibility ?? '';
  const reproKnown = levels.includes(repro);
  const evidenceCount = (t.evidence || []).length;
  const inspectHtml = inspectPanel(t);

  $('#detail').innerHTML = `
    <div class="detail-head">
      <div class="title-row">
        <span class="seq">${t.seq ? '#' + esc(t.seq) : '未编号'}</span>
        <h2>${esc(t.branch)}</h2>
        <span class="chip ${statusClass(S)}">${esc(S)}</span>
        ${t.due_flag ? `<span class="chip due">${esc(t.due_flag)}</span>` : ''}${t.busy ? '<span class="chip busy">运行中</span>' : ''}
      </div>
      <div class="meta-line">${esc(t.domain)} / ${esc(t.subdomain)} · ${esc(t.difficulty)} · ${esc(t.lang_framework || '技术栈未定')}</div>
      ${stepper(t)}
      ${t.last_error ? `<div class="error-box">${esc(t.last_error)}</div>` : ''}
      ${t.env_blockers?.length ? `<div class="error-box">尚不能提交初始快照：\n${esc(t.env_blockers.join('\n'))}</div>` : ''}
      <div class="actions">${actions.join('')}
        <span class="fold-all"><button type="button" class="ghost" data-fold-all="1">全部展开</button><button type="button" class="ghost" data-fold-all="0">全部折叠</button></span></div>
    </div>

    ${inspectHtml}

    ${panel(t, 'info', '题目信息', `<div class="fields">
      ${field('User Prompt', `${issuesHtml(t.prompt_issues).replace('口吻检查通过', '无 AI 化或刻意口语表达')}
        <textarea id="f-user_prompt" rows="9" ${ro(editableInfo)}>${esc(t.user_prompt)}</textarea>`)}
      ${field('所属领域', plain(`${t.domain} / ${t.subdomain}`))}
      ${field('任务类型', plain(t.task_type))}
      ${field('任务难度', `<select id="f-difficulty" ${editableInfo ? '' : 'disabled'}><option ${t.difficulty === '困难' ? 'selected' : ''}>困难</option><option ${t.difficulty === '地狱' ? 'selected' : ''}>地狱</option></select>`)}
      ${field('语言/框架', `<input id="f-lang_framework" value="${esc(t.lang_framework)}" ${ro(editableInfo)}>`)}
      ${field('差异说明', plain(t.similarity_note))}
      ${field('内部验收要点', fold(`${(t.acceptance_points || []).length} 条 只给验收环节使用`,
        `<textarea id="f-acceptance_points" rows="10" ${ro(editableInfo)}>${esc((t.acceptance_points || []).join('\n'))}</textarea>`, editableInfo), '每行一条')}
      ${field('环境需求', fold('工具链 显卡 外网地址',
        `<textarea id="f-env_requirements" rows="10" class="mono" ${ro(editableInfo)}>${esc(JSON.stringify(t.env_requirements || {}, null, 2))}</textarea>`, editableInfo), 'JSON')}
      ${editableInfo ? field('', '<div><button data-act="save-info">保存题目信息</button></div>') : ''}
    </div>`, `${esc(t.difficulty)}`)}

    ${panel(t, 'materials', '素材', `
      <div class="table-wrap"><table><colgroup><col style="width:32%"><col style="width:24%"><col style="width:18%"><col></colgroup>
        <thead><tr><th>路径</th><th>来源</th><th>状态</th><th>操作</th></tr></thead>
        <tbody>${mats || '<tr><td colspan="4" class="muted">无素材</td></tr>'}</tbody></table></div>`, `${(t.materials || []).length} 项`)}

    ${panel(t, 'env', '环境检查', `<div class="fields">
      ${field('工具链与显卡', foldChecks(t.env_check))}
      ${field('外网连通性', foldChecks(t.net_check))}
      ${field('做题客户端', foldChecks(t.client_check?.items), 'WSL ~/.claude')}
    </div>`)}

    ${panel(t, 'do', '做题', `<div class="fields">
      ${field('做题命令', codeBlock(t.do_command))}
      ${field('Prompt', '<div><button data-copy-prompt="1">复制 prompt 原文</button></div>')}
      ${field('初始快照', link(t.init_permalink))}
      ${field('重做次数', plain(t.redo_count))}
    </div>`)}

    ${panel(t, 'collect', '收集结果', `<div class="fields">
      ${field('SessionID', plain(t.session_id))}
      ${field('TurnID/PromptID', plain(t.prompt_id))}
      ${field('Harness', plain(`${t.harness ?? ''} ${t.harness_version ?? ''}`))}
      ${field('操作系统', plain(t.os))}
      ${field('模型', plain(t.model_name))}
      ${field('继续次数', plain(t.continue_count))}
      ${field('完成时间', plain(t.done_time))}
      ${field('产物快照', link(t.artifact_permalink, '未收集'))}
      ${field('待上传轨迹', codeBlock(t.trajectory_path))}
      ${summary ? field('轨迹摘要', fold(`${summary.steps.length} 步 报错 ${summary.tool_errors} 次 改动文件 ${summary.files_changed.length} 个`,
        `<pre class="copy">${esc(summary.steps.map(s => s.kind === 'text' ? `#${s.n} 文字 ${s.text}` : `#${s.n} ${s.tool}${s.error ? ' 报错' : ''} ${s.input}${s.output ? '\n    ' + s.output : ''}`).join('\n'))}</pre>
        <div class="sub-title">最终回复</div><pre class="copy">${esc(summary.final_text)}</pre>`)) : ''}
    </div>`, t.session_id ? '已收集' : '')}

    ${panel(t, 'review', '验收与 Feedback', `<div class="fields">
      ${field('交付完整性', `<input id="f-delivery_score" type="number" min="1" max="5" value="${esc(t.delivery_score ?? '')}" ${ro(reviewEditable)}>`, '1 到 5')}
      ${field('评分理由', `<textarea id="f-score_reason" rows="4" ${ro(reviewEditable)}>${esc(t.score_reason ?? '')}</textarea>`)}
      ${field('Feedback', `${t.feedback ? issuesHtml(t.feedback_issues) : ''}<textarea id="f-feedback" rows="10" ${ro(reviewEditable)}>${esc(t.feedback ?? '')}</textarea>`)}
      ${field('环境可复现等级', `<select id="f-reproducibility" ${reviewEditable ? '' : 'disabled'}>
          <option value="" ${reproKnown ? '' : 'selected'}>请选择</option>
          ${levels.map(l => `<option ${l === repro ? 'selected' : ''}>${esc(l)}</option>`).join('')}</select>
        ${repro && !reproKnown ? `<div class="muted">验收原建议 ${esc(repro)}</div>` : ''}`)}
      ${field('备注', `<textarea id="f-remark" rows="3" ${ro(reviewEditable || editableInfo)}>${esc(t.remark ?? '')}</textarea>`)}
      ${t.review_self_check ? field('自检', selfCheckHtml(t.review_self_check)) : ''}
      ${S === '待查验' ? field('打回意见', '<textarea id="f-reject" rows="3" placeholder="打回时必填"></textarea>') : ''}      ${field('证据清单', `<div class="muted hint">验收时每核对一项记一条 现象 执行的命令 输出摘录 用来核对 Feedback 里每个结论都有实测依据 也是挑选录屏内容的来源 不导出到提交表</div>
        ${evidenceCount ? fold(`${evidenceCount} 条 其中 ${(t.evidence || []).filter(e => e.needs_recording_confirm).length} 条需录屏确认`,
          `<div class="table-wrap"><table class="evidence"><colgroup><col style="width:26%"><col style="width:34%"><col><col style="width:96px"></colgroup>
          <thead><tr><th>现象</th><th>命令</th><th>输出摘录</th><th>录屏</th></tr></thead><tbody>${evidence}</tbody></table></div>`) : plain('')}`)}
    </div>`, t.delivery_score ? `评分 ${esc(t.delivery_score)}` : '')}

    ${panel(t, 'record', '录屏', `<div class="fields">
      ${field('录屏目录', `${codeBlock('cd ' + t.record_dir)}<div class="muted hint">待录屏阶段点「准备录屏环境」后 产物快照会全新克隆到这里 录屏步骤的命令都在这个目录下执行</div>`)}
      ${field('录屏步骤', steps ? `<ol class="steps">${steps}</ol>` : plain(''))}
      ${field('录屏文件', S === '待录屏'
        ? `<div class="inline"><select id="f-recording_link">${t.recording_link ? `<option selected>${esc(t.recording_link)}</option>` : '<option value="">读取录屏目录中</option>'}</select>
            <button type="button" data-rec-refresh="1">重新读取</button></div><div id="rec-info" class="rec-info"></div>`
        : codeBlock(t.recording_link), '默认取最新的 mp4')}
      ${field('轨迹文件', t.trajectory_path ? `${codeBlock(t.trajectory_path)}<div class="muted hint">即收集结果中的待上传轨迹 提交表的轨迹文件列直接取它</div>` : plain(''))}
    </div>`, (t.record_steps || []).length ? `${t.record_steps.length} 步` : '')}

    ${panel(t, 'runs', '运行记录', `
      <div class="table-wrap"><table><colgroup><col style="width:16%"><col style="width:12%"><col style="width:22%"><col><col style="width:80px"></colgroup>
        <tbody>${(t.runs || []).map(r => `<tr><td class="mono">${esc(r.kind)}</td><td><span class="chip ${r.status === 'success' ? 'ok' : r.status === 'running' ? 'busy' : 'fail'}">${esc(r.status)}</span></td>
          <td class="t-time">${esc(r.started_at)}</td><td>${esc(r.error || '')}</td><td><button class="ghost" data-log="${esc(r.run_key)}">日志</button></td></tr>`).join('') || '<tr><td class="muted">无</td></tr>'}</tbody></table></div>`,
      `${(t.runs || []).length} 次`)}`;

  const D = $('#detail');
  // 记住每张卡片的展开状态
  D.querySelectorAll('details.panel').forEach(d => d.addEventListener('toggle', () => {
    try { localStorage.setItem(panelKey(t, d.dataset.panel), d.open ? '1' : '0'); } catch (e) { /* 本地存储不可用时忽略 */ }
  }));
  // 待录屏时自动读取录屏目录，默认选中最新的 mp4，由人工核验
  if (S === '待录屏') {
    const loadRec = () => run(() => loadRecordings(t));
    D.querySelector('[data-rec-refresh]').onclick = loadRec;
    loadRec();
  }
  D.querySelectorAll('[data-fold-all]').forEach(b => b.onclick = () => {
    D.querySelectorAll('details.panel').forEach(d => { d.open = b.dataset.foldAll === '1'; });
  });
  D.querySelectorAll('[data-copy]').forEach(b => b.onclick = e => { e.preventDefault(); copyText(b.dataset.copy); });
  D.querySelectorAll('[data-copy-prompt]').forEach(b => b.onclick = () => copyText(t.user_prompt));
  // 记住每轮质检卡片的展开状态
  D.querySelectorAll('details.insp').forEach(d => d.addEventListener('toggle', () => {
    try { localStorage.setItem(`insp:${t.id}:${d.dataset.insp}`, d.open ? '1' : '0'); } catch (e) { /* 本地存储不可用时忽略 */ }
  }));
  D.querySelectorAll('[data-log]').forEach(b => b.onclick = () => openLog(b.dataset.log, b.dataset.log));
  D.querySelectorAll('[data-act]').forEach(b => b.onclick = () => run(() => doAction(t, b.dataset.act, b.dataset.idx)));
}

function val(id) { return $('#f-' + id)?.value; }

async function doAction(t, act, idx) {
  const id = t.id;
  if (act === 'save-info') {
    let env;
    try { env = JSON.parse(val('env_requirements') || '{}'); } catch (e) { throw new Error('环境需求不是合法 JSON'); }
    await api('/api/tasks/' + id, { method: 'PUT', body: {
      user_prompt: val('user_prompt'), difficulty: val('difficulty'), lang_framework: val('lang_framework'),
      acceptance_points: val('acceptance_points').split('\n').map(s => s.trim()).filter(Boolean),
      env_requirements: env, remark: val('remark') } });
    toast('已保存');
    return showTask(id);
  }
  if (act === 'save-review') {
    await saveReview(id);
    toast('已保存');
    return showTask(id);
  }
  if (act === 'delete') {
    if (!confirm(`确定删除题目 ${t.branch}？做题目录与远端分支不会被删除`)) return;
    await api('/api/tasks/' + id, { method: 'DELETE' });
    current = null;
    $('#detail').innerHTML = '<div class="empty-state"><div><b>已删除</b>做题目录与远端分支仍保留</div></div>';
    return loadList();
  }
  if (act === 'redo' && !confirm('重做会作废当前会话，并把做题目录恢复到初始快照（未提交的改动会丢失），确定？')) return;
  const body = {};
  if (idx !== undefined) body.idx = +idx;
  if (act === 'approve') await saveReview(id);
  if (act === 'reject') body.comment = val('reject');
  if (act === 'rework') {
    body.text = val('inspect_text') || '';
    if (!body.text.trim()) throw new Error('请粘贴质检原文');
  }
  if (act === 'collect' || (act === 'retry' && t.failed_from === '待收集')) {
    // 产物提交信息：feat: 模型产物 - <概述>
    const s = prompt('产物概述（用于提交信息 feat: 模型产物 - <概述>）', t.branch);
    if (s === null) return;
    body.summary = s;
  }
  if (act === 'complete') {
    body.recording_link = val('recording_link');
    if (!body.recording_link) throw new Error('请先选定录屏文件');
    if (!confirm(`确认录屏文件：\n${body.recording_link}\n\n标记完成后会分配提交表序号`)) return;
  }
  const r = await api(`/api/tasks/${id}/action/${act}`, { method: 'POST', body });
  if (r.run_key) openLog(r.run_key, `${t.branch} ${act}`);
  else toast('完成');
  await loadList();
  await showTask(id);
}

async function saveReview(id) {
  await api('/api/tasks/' + id, { method: 'PUT', body: {
    delivery_score: val('delivery_score'), score_reason: val('score_reason'), feedback: val('feedback'),
    reproducibility: val('reproducibility'), remark: val('remark') } });
}

// ---------- 运行日志 ----------
function openLog(runKey, title) {
  logRun = runKey;
  logOffset = 0;
  $('#log-title').textContent = title;
  $('#log-body').textContent = '';
  $('#log-panel').classList.remove('hidden');
  // 日志抽屉打开时给两侧面板让出高度
  document.body.classList.add('log-open');
  clearInterval(logTimer);
  pollLog();
  logTimer = setInterval(pollLog, 1500);
}

async function pollLog() {
  if (!logRun) return;
  const key = logRun;
  const r = await api(`/api/runs/${key}/log?offset=${logOffset}`).catch(() => null);
  if (!r || key !== logRun) return;
  const body = $('#log-body');
  const atBottom = body.scrollTop + body.clientHeight >= body.scrollHeight - 20;
  body.textContent += r.text;
  logOffset = r.offset;
  if (atBottom) body.scrollTop = body.scrollHeight;
  $('#log-status').textContent = r.status === 'running' ? '运行中' : r.status === 'success' ? '成功' : '失败 ' + (r.error || '');
  if (r.status !== 'running') {
    clearInterval(logTimer);
    loadList();
    if (current) showTask(current);
    if (!$('#tab-gen').classList.contains('hidden')) loadGenRuns();
  }
}
$('#log-close').onclick = () => {
  clearInterval(logTimer); logRun = null;
  $('#log-panel').classList.add('hidden');
  document.body.classList.remove('log-open');
};
$('#log-stop').onclick = () => run(async () => { if (logRun) await api(`/api/runs/${logRun}/stop`, { method: 'POST' }); toast('已发送停止'); });

// ---------- 批量出题 ----------
function fillDomains() {
  // 出题领域固定为后端给出的主领域，页面只展示不提供切换
  const domain = Object.keys(META.domains)[0] || '';
  $('#gen-domain').value = domain;
  $('#gen-domain-label').textContent = domain;
  $('#gen-sub').innerHTML = '<option value="">由 AI 选择</option>' +
    (META.domains[domain] || []).map(s => `<option>${esc(s)}</option>`).join('');
}

$('#gen-form').onsubmit = e => {
  e.preventDefault();
  run(async () => {
    const body = Object.fromEntries(new FormData(e.target));
    body.count = +body.count;
    const r = await api('/api/gen', { method: 'POST', body });
    openLog(r.run_key, '批量出题');
    loadGenRuns();
  });
};

async function loadGenRuns() {
  const runs = (await api('/api/runs')).filter(r => r.kind === 'qa-gen');
  $('#gen-runs').innerHTML = '<div class="table-wrap"><table><colgroup><col style="width:24%"><col style="width:14%"><col><col style="width:80px"></colgroup><tbody>' +
    (runs.map(r => `<tr><td class="t-time">${esc(r.started_at)}</td>
    <td><span class="chip ${r.status === 'success' ? 'ok' : r.status === 'running' ? 'busy' : 'fail'}">${esc(r.status)}</span></td>
    <td>${esc(r.error || '')}</td><td><button class="ghost" data-log="${esc(r.run_key)}">日志</button></td></tr>`).join('') ||
    '<tr><td class="muted">无</td></tr>') + '</tbody></table></div>';
  document.querySelectorAll('#gen-runs [data-log]').forEach(b => b.onclick = () => openLog(b.dataset.log, '批量出题'));
}

// ---------- 配置 ----------
const CONFIG_FIELDS = [
  ['github_repo', 'GitHub 仓库（ssh 地址，推送使用 WSL 的 ssh 凭据）'],
  ['codex_path', 'codex 可执行文件路径（WSL 路径）'],
  ['agent_workspace_win', 'Codex 工作区（Windows 路径）'],
  ['agent_workspace_wsl', 'Codex 工作区（WSL 路径）'],
  ['model', '模型名'],
  ['reasoning_effort', '推理强度'],
  ['wsl_distro', 'WSL 发行版名'],
  ['concurrency', 'AI 环节并发数', 'number'],
  ['run_timeout_min', '单次运行超时（分钟）', 'number'],
  ['port_base', '验收端口起点', 'number'],
  ['port_span', '每题端口数量', 'number'],
  ['tasks_root', '做题目录根'],
  ['verify_root', '验收目录根'],
  ['record_root', '录屏目录根'],
  ['recording_dir', '录屏文件目录（WSL 路径，自动取其中最新的 mp4）'],
  ['seq_base', '提交表已有最大序号（新完成的题目从下一个号开始）', 'number'],
];

async function loadConfig() {
  const c = await api('/api/config');
  // 括号里的说明拆成小字提示，标签列保持简短对齐
  $('#config-form').innerHTML = '<h3>配置</h3><div class="fields">' + CONFIG_FIELDS.map(([k, label, type]) => {
    const m = label.match(/^(.*?)（(.*)）$/);
    return field(m ? m[1] : label, `<input name="${k}" type="${type || 'text'}" value="${esc(c[k])}" class="${type ? '' : 'mono'}">`, m ? m[2] : '');
  }).join('') + '<div class="form-foot"><button class="primary" type="submit">保存配置</button></div></div>';
}

$('#config-form').onsubmit = e => {
  e.preventDefault();
  run(async () => {
    const body = Object.fromEntries(new FormData(e.target));
    CONFIG_FIELDS.filter(f => f[2] === 'number').forEach(([k]) => body[k] = +body[k]);
    await api('/api/config', { method: 'PUT', body });
    toast('配置已保存');
  });
};

// ---------- 启动 ----------
(async () => {
  META = await api('/api/meta');
  $('#status-filter').innerHTML += META.statuses.map(s => `<option>${esc(s)}</option>`).join('');
  fillDomains();
  await run(loadList);
  setInterval(() => run(loadList), 10000);
})();
