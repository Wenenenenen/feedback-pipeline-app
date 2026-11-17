// 流水线管理页面：题目列表、详情操作、批量出题、配置、运行日志
'use strict';

const $ = (s, el = document) => el.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
let META = { statuses: [], domains: {} };
let current = null;      // 当前详情题目 id
// 运行日志：每个运行一个标签，各自记录读取位置与已读内容，运行中的都在后台轮询
const logTabs = new Map();  // run_key → {title, offset, text, status, error}
let logRun = null;          // 正在查看的运行 ID
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

// 多选下拉：按钮显示已选摘要，点开是勾选面板；一项都不选等于全部。
// 选择按 storeKey 记在本机，下次打开沿用；面板用固定定位，不会被滚动容器裁掉。
function multiPicker(root, { options, allText, storeKey, initial = [], onChange }) {
  let selected = new Set(initial);
  try {
    const saved = storeKey && JSON.parse(localStorage.getItem(storeKey) || 'null');
    if (Array.isArray(saved)) selected = new Set(saved);
  } catch (e) { /* 本地存储不可用时用初始值 */ }
  root.classList.add('picker');
  root.innerHTML = `<button type="button" class="picker-btn"><span class="picker-text"></span><i class="picker-caret">▾</i></button>
    <div class="picker-pop hidden" role="listbox" aria-multiselectable="true"></div>`;
  const btn = $('.picker-btn', root), pop = $('.picker-pop', root);

  function summary() {
    const labels = options.filter(o => selected.has(o.value)).map(o => o.label);
    if (!labels.length) return allText;
    return labels.length <= 2 ? labels.join('、') : `${labels[0]} 等 ${labels.length} 项`;
  }
  function renderPop() {
    pop.innerHTML = options.map(o => `${o.group ? '<div class="picker-sep"></div>' : ''}
      <label class="picker-item"><input type="checkbox" value="${esc(o.value)}" ${selected.has(o.value) ? 'checked' : ''}> ${esc(o.label)}</label>`).join('') +
      `<div class="picker-foot"><button type="button" class="ghost" data-act="clear">${esc(allText)}</button></div>`;
    pop.querySelectorAll('input').forEach(x => x.onchange = () => {
      x.checked ? selected.add(x.value) : selected.delete(x.value);
      changed();
    });
    $('[data-act="clear"]', pop).onclick = () => { selected.clear(); renderPop(); changed(); };
  }
  function refreshText() {
    btn.querySelector('.picker-text').textContent = summary();
    btn.classList.toggle('active', api_.values().length > 0);
  }
  function changed() {
    refreshText();
    try { if (storeKey) localStorage.setItem(storeKey, JSON.stringify([...selected])); } catch (e) { /* 忽略 */ }
    onChange?.(api_.values());
  }
  function place() {
    const r = btn.getBoundingClientRect();
    pop.style.top = `${r.bottom + 4}px`;
    pop.style.left = `${Math.min(r.left, window.innerWidth - pop.offsetWidth - 8)}px`;
  }
  function close() { pop.classList.add('hidden'); }
  btn.onclick = () => {
    if (!pop.classList.contains('hidden')) return close();
    renderPop();
    pop.classList.remove('hidden');
    place();
  };
  document.addEventListener('click', e => { if (!root.contains(e.target)) close(); });
  document.addEventListener('keydown', e => { if (e.key === 'Escape') close(); });
  window.addEventListener('resize', close);
  document.addEventListener('scroll', e => { if (!pop.contains(e.target)) close(); }, true);

  const api_ = {
    // 只返回当前选项里有的值（选项变化后旧值自动失效），按选项顺序排列
    values: () => options.filter(o => selected.has(o.value)).map(o => o.value),
    set(vals) { selected = new Set(vals); changed(); },
    // 换选项只刷新按钮文字，不触发 onChange
    setOptions(opts) { options = opts; refreshText(); },
  };
  refreshText();
  return api_;
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
function showTab(name) {
  document.querySelectorAll('.tab').forEach(x => x.classList.toggle('active', x.dataset.tab === name));
  document.querySelectorAll('.tab-body').forEach(x => x.classList.toggle('hidden', x.id !== 'tab-' + name));
  if (name === 'gen') loadGenRuns();
  if (name === 'config') loadConfig();
  // 提交表每次切进来都按当前范围重新读取
  if (name === 'submission') run(loadSubmission);
}
document.querySelectorAll('.tab').forEach(b => b.onclick = () => showTab(b.dataset.tab));

// ---------- 列表 ----------
// 状态筛选选项：各状态之后是自动做题的两项（以 __ 开头，按题目标记过滤）
function statusOptions(withAuto) {
  const opts = META.statuses.map(s => ({ value: s, label: s }));
  return withAuto ? opts.concat({ value: '__auto', label: '自动做题中', group: true },
    { value: '__paused', label: '自动需处理' }) : opts;
}
const statusFilter = multiPicker($('#status-filter'), {
  options: [], allText: '全部状态', storeKey: 'list-status-filter', onChange: () => run(loadList),
});

async function loadList() {
  // 多选按「或」组合：题目满足任一勾选项即显示；不勾选时显示全部
  const sel = new Set(statusFilter.values());
  let rows = await api('/api/tasks');
  if (sel.size) rows = rows.filter(t => sel.has(t.status) || (sel.has('__auto') && t.auto_run) || (sel.has('__paused') && t.auto_paused));
  const checked = new Set([...document.querySelectorAll('.row-check:checked')].map(x => +x.value));
  $('#task-count').textContent = `${rows.length} 题`;
  $('#task-table tbody').innerHTML = rows.map(t => `
    <tr data-id="${t.id}" class="${t.id === current ? 'sel' : ''}">
      <td><input type="checkbox" class="row-check" value="${t.id}" ${checked.has(t.id) ? 'checked' : ''}></td>
      <td class="t-seq">${t.client_seq ? '#' + esc(t.client_seq) : '—'}${t.client_submit_time ? `<div class="t-client" title="已在甲方平台提交 ${esc(t.client_submit_time)}">已提交</div>` : ''}</td>
      <td><div class="t-branch">${esc(t.branch)}</div><div class="t-sub">${esc(t.subdomain)}</div></td>
      <td>${esc(t.difficulty)}</td>
      <td><div class="chip-stack"><span class="chip ${statusClass(t.status)}">${esc(t.status)}</span>
          ${t.busy ? '<span class="chip busy">运行中</span>' : ''}
          ${t.auto_run ? '<span class="chip auto">自动</span>' : ''}
          ${t.auto_paused ? '<span class="chip fail">需处理</span>' : ''}
          ${t.due_flag ? `<span class="chip due">${esc(t.due_flag)}</span>` : ''}</div></td>
      <td class="t-time">${esc(shortTime(t.update_time))}${t.done_time ? `<br>完成 ${esc(shortTime(t.done_time))}` : ''}</td>
    </tr>`).join('') || '<tr><td colspan="6" class="muted" style="text-align:center;padding:28px">暂无题目</td></tr>';
  document.querySelectorAll('#task-table tbody tr[data-id]').forEach(tr => tr.onclick = e => {
    if (e.target.classList.contains('row-check')) return;
    showTask(+tr.dataset.id);
  });
}
$('#refresh').onclick = () => run(loadList);
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
// 「复制整行」与「复制表头」不带的列（甲方表格里没有或不需要粘贴的）
const ROW_COPY_SKIP = new Set(['序号', '分支', '备注']);

function tsvCell(v) {
  const s = String(v ?? '');
  return /[\t\n\r"]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
}

// 甲方提交标记：已提交时可取消，未提交给出标记按钮；没有甲方序号的题不能标记
function clientMarkHtml(r) {
  if (r.client_submit_time) {
    return `<span class="chip ok" title="已在甲方平台提交 ${esc(r.client_submit_time)}">甲方已提交</span>
      <button type="button" class="ghost" data-client-mark="${r.id}" title="取消已提交标记">取消</button>`;
  }
  return r.client_seq ? `<span class="chip">甲方未提交</span>
      <button type="button" data-client-mark="${r.id}" title="在甲方平台提交后点这里标记">标记已提交</button>` : '';
}

// 标记或取消甲方提交：返回保存后的题目，放弃取消时返回 null
async function toggleClientSubmit(id, submitted) {
  if (submitted && !confirm('取消「已在甲方平台提交」标记？')) return null;
  await api(`/api/tasks/${id}/action/client_submit`, { method: 'POST', body: { submitted: !submitted } });
  toast(submitted ? '已取消甲方提交标记' : '已标记甲方提交');
  return api('/api/tasks/' + id);
}

// 弹窗修改或补填甲方序号：返回保存后的题目，取消时返回 null
async function editClientSeq(id, cur) {
  const s = prompt('甲方序号（留空则清除）', cur || '');
  if (s === null) return null;
  await api(`/api/tasks/${id}/action/client_seq`, { method: 'POST', body: { client_seq: s } });
  toast(s.trim() ? '已保存甲方序号' : '已清除甲方序号');
  return api('/api/tasks/' + id);
}

// User Prompt 的第一句：取第一个非空行到第一个句末标点，过长时只截开头
function promptHead(text, max = 80) {
  const line = String(text || '').split('\n').map(s => s.trim()).find(Boolean) || '';
  const first = /^[^。！？!?；;]*[。！？!?；;]?/.exec(line)[0];
  return first.length > max ? first.slice(0, max) + '…' : first;
}

// 导出 Excel：范围与提交表页一致（勾选的题目，或全部已完成的），文件名取后端给的带时间戳的名字
async function downloadSubmission(ids) {
  const res = await fetch('/api/submission/xlsx', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ids }) });
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || '导出失败');
  const m = /filename\*=UTF-8''([^;]+)/.exec(res.headers.get('Content-Disposition') || '');
  const a = document.createElement('a');
  a.href = URL.createObjectURL(await res.blob());
  a.download = m ? decodeURIComponent(m[1]) : '提交表.xlsx';
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  toast('已导出 ' + a.download);
}

// 提交表范围：all 为按状态筛选（默认只看已完成），checked 为题目列表里勾选的题
let subScope = 'all';
const subStatus = multiPicker($('#sub-status'), {
  options: [], allText: '全部状态', storeKey: 'sub-status-filter', initial: ['已完成'],
  onChange: () => { subScope = 'all'; run(loadSubmission); },
});

function checkedIds() {
  return [...document.querySelectorAll('.row-check:checked')].map(x => +x.value);
}

async function loadSubmission() {
  const checked = checkedIds();
  // 选了「勾选的题目」但列表里没有勾选时退回按状态筛选
  const useChecked = subScope === 'checked' && checked.length > 0;
  const statuses = subStatus.values();
  const data = await api('/api/submission', { method: 'POST', body: useChecked ? { ids: checked } : { statuses } });
  $('#sub-scope-all').classList.toggle('active', !useChecked);
  $('#sub-scope-checked').classList.toggle('active', useChecked);
  $('#sub-scope-checked').disabled = !checked.length;
  $('#sub-scope-checked').textContent = `列表勾选的${checked.length ? ` ${checked.length} 题` : ''}`;
  $('#sub-status').classList.toggle('dim', useChecked);
  const stText = statuses.length ? statuses.join('、') : '全部状态';
  $('#sub-scope').textContent = useChecked ? `勾选的 ${data.rows.length} 题` : `${stText} ${data.rows.length} 题`;
  // 导出与写入飞书都按当前页里的题目
  const ids = data.rows.map(r => r.id);
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
        <span class="client-mark" data-client-slot="${ri}">${clientMarkHtml(r)}</span>
        <span class="feishu-mark" data-feishu-slot="${ri}"></span>
        <span class="spacer"></span>
        ${nChanged ? `<button type="button" data-changed-copy="${ri}" title="每行一格 列名与新值以制表符分隔">复制改动格</button>` : ''}
        <button type="button" data-row-copy="${ri}">复制整行</button>
        <span class="sub-prompt">${esc(promptHead(r.cells.find(c => c.name === 'User Prompt')?.value))}</span>
      </summary>
      <div class="sub-grid">${r.cells.map(c => {
        const isChanged = c.name in changed;
        // 上传文件的格复制所在文件夹，展示仍是完整路径
        const what = c.copy ? '所在文件夹' : (isChanged ? '新值' : '');
        const tip = isChanged ? `返修改过 原值：${changed[c.name] || '空'}\n点击复制${what}` : `点击复制${what}`;
        return `
        <div class="sub-cell ${c.wide ? 'wide' : ''} ${c.value ? '' : 'empty'} ${isChanged ? 'changed' : ''}" data-cell="${esc(c.copy ?? c.value)}" title="${esc(tip)}">
          <div class="sub-name">${esc(c.name)}${isChanged ? '<span class="changed-tag">已改</span>' : ''}</div>
          <div class="sub-val">${c.value ? esc(c.value) : '空'}</div>
        </div>`;
      }).join('')}</div>
    </details>`;
  }).join('') || '<div class="empty-state"><div><b>没有可展示的题目</b>在题目列表勾选题目，或等题目标记完成后再看</div></div>';
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
    copyText(data.rows[+b.dataset.rowCopy].cells.filter(c => !ROW_COPY_SKIP.has(c.name)).map(c => tsvCell(c.value)).join('\t'));
  });
  // 只复制最近一轮返修改过的格：每行「列名 制表符 新值」，按提交表列顺序
  B.querySelectorAll('[data-changed-copy]').forEach(b => b.onclick = e => {
    e.preventDefault();
    e.stopPropagation();
    const r = data.rows[+b.dataset.changedCopy];
    const changed = r.rework?.changed || {};
    copyText(r.cells.filter(c => c.name in changed).map(c => `${c.name}\t${tsvCell(c.value)}`).join('\n'));
  });
  // 甲方提交标记：改完只刷新这一行的标记，不重绘整页（保持各行折叠状态）
  const bindClientMark = slot => slot.querySelectorAll('[data-client-mark]').forEach(b => b.onclick = e => {
    e.preventDefault();
    e.stopPropagation();
    run(async () => {
      const r = data.rows[+slot.dataset.clientSlot];
      const t = await toggleClientSubmit(r.id, !!r.client_submit_time);
      if (!t) return;
      r.client_seq = t.client_seq;
      r.client_submit_time = t.client_submit_time;
      slot.innerHTML = clientMarkHtml(r);
      bindClientMark(slot);
      loadList();
      if (current === r.id) showTask(r.id);
    });
  });
  B.querySelectorAll('[data-client-slot]').forEach(bindClientMark);
  $('#sub-expand').onclick = () => B.querySelectorAll('details.sub-row').forEach(d => { d.open = true; });
  $('#sub-collapse').onclick = () => B.querySelectorAll('details.sub-row').forEach(d => { d.open = false; });
  // 表头与「复制整行」去掉同样的列，粘贴后列对得上
  $('#sub-copy-head').onclick = () => copyText(data.columns.filter(c => !ROW_COPY_SKIP.has(c)).join('\t'));
  $('#sub-xlsx').disabled = !data.rows.length;
  $('#sub-xlsx').onclick = () => run(() => downloadSubmission(ids));
  // 写入飞书：范围与当前页一致；写完刷新各行的飞书标记
  const rowIds = data.rows.map(r => r.id);
  $('#sub-feishu').disabled = !data.rows.length;
  $('#sub-feishu').onclick = () => run(async () => {
    if (!confirm(`把当前页的 ${rowIds.length} 题写入飞书？按 UID（甲方序号）有则更新 无则新增`)) return;
    await syncFeishu(rowIds, `写入飞书 ${rowIds.length} 题`);
  });
  loadFeishuMarks(data, rowIds, ++subRender);
}

// 后台写入飞书并打开运行日志，运行结束后刷新提交表页的飞书标记
async function syncFeishu(ids, title) {
  const r = await api('/api/feishu/sync', { method: 'POST', body: { ids } });
  openLog(r.run_key, title);
  const timer = setInterval(async () => {
    const tab = logTabs.get(r.run_key);
    // 日志标签被关掉时停止等待
    if (!tab) return clearInterval(timer);
    if (tab.status !== 'running') {
      clearInterval(timer);
      if (!$('#tab-submission').classList.contains('hidden')) run(loadSubmission);
    }
  }, 3000);
}

// 各行的飞书标记：飞书表里有这个 UID 时显示已写入（多条时提示重复），每行都可单独写入
// 提交表页的渲染代号：飞书状态返回时页面已按新范围重绘的，丢弃这次结果
let subRender = 0;

async function loadFeishuMarks(data, ids, render) {
  const B = $('#sub-body');
  let st;
  try {
    st = await api('/api/feishu/status', { method: 'POST', body: { ids } });
  } catch (e) {
    if (render !== subRender) return;
    B.querySelectorAll('[data-feishu-slot]').forEach(s => { s.innerHTML = `<span class="chip fail" title="${esc(e.message)}">飞书读取失败</span>`; });
    return;
  }
  if (render !== subRender || !st.configured) return;
  B.querySelectorAll('[data-feishu-slot]').forEach(slot => {
    const r = data.rows[+slot.dataset.feishuSlot];
    const recs = st.records[r.id] || [];
    const chip = recs.length > 1 ? `<span class="chip fail" title="${esc(recs.join(' '))}">飞书重复 ${recs.length} 条</span>`
      : recs.length ? `<span class="chip ok" title="${esc(recs[0])}">飞书已写入</span>` : '<span class="chip">飞书未写入</span>';
    slot.innerHTML = `${chip}<button type="button" class="${recs.length ? 'ghost' : ''}" data-feishu-one="${r.id}" title="按 UID 有则更新 无则新增">${recs.length ? '重写飞书' : '写入飞书'}</button>`;
    slot.querySelector('[data-feishu-one]').onclick = e => {
      e.preventDefault();
      e.stopPropagation();
      run(() => syncFeishu([r.id], `${r.branch} 写入飞书`));
    };
  });
}

// 题目列表的「提交表」按钮：有勾选时直接看勾选的题，否则看全部已完成的
$('#submission').onclick = () => {
  subScope = checkedIds().length ? 'checked' : 'all';
  showTab('submission');
};
$('#sub-scope-all').onclick = () => { subScope = 'all'; run(loadSubmission); };
$('#sub-scope-checked').onclick = () => { subScope = 'checked'; run(loadSubmission); };
$('#sub-refresh').onclick = () => run(loadSubmission);

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

// 这一轮在提交表里要改的列：只列列名，新旧内容到提交表页里看
function changedColumnsHtml(i, isLast, S) {
  if (i.state === '返修中' && !i.summary) return '<div class="plain empty">AI 处理完后列出</div>';
  const cols = i.changed_columns || [];
  const recordHint = isLast && S === '待录屏' ? '<div class="muted hint">运行录屏一格要等重新录屏并标记完成后才会更新</div>' : '';
  return (cols.length ? `<div class="chip-stack">${cols.map(c => `<span class="chip human">${esc(c)}</span>`).join('')}</div>`
    : '<div class="plain empty">提交表没有要改的格</div>') +
    '<div class="muted hint">新旧内容在提交表页里看 改过的格会高亮</div>' + recordHint;
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
  // 初始化与准备环境一次跑完，只在需要人工处理时停在待准备环境
  if (S === '待初始化') actions.push(btn('初始化并准备环境', 'prepare', 'primary'));
  if (S === '待准备环境') {
    actions.push(btn('继续准备', 'prepare', 'primary'));
    if ((t.env_check || []).some(i => i.manual && !i.ok)) actions.push(btn('确认显卡能力并继续', 'confirm_gpu'));
  }
  if (S === '待做题') actions.push(btn('做题完成', 'mark_done', 'primary'), btn('重做', 'redo', 'danger'));
  if (S === '待收集') actions.push(btn('开始收集', 'collect', 'primary'), btn('重做', 'redo', 'danger'));
  if (S === '待验收') actions.push(btn('开始验收', 'review', 'primary'));
  if (S === '待查验') actions.push(btn('保存修改', 'save-review'), btn('通过', 'approve', 'primary'), btn('打回重新生成', 'reject', 'danger'));
  // 录屏环境就绪后按钮改为「重新准备」，避免关掉日志后不知道是否准备过
  if (S === '待录屏') actions.push(t.record_ready ? btn('重新准备录屏环境', 'prepare_record', 'ghost')
    : btn('准备录屏环境', 'prepare_record'), btn('标记完成', 'complete', 'primary'));
  if (S === '失败') {
    // 自动流程停下时重试按钮在停下处理框里，这里不重复
    if (!t.auto_pause) actions.push(btn('重试', 'retry', 'primary'));
    if (['待做题', '待收集'].includes(t.failed_from)) actions.push(btn('重做', 'redo', 'danger'));
  }
  // 甲方序号随时可改或补填；有甲方序号且已完成的题可标记甲方提交，已标记的可取消
  actions.push(btn(t.client_seq ? '改甲方序号' : '填写甲方序号', 'client_seq', 'ghost'));
  if (t.client_submit_time) actions.push(btn('取消甲方提交标记', 'client_submit', 'ghost'));
  else if (t.client_seq && ['已完成', '待返修'].includes(S)) actions.push(btn('标记甲方已提交', 'client_submit'));
  // 自动做题：未接管的未完成题可以交给自动，已接管的可以转人工
  if (t.auto_run && !t.auto_pause) actions.push(btn('转人工', 'auto_release', 'ghost', 'title="退出自动流程 之后按手动流程处理"'));
  else if (!['已完成', '待返修'].includes(S)) actions.push(btn('交给自动', 'auto_join', 'ghost', 'title="由自动做题调度器接着推进这道题"'));
  actions.push(btn('删除题目', 'delete', 'danger push'));
  const recordReady = t.record_ready
    ? `<span class="chip ok">已就绪</span> <span class="muted">${esc(t.record_ready_time || '')} 产物快照 ${esc((t.artifact_sha || '').slice(0, 8))}</span>`
    : `<span class="chip ${S === '待录屏' ? 'human' : ''}">未准备</span>${t.record_ready_sha && t.record_ready_sha !== t.artifact_sha
      ? ' <span class="muted">产物快照已变化 需重新准备</span>' : ''}`;

  const mats = (t.materials || []).map(m => `<tr>
      <td><span class="mono">${esc(m.path)}</span><br><span class="muted">${esc(m.purpose)}</span></td>
      <td>${esc(m.source)}${m.url ? `<br><a href="${esc(m.url)}" target="_blank">${esc(m.url)}</a>` : ''}</td>
      <td>${esc(m.state)} <span class="muted">${esc(m.note || '')}</span><br><span class="muted">${esc(m.license || '')}</span></td>
      <td>${m.script ? fold('查看脚本', codeBlock(m.script)) : ''}</td></tr>`).join('');

  const summary = t.traj_summary;
  const reviewEditable = S === '待查验';
  const ro = on => on ? '' : 'readonly';
  const evidence = (t.evidence || []).map(e => `<tr><td>${esc(e.phenomenon)}</td><td>${codeBlock(e.command)}</td>
      <td>${esc(e.output_excerpt)}</td><td>${e.recordable ? '可录' : '不可录'}${e.needs_recording_confirm ? '<br><span class="chip human">需录屏确认</span>' : ''}</td></tr>`).join('');
  const steps = (t.record_steps || []).map(s => `<li><div>${esc(s.action)}${s.command ? codeBlock(s.command) : ''}<span class="muted">预期 ${esc(s.expect)}</span></div></li>`).join('');
  // 录屏前准备与录完收尾不录，旧题没有这一项时不显示
  const prep = t.record_prep || {};
  const levels = META.repro_levels || [];
  const repro = t.reproducibility ?? '';
  const reproKnown = levels.includes(repro);
  const evidenceCount = (t.evidence || []).length;
  const inspectHtml = inspectPanel(t);

  $('#detail').innerHTML = `
    <div class="detail-head">
      <div class="title-row">
        <span class="seq" title="甲方序号">${t.client_seq ? '#' + esc(t.client_seq) : '未编号'}</span>
        <h2>${esc(t.branch)}</h2>
        <span class="chip ${statusClass(S)}">${esc(S)}</span>
        ${t.due_flag ? `<span class="chip due">${esc(t.due_flag)}</span>` : ''}${t.busy ? '<span class="chip busy">运行中</span>' : ''}
        ${t.client_submit_time ? `<span class="chip ok" title="已在甲方平台提交 ${esc(t.client_submit_time)}">甲方已提交</span>`
          : ['已完成', '待返修'].includes(S) ? '<span class="chip">甲方未提交</span>' : ''}
        ${S === '待录屏' && t.record_ready ? '<span class="chip ok">录屏环境已就绪</span>' : ''}
        ${t.auto_run ? '<span class="chip auto" title="由自动做题调度器推进">自动</span>' : ''}
      </div>
      <div class="meta-line">${esc(t.domain)} / ${esc(t.subdomain)} · ${esc(t.difficulty)} · ${esc(t.lang_framework || '技术栈未定')}</div>
      ${stepper(t)}
      ${autoPauseHtml(t)}
      ${t.last_error && !t.auto_pause ? `<div class="error-box">${esc(t.last_error)}</div>` : ''}
      ${t.env_blockers?.length ? `<div class="error-box">需要人工处理 处理完点「继续准备」：\n${esc(t.env_blockers.join('\n'))}</div>` : ''}
      <div class="actions">${actions.join('')}
        <span class="fold-all"><button type="button" class="ghost" data-fold-all="1">全部展开</button><button type="button" class="ghost" data-fold-all="0">全部折叠</button></span></div>
    </div>

    ${inspectHtml}

    ${panel(t, 'info', '题目信息', `<div class="fields">
      ${field('User Prompt', `${issuesHtml(t.prompt_issues).replace('口吻检查通过', 'prompt 检查通过')}
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
      ${t.auto_run ? field('观看自动做题', `${codeBlock(t.do_attach)}<div class="muted hint">自动做题时 Claude Code 在后台 tmux 会话里运行 在 WSL 终端执行这条命令只读观看 按 Ctrl+b 再按 d 退出观看</div>`) : ''}
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
      ${field('待上传轨迹', codeBlock(t.trajectory_win))}
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
      ${field('录屏环境', `<div class="plain">${recordReady}</div>`)}
      ${field('录屏目录', `${codeBlock('cd ' + t.record_dir)}<div class="muted hint">待录屏阶段点「准备录屏环境」后 产物快照会全新克隆到这里 录屏前准备 录屏步骤和收尾的命令都在这个目录下执行</div>`)}
      ${prep.setup ? field('录屏前准备', `${codeBlock(prep.setup)}<div class="muted hint">开录前在录屏目录终端执行 安装依赖并后台启动服务 这部分不录</div>`) : ''}
      ${field('录屏步骤', steps ? `${(t.record_issues || []).length ? issuesHtml(t.record_issues) : ''}<div class="muted hint">从这里开始录 直接在产物页面上操作出问题</div><ol class="steps">${steps}</ol>` : plain(''))}
      ${prep.cleanup ? field('录完收尾', `${codeBlock(prep.cleanup)}<div class="muted hint">停止录制后执行 不录</div>`) : ''}
      ${field('录屏文件', S === '待录屏'
        ? `<div class="inline"><select id="f-recording_link">${t.recording_link ? `<option selected>${esc(t.recording_link)}</option>` : '<option value="">读取录屏目录中</option>'}</select>
            <button type="button" data-rec-refresh="1">重新读取</button></div><div id="rec-info" class="rec-info"></div>`
        : codeBlock(t.recording_link), '默认取最新的 mp4')}
      ${t.record_script ? field('自动录屏脚本', recordScriptHtml(t)) : ''}
      ${field('轨迹文件', t.trajectory_path ? `${codeBlock(t.trajectory_win)}<div class="muted hint">即收集结果中的待上传轨迹 提交表的轨迹文件列直接取它</div>` : plain(''))}
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
  if (act === 'auto_release' && !confirm('转人工后自动做题不再推进这道题，之后按手动流程处理，确定？')) return;
  const body = {};
  if (act === 'auto_retry_regen') { act = 'auto_retry'; body.regen_script = true; }
  if (idx !== undefined) body.idx = +idx;
  if (act === 'approve') await saveReview(id);
  if (act === 'reject') body.comment = val('reject');
  if (act === 'rework') {
    body.text = val('inspect_text') || '';
    if (!body.text.trim()) throw new Error('请粘贴质检原文');
  }
  // 收集时产物提交信息直接用后端默认的 feat: 模型产物 - <分支名>，不再弹窗询问概述
  if (act === 'prepare_record' && t.record_ready &&
      !confirm('录屏环境已准备过，重新准备会删除录屏目录后重新克隆（目录里装的依赖、生成的文件都会清掉），确定？')) return;
  if (act === 'client_submit') {
    if (await toggleClientSubmit(id, !!t.client_submit_time)) { await loadList(); await showTask(id); }
    return;
  }
  if (act === 'client_seq') {
    if (await editClientSeq(id, t.client_seq)) { await loadList(); await showTask(id); }
    return;
  }
  if (act === 'complete') {
    body.recording_link = val('recording_link');
    if (!body.recording_link) throw new Error('请先选定录屏文件');
    if (!confirm(`确认录屏文件：\n${body.recording_link}`)) return;
  }
  const r = await api(`/api/tasks/${id}/action/${act}`, { method: 'POST', body });
  if (r.run_key) openLog(r.run_key, `${t.branch} ${act}`);
  else toast(act === 'auto_retry' ? '已重试 自动做题会接着推进' : '完成');
  await loadList();
  await showTask(id);
  if (act.startsWith('auto_')) loadAuto();
}

// 自动流程停下时的处理框：环节、原因、逐条处理建议，以及日志、重试、转人工
function autoPauseHtml(t) {
  const p = t.auto_pause;
  if (!p) return '';
  const regen = p.step === '待录屏' ? btn('重新生成录屏脚本并重试', 'auto_retry_regen') : '';
  return `<div class="auto-pause">
    <div class="ap-head"><b>自动流程已停下</b><span class="chip fail">${esc(p.step)}</span><span class="muted">${esc(p.time || '')}</span></div>
    <pre class="ap-reason">${esc(p.reason)}</pre>
    <div class="ap-hints-title">处理建议</div>
    <ul class="ap-hints">${(p.hints || []).map(h => `<li>${esc(h)}</li>`).join('')}</ul>
    <div class="ap-actions">${p.run_key ? `<button type="button" class="ghost" data-log="${esc(p.run_key)}">查看日志</button>` : ''}
      ${btn('重试', 'auto_retry', 'primary')}${regen}${btn('转人工', 'auto_release', 'ghost')}
      ${['running', 'finishing'].includes(autoState.do?.state) ? '' : '<span class="muted">自动做题未开启 重试后要开启才会接着推进</span>'}</div>
  </div>`;
}

function recordScriptHtml(t) {
  const r = t.record_script || {};
  const head = r.manual_needed ? `<span class="fold-bad">判断需要人工录屏</span> ${esc(r.reason || '')}`
    : `预计 ${esc(r.est_seconds ?? '?')} 秒 生成于 ${esc(r.time || '')}`;
  return fold(head, `${r.reason && !r.manual_needed ? `<div class="muted hint">${esc(r.reason)}</div>` : ''}${codeBlock(r.script)}
    <div class="muted hint">产物快照或录屏步骤变化后会重新生成 也可以在停下时点「重新生成录屏脚本并重试」</div>`);
}

async function saveReview(id) {
  await api('/api/tasks/' + id, { method: 'PUT', body: {
    delivery_score: val('delivery_score'), score_reason: val('score_reason'), feedback: val('feedback'),
    reproducibility: val('reproducibility'), remark: val('remark') } });
}

// ---------- 运行日志 ----------
function openLog(runKey, title) {
  // 已打开的运行直接切过去，不重读内容；新运行追加一个标签
  if (!logTabs.has(runKey)) logTabs.set(runKey, { title, offset: 0, text: '', status: 'running', error: '' });
  logRun = runKey;
  $('#log-panel').classList.remove('hidden');
  // 日志抽屉打开时给两侧面板让出高度
  document.body.classList.add('log-open');
  renderLogTabs();
  showLogBody(true);
  pollLogs();
  if (!logTimer) logTimer = setInterval(pollLogs, 1500);
}

function logStatusText(t) {
  return t.status === 'running' ? '运行中' : t.status === 'success' ? '成功' : '失败 ' + (t.error || '');
}

function renderLogTabs() {
  $('#log-tabs').innerHTML = [...logTabs].map(([k, t]) =>
    `<span class="log-tab${k === logRun ? ' active' : ''}" role="tab" data-run="${esc(k)}" title="${esc(t.title)}">
      <i class="dot ${esc(t.status)}"></i><span class="name">${esc(t.title)}</span>
      <button type="button" class="x" data-close="${esc(k)}" title="关闭这个日志">×</button></span>`).join('');
  $('#log-tabs').querySelectorAll('[data-run]').forEach(el => el.onclick = e => {
    if (e.target.dataset.close) return closeLog(e.target.dataset.close);
    logRun = el.dataset.run;
    renderLogTabs();
    showLogBody(true);
  });
  const cur = logTabs.get(logRun);
  $('#log-status').textContent = cur ? logStatusText(cur) : '';
  $('#log-stop').disabled = !cur || cur.status !== 'running';
}

// 切换标签时整体替换内容并滚到底部；同一标签追加内容时只在原本就在底部时跟随
function showLogBody(reset, appended = '') {
  const body = $('#log-body');
  const cur = logTabs.get(logRun);
  if (reset) {
    body.textContent = cur ? cur.text : '';
    body.scrollTop = body.scrollHeight;
    return;
  }
  const atBottom = body.scrollTop + body.clientHeight >= body.scrollHeight - 20;
  body.textContent += appended;
  if (atBottom) body.scrollTop = body.scrollHeight;
}

let logPolling = false;
async function pollLogs() {
  if (logPolling) return;
  logPolling = true;
  try {
    const running = [...logTabs].filter(([, t]) => t.status === 'running' || !t.offset);
    let finished = false;
    await Promise.all(running.map(async ([key, t]) => {
      const r = await api(`/api/runs/${key}/log?offset=${t.offset}`).catch(() => null);
      // 读取期间标签被关掉了就丢弃结果
      if (!r || logTabs.get(key) !== t) return;
      t.text += r.text;
      t.offset = r.offset;
      if (t.status === 'running' && r.status !== 'running') finished = true;
      t.status = r.status;
      t.error = r.error || '';
      if (key === logRun && r.text) showLogBody(false, r.text);
    }));
    renderLogTabs();
    if (finished) {
      loadList();
      if (current) showTask(current);
      if (!$('#tab-gen').classList.contains('hidden')) loadGenRuns();
    }
    if (![...logTabs.values()].some(t => t.status === 'running')) { clearInterval(logTimer); logTimer = null; }
  } finally {
    logPolling = false;
  }
}

function closeLog(key) {
  logTabs.delete(key);
  if (!logTabs.size) return closeAllLogs();
  if (key === logRun) {
    logRun = [...logTabs.keys()].pop();
    showLogBody(true);
  }
  renderLogTabs();
}

function closeAllLogs() {
  clearInterval(logTimer); logTimer = null;
  logTabs.clear(); logRun = null;
  $('#log-panel').classList.add('hidden');
  document.body.classList.remove('log-open');
}
$('#log-close').onclick = closeAllLogs;

// 手动打开日志：所有运行中的运行补成标签；既没有运行中的也没有已打开的标签时，打开最近一次运行
async function openLogsManually() {
  const [runs, tasks] = await Promise.all([api('/api/runs'), api('/api/tasks')]);
  const branch = Object.fromEntries(tasks.map(t => [t.id, t.branch]));
  const title = r => `${r.task_id ? branch[r.task_id] || '#' + r.task_id : '批量出题'} ${r.kind}`;
  // 接口按时间倒序，反过来让最新的运行排在最右并成为当前标签
  runs.filter(r => r.status === 'running').reverse().forEach(r => openLog(r.run_key, title(r)));
  if (!logTabs.size && runs[0]) openLog(runs[0].run_key, title(runs[0]));
  if (!logTabs.size) toast('还没有运行记录');
}
$('#log-open').onclick = () => run(openLogsManually);
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

// 甲方序号预览：与后端同样的写法（逗号、空格分隔，支持 12100-12107 区间），只用于提示数量，校验以后端为准
function previewClientSeqs(text) {
  const out = [];
  const src = String(text || '').trim().replace(/\s*[-~～—–]\s*/g, '-');
  for (const tok of src.split(/[,，、;；\s]+/).filter(Boolean)) {
    const m = /^#?(\d+)(?:-#?(\d+))?$/.exec(tok);
    if (!m) return { error: `格式不对：${tok}` };
    const a = +m[1], b = +(m[2] || m[1]);
    if (a <= 0 || b < a) return { error: `序号无效：${tok}` };
    if (out.length + b - a + 1 > 10) return { error: '一次最多 10 个甲方序号' };
    for (let n = a; n <= b; n++) out.push(String(n));
  }
  return { seqs: out };
}
$('#gen-seqs').oninput = e => {
  const p = previewClientSeqs(e.target.value);
  $('#gen-seqs-hint').textContent = p.error ? p.error
    : p.seqs.length ? `将出 ${p.seqs.length} 道题：${p.seqs.join('、')}` : '逗号或空格分隔，连续的写成区间；入库的题按顺序分配这些序号';
};

$('#gen-form').onsubmit = e => {
  e.preventDefault();
  run(async () => {
    const body = Object.fromEntries(new FormData(e.target));
    const r = await api('/api/gen', { method: 'POST', body });
    openLog(r.run_key, '批量出题');
    // 这批序号已交给本次出题，清空输入避免重复提交
    $('#gen-seqs').value = '';
    $('#gen-seqs').oninput({ target: $('#gen-seqs') });
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
  ['codex_follow_toml', '沿用 config.toml 的供应商与模型（用 CC Switch 或第三方 API 时勾选；勾选后模型取 config.toml 的 model，没有时才用上面的模型名）', 'checkbox'],
  ['codex_config_toml', 'config.toml 路径（WSL 路径，留空自动定位 Windows 的 .codex\\config.toml）'],
  ['wsl_distro', 'WSL 发行版名'],
  ['concurrency', 'AI 环节并发数', 'number'],
  ['run_timeout_min', '单次运行超时（分钟）', 'number'],
  ['codex_max_reconnects', '连续重连上限（codex 连续重连达到该次数即判定连不上供应商并失败，0 表示不限制）', 'number'],
  ['port_base', '验收端口起点', 'number'],
  ['port_span', '每题端口数量', 'number'],
  ['tasks_root', '做题目录根'],
  ['verify_root', '验收目录根'],
  ['record_root', '录屏目录根'],
  ['recording_dir', '录屏文件目录（WSL 路径，自动取其中最新的 mp4）'],
  ['upload_dir', '待上传文件目录（WSL 路径，轨迹与自动录屏放在这里，文件名为甲方序号-分支名）'],
  ['gpu_auto_confirm', '自动确认显卡能力（准备环境时依赖显卡的题直接记为通过 不再停下等人工确认 环境检查明细里注明）', 'checkbox'],
  ['pool_gen_count', '补充题目池每轮出题数（1 到 10）', 'number'],
  ['do_parallel_max', '自动做题并行数（同时做题的 Claude Code 会话上限）', 'number'],
  ['auto_backlog_max', '自动做题后段积压上限（收集 验收 查验 录屏阶段没停下的自动题达到该数量就暂不取新题）', 'number'],
  ['do_ready_timeout_sec', '等待 Claude Code 就绪（秒）', 'number'],
  ['do_timeout_min', '单题做题超时（分钟）', 'number'],
  ['do_idle_sec', '做完判定的静止时长（秒，轨迹记录本轮结束后再静止这么久才算做完）', 'number'],
  ['do_api_error_redo_max', '接口报错自动重做次数（做题时遇到 504 529 等接口报错中断 自动作废会话并恢复初始快照重新做题的次数上限 0 表示不自动重做 直接停下）', 'number'],
  ['do_api_error_wait_sec', '接口报错后等待（秒，等这么久再重新做题）', 'number'],
  ['record_max_sec', '自动录屏时长上限（秒）', 'number'],
  ['record_audio', '自动录屏录制声音（虚拟屏需要 WSL 中安装 pulseaudio 本机桌面需要 Windows 的 pyaudiowpatch）', 'checkbox'],
  ['record_desktop', '自动录屏在本机桌面录制（用本机 Chrome Windows Terminal 和真实文件对话框 画面与真人录屏一致 只在桌面空闲时录 有人碰鼠标键盘立即中止 关闭时在 WSL 虚拟屏后台录制）', 'checkbox'],
  ['record_idle_min', '本机桌面录制前的空闲时长（分钟 没有鼠标键盘操作且没锁屏达到这么久才开录）', 'number'],
  ['record_user_retries', '本机桌面录制被打断后的重录次数', 'number'],
  ['feishu_url', '飞书多维表格链接（表头同提交表 另有存甲方序号的 UID 列 留空时不写入飞书 使用 WSL 中 lark-cli 登录的账号）'],
  ['feishu_auto_sync', '完成后自动写入飞书（题目进入已完成 含返修完成 后按 UID 有则更新 无则新增 失败只记在运行日志里）', 'checkbox'],
];

async function loadConfig() {
  const c = await api('/api/config');
  // 括号里的说明拆成小字提示，标签列保持简短对齐
  $('#config-form').innerHTML = '<h3>配置</h3><div class="fields">' + CONFIG_FIELDS.map(([k, label, type]) => {
    const m = label.match(/^(.*?)（(.*)）$/);
    // 勾选框单独渲染，保存时按勾选状态转成布尔值
    const ctl = type === 'checkbox'
      ? `<label class="check"><input name="${k}" type="checkbox" ${c[k] ? 'checked' : ''}> 启用</label>`
      : `<input name="${k}" type="${type || 'text'}" value="${esc(c[k])}" class="${type ? '' : 'mono'}">`;
    return field(m ? m[1] : label, ctl, m ? m[2] : '');
  }).join('') + '<div class="form-foot"><button class="primary" type="submit">保存配置</button></div></div>';
}

$('#config-form').onsubmit = e => {
  e.preventDefault();
  run(async () => {
    const body = Object.fromEntries(new FormData(e.target));
    CONFIG_FIELDS.filter(f => f[2] === 'number').forEach(([k]) => body[k] = +body[k]);
    // 未勾选的复选框不在 FormData 里，按元素状态显式取值
    CONFIG_FIELDS.filter(f => f[2] === 'checkbox').forEach(([k]) => body[k] = e.target.elements[k].checked);
    await api('/api/config', { method: 'PUT', body });
    toast('配置已保存');
  });
};

// ---------- 全自动 ----------
let autoState = {};
const AUTO_STATE_TEXT = { idle: '未开启', running: '运行中', stopping: '停止中', failed: '出错停止' };

async function loadAuto() {
  const prev = autoState;
  autoState = await api('/api/auto');
  renderAutoBar();
  renderPoolCard();
  // 开关状态或自动题的状态有变化时刷新列表与详情
  const sig = s => JSON.stringify([s.gen?.state, s.gen?.added, s.do?.state, (s.do?.paused || []).map(x => x.id),
    (s.do?.preparing || []).map(x => x.status), (s.do?.doing || []).map(x => x.id),
    (s.do?.backlog || []).map(x => x.status)]);
  if (prev.do && sig(prev) !== sig(autoState)) {
    loadList();
    if (current) showTask(current);
  }
}

function renderAutoBar() {
  const d = autoState.do || {};
  const st = d.state || 'idle';
  const front = (d.preparing || [])[0];
  const doingN = (d.doing || []).length;
  const paused = (d.paused || []).length;
  // 按钮随运行方式变化：未开启只有开始；运行中可选做完当前题后停止或立即停止；做完当前题后停止时可恢复取题
  const B = {
    start: `<button type="button" class="primary" data-auto-do="start" title="从题目池按顺序取题 自动准备环境 做题 收集 验收 查验 录屏">开始做题</button>`,
    resume: `<button type="button" class="ghost" data-auto-do="start" title="恢复从题目池取新题">继续取题</button>`,
    finish: `<button type="button" data-auto-do="finish" title="不再取新题 已接管的题继续走完所有环节 全部完成或停下后自动关闭">做完当前题后停止</button>`,
    stop: `<button type="button" class="danger" data-auto-do="stop" title="不再取新题 不再启动下一环节 正在跑的环节跑完就关闭 已接管的题停在当前状态">停止做题</button>`,
    stopping: '<button type="button" disabled>停止中…</button>',
  };
  const buttons = { idle: [B.start], running: [B.finish, B.stop],
    finishing: ['<span class="chip human">做完当前题后停止</span>', B.resume, B.stop], stopping: [B.stopping] }[st] || [B.start];
  $('#auto-bar').innerHTML = `
    ${buttons.join('')}
    ${st !== 'idle' || front || doingN || paused ? `<span class="auto-sum">
      ${front ? `<a href="#" data-goto="${front.id}" title="准备线当前的题（Claude Code 开始做题后就准备下一道）">准备 ${esc(front.branch)} ${esc(front.status)}</a>` : '<span class="muted">准备线空闲</span>'}
      <span title="${esc((d.doing || []).map(x => x.branch).join(' ') || '没有在做题的会话')}">做题 ${doingN}/${esc(d.doing_max ?? '-')}</span>
      <span title="收集 验收 查验 录屏阶段的自动题（不含停下的） / 积压上限">后段 ${(d.backlog || []).length}/${esc(d.backlog_max ?? '-')}</span>
      ${paused ? `<a href="#" class="bad" data-paused="1" title="只看自动流程停下待处理的题">需处理 ${paused}</a>` : ''}
      <span class="muted" title="待初始化且未被接管的题">池 ${esc(d.pool ?? 0)}</span>
      ${d.error ? `<span class="bad" title="${esc(d.error)}">调度出错</span>` : ''}</span>` : ''}`;
  const DO_TOAST = { start: '已开始自动做题', finish: '不再取新题 已接管的题走完后自动停止', stop: '已停止取新题 在跑的环节跑完为止' };
  $('#auto-bar').querySelectorAll('[data-auto-do]').forEach(b => b.onclick = () => run(async () => {
    const action = b.dataset.autoDo;
    autoState = await api('/api/auto/do', { method: 'POST', body: { action } });
    toast(DO_TOAST[action]);
    renderAutoBar();
    loadList();
  }));
  $('#auto-bar').querySelectorAll('[data-goto]').forEach(a => a.onclick = e => { e.preventDefault(); run(() => showTask(+a.dataset.goto)); });
  $('#auto-bar').querySelectorAll('[data-paused]').forEach(a => a.onclick = e => {
    e.preventDefault();
    statusFilter.set(['__paused']);
  });
}

function renderPoolCard() {
  const g = autoState.gen || {};
  const st = g.state || 'idle';
  const busy = st === 'running' || st === 'stopping';
  const label = st === 'running' ? '停止补充' : st === 'stopping' ? '本轮结束后停止' : '补充题目池';
  const chip = { running: 'busy', failed: 'fail', stopping: 'human' }[st] || '';
  $('#pool-card').innerHTML = `<h3>补充题目池</h3>
    <div class="pool-row">
      <div class="pool-stat"><b>${esc(autoState.do?.pool ?? 0)}</b><span class="muted">道题待做</span></div>
      <span class="chip ${chip}">${AUTO_STATE_TEXT[st] || esc(st)}</span>
      ${g.started_at ? `<span class="muted">本次 ${esc(g.started_at)} 起 已完成 ${esc(g.rounds)} 轮 入库 ${esc(g.added)} 题</span>` : ''}
      <span class="spacer"></span>
      ${g.current_run ? `<button type="button" class="ghost" data-log="${esc(g.current_run)}">当前出题日志</button>` : ''}
      <button type="button" id="pool-btn" class="${st === 'running' ? 'danger' : 'primary'}" ${st === 'stopping' ? 'disabled' : ''}>${label}</button>
    </div>
    <div class="muted hint">每轮 ${esc(g.count ?? 10)} 题 子方向与难度由 AI 判断 技术栈偏好与补充要求为空 出完的题进入题目池（待初始化）；${busy ? '再点一次在当前这轮出题结束后停止' : '开始后一轮接一轮不停出题'}；出错即停</div>
    ${st === 'failed' ? `<div class="error-box">出错停止：${esc(g.error)}</div>
      ${g.last_run ? `<div><button type="button" data-log="${esc(g.last_run)}">查看出错日志</button></div>` : ''}` : ''}`;
  $('#pool-btn').onclick = () => run(async () => {
    const action = st === 'running' ? 'stop' : 'start';
    // 自动出题还不能分配甲方序号，暂停使用（正在运行的仍可停止）
    if (action === 'start') return toast('补充题目池暂停使用：自动出题还不能分配甲方序号，请用下方批量出题', true);
    autoState = await api('/api/auto/gen', { method: 'POST', body: { action } });
    toast(action === 'start' ? '已开始补充题目池' : '当前这轮出题结束后停止');
    renderPoolCard();
  });
  $('#pool-card').querySelectorAll('[data-log]').forEach(b => b.onclick = () => openLog(b.dataset.log, '补充题目池'));
}

// ---------- 启动 ----------
(async () => {
  META = await api('/api/meta');
  statusFilter.setOptions(statusOptions(true));
  subStatus.setOptions(statusOptions(false));
  fillDomains();
  await run(loadList);
  await run(loadAuto);
  setInterval(() => run(loadList), 10000);
  setInterval(() => run(loadAuto), 5000);
})();
