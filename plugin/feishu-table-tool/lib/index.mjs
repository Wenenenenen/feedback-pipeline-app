import { createHash } from 'node:crypto';
import { stat } from 'node:fs/promises';
import { LarkClient, TableToolError } from './client.mjs';
import { normalizeField, mapFields, mapAttachments, visible, empty, fail, isObject } from './fields.mjs';

export { TableToolError } from './client.mjs';
export { visible, valueFor } from './fields.mjs';
const digest = value => createHash('sha256').update(JSON.stringify(value)).digest('hex');
const dataOf = result => result.data ?? result;
const baseArgs = (command, target) => ['base', command, '--base-token', target.baseToken, '--table-id', target.tableId, '--as', 'user', '--format', 'json'];
const sheetArgs = (command, target) => ['sheets', command, '--url', target.url, '--sheet-id', target.sheetId, '--as', 'user', '--format', 'json'];

function select(items, selector, label) {
  const choices = selector ? items.filter(x => x.id === selector || x.name === selector) : items;
  if (selector && choices.length !== 1) fail(`${label}“${selector}”不存在或不唯一，请使用 ID。`);
  return choices.length === 1 ? choices[0] : undefined;
}

function targetURL(value) {
  let url;
  try { url = new URL(value); } catch { fail('请提供完整的飞书表格 HTTPS 链接。'); }
  if (url.protocol !== 'https:' || url.username || url.password) fail('表格链接必须使用 HTTPS，且不应包含账号密码。');
  return url;
}

export function columnName(number) {
  let text = '';
  while (number > 0) { number--; text = String.fromCharCode(65 + number % 26) + text; number = Math.floor(number / 26); }
  return text;
}
const columnIndex = name => [...name].reduce((n, c) => n * 26 + c.charCodeAt(0) - 64, 0);

function cellMatrix(values) {
  if (!Array.isArray(values) || !values.length || !Array.isArray(values[0]) || !values[0].length) fail('values 必须是非空的二维数组。');
  const width = values[0].length;
  if (values.some(row => !Array.isArray(row) || row.length !== width)) fail('values 每一行的列数必须一致。');
  if (values.length * width > 10_000) fail('单次最多填写 10000 个单元格，请拆分区域。');
  return values.map(row => row.map(value => {
    if (value === null) return {}; // null 在电子表格中代表跳过，不清除原值。
    if (Array.isArray(value)) {
      if (value.some(v => !['string', 'number', 'boolean'].includes(typeof v) || (typeof v === 'number' && !Number.isFinite(v)))) fail('多选单元格必须是文本、数字或布尔值数组。');
      return { multiple_values: value.map(v => ({ value: v })) };
    }
    const format = isObject(value) ? value.format : undefined;
    if (isObject(value)) {
      if (Object.keys(value).some(key => !['value', 'format'].includes(key))) fail('单元格对象仅支持 value 和 format。公式请使用飞书原生工具。');
      value = value.value;
    }
    if (!['string', 'number', 'boolean'].includes(typeof value) || (typeof value === 'number' && !Number.isFinite(value))) fail('单元格仅支持文本、数字、布尔、多选数组或 null（跳过）。');
    if (typeof value === 'string' && value.startsWith('=')) fail('本工具填写静态数据；以 = 开头的内容会被 CLI 当作公式，已停止。');
    if (format !== undefined && typeof format !== 'string') fail('format 必须是数字格式字符串。');
    const cell = { value };
    if (typeof value === 'string' || format) cell.cell_styles = { number_format: format || '@' };
    return cell;
  }));
}

function writeRange(range, cells, sheet) {
  const match = /^([A-Z]+)([1-9]\d*)(?::([A-Z]+)([1-9]\d*))?$/.exec(String(range || '').toUpperCase());
  if (!match) fail('range 请使用 A1 或 A1:C3，不要附带子表名。');
  const col = columnIndex(match[1]), row = Number(match[2]);
  const endCol = col + cells[0].length - 1, endRow = row + cells.length - 1;
  if (match[3] && (columnIndex(match[3]) !== endCol || Number(match[4]) !== endRow)) fail('range 的行列尺寸必须与 values 完全一致。');
  if ((sheet.row_count && endRow > sheet.row_count) || ((sheet.column_count || sheet.col_count) && endCol > (sheet.column_count || sheet.col_count))) fail('写入超出当前子表网格，请先在飞书中扩展区域。');
  return `${match[1]}${row}:${columnName(endCol)}${endRow}`;
}

async function attachmentStamp(records) {
  const paths = [...new Set(records.flatMap(row => Object.values(row.attachments || {}).flat()))];
  return Promise.all(paths.map(async path => {
    const info = await stat(path);
    return { path, size: info.size, mtimeMs: info.mtimeMs };
  }));
}

export function createTableTool({ client = new LarkClient(), cliPath, profile, timeout } = {}) {
  if (cliPath || profile || timeout) client = new LarkClient({ cliPath, profile, timeout });

  async function inspect(urlString, { tableId, sheetId } = {}) {
    const url = targetURL(urlString), path = url.pathname;
    const isSheet = /\/(?:sheets|spreadsheets)\//.test(path);
    if (isSheet) return inspectSheets(urlString, sheetId || url.searchParams.get('sheet'));
    if (!/\/(?:base|wiki|app)\//.test(path)) fail('支持 /base/、/wiki/、/sheets/、/spreadsheets/ 和 /share/base/form/ 链接。');
    const resolved = dataOf(await client.json(['base', '+url-resolve', '--url', urlString, '--as', 'user', '--format', 'json']));
    if (/\/share\/base\/form\//.test(path) || resolved.share_token && /form/.test(resolved.resource_type || resolved.input_type || '')) {
      const shareToken = resolved.share_token || path.split('/').filter(Boolean).at(-1);
      const detail = dataOf(await client.json(['base', '+form-detail', '--share-token', shareToken, '--as', 'user', '--format', 'json']));
      if (!Array.isArray(detail.questions)) fail('表单未返回 questions，无法安全生成填写项。');
      return { kind: 'form', url: urlString, shareToken, baseToken: detail.base_token, title: detail.title || detail.name || '分享表单', fields: detail.questions.map(normalizeField) };
    }
    if (['sheet', 'sheets', 'spreadsheet'].includes(resolved.resource_type)) return inspectSheets(urlString, sheetId);
    if (!resolved.base_token) fail('这个链接没有指向可填写的数据表或电子表格。请复制实际数据表链接。');
    const tablesData = dataOf(await client.json(['base', '+table-list', '--base-token', resolved.base_token, '--as', 'user', '--format', 'json']));
    if (!Array.isArray(tablesData.tables)) fail('CLI 未返回数据表列表。');
    const tables = tablesData.tables.map(t => ({ ...t, id: t.id || t.table_id, name: t.name || t.table_name }));
    const table = select(tables, tableId || resolved.table_id || url.searchParams.get('table'), '数据表');
    let fields = [];
    if (table) {
      const response = dataOf(await client.json(baseArgs('+field-list', { baseToken: resolved.base_token, tableId: table.id })));
      if (!Array.isArray(response.fields)) fail('CLI 未返回字段列表。');
      fields = response.fields.map(normalizeField).sort((a, b) => a.name.localeCompare(b.name, 'zh-CN') || a.id.localeCompare(b.id));
    }
    return { kind: 'base', url: urlString, baseToken: resolved.base_token, tableId: table?.id, recordId: resolved.record_id || url.searchParams.get('record'), tables, fields };
  }

  async function inspectSheets(url, selector) {
    const info = dataOf(await client.json(['sheets', '+workbook-info', '--url', url, '--as', 'user', '--format', 'json']));
    if (!Array.isArray(info.sheets)) fail('CLI 未返回子表列表。');
    const sheets = info.sheets.map(s => ({ ...s, id: s.sheet_id, name: s.title || s.sheet_name }));
    const grid = sheets.filter(s => (s.resource_type || 'sheet') === 'sheet' && (selector || !s.is_hidden));
    const sheet = select(grid, selector, '普通网格子表');
    return { kind: 'sheets', url, sheets, sheetId: sheet?.id, sheet, fields: [] };
  }

  async function prepare(request) {
    if (!isObject(request)) fail('请求必须是 JSON 对象。');
    const normalized = structuredClone(request);
    const info = await inspect(request.url, request);
    const plan = { version: 1, request: normalized, target: info, action: request.action || (info.kind === 'base' ? 'create' : info.kind === 'form' ? 'submit-form' : 'cells') };
    normalized.action = plan.action;
    if (info.kind === 'base') {
      if (!info.tableId) fail('这个 Base 有多张数据表，请填写 tableId。');
      normalized.tableId = info.tableId;
      if (!['create', 'update'].includes(plan.action)) fail('多维表格 action 只能是 create 或 update。');
      if (!Array.isArray(request.records) || request.records.length < 1 || request.records.length > 200) fail('records 需要 1–200 条记录，同表请串行分批。');
      const rows = [];
      for (const row of request.records) {
        if (!isObject(row)) fail('每条记录必须是对象。');
        const fields = mapFields(info.fields, row.fields);
        const attachments = await mapAttachments(info.fields, row.attachments);
        if (!Object.keys(fields).length && !Object.keys(attachments).length) fail('每条记录至少填写一个字段或附件。');
        const recordId = row.recordId || (plan.action === 'update' ? info.recordId : undefined);
        if (plan.action === 'update' && !/^rec[a-zA-Z0-9]+$/.test(recordId || '')) fail('更新必须提供真实的 recordId。');
        if (plan.action === 'create' && row.recordId) fail('新增不接受 recordId；修改已有记录请使用 update。');
        rows.push({ ...(recordId ? { recordId } : {}), fields, attachments });
      }
      if (plan.action === 'update') {
        const ids = rows.map(r => r.recordId);
        if (new Set(ids).size !== ids.length) fail('同一批不能重复更新同一个 recordId。');
        const args = baseArgs('+record-get', info);
        args[args.indexOf('--format') + 1] = 'ndjson';
        // 只投影一列，检验记录存在性，避免 CLI 对不存在的 ID 返回空成功。
        if (info.fields.length) args.push('--field-id', info.fields.find(f => f.type === 'auto_number')?.id || info.fields[0].id);
        for (const id of ids) args.push('--record-id', id);
        const result = await client.json(args, { artifact: 'records.ndjson' });
        const found = new Set(result.artifact.map(r => r.record_id));
        const missing = ids.filter(id => !found.has(id));
        if (missing.length) fail(`记录不存在或你无权读取：${missing.join('、')}`);
      }
      for (const row of rows) for (const field of info.fields) {
        if (plan.action === 'create' && field.required && !field.readonly && empty(row.fields[field.name]) && empty(row.attachments[field.name])) fail(`缺少必填字段：${field.name}`);
      }
      normalized.records = rows;
      plan.payload = plan.action === 'create'
        ? { create_records: rows.map(r => r.fields) }
        : { update_records: Object.fromEntries(rows.map(r => [r.recordId, r.fields])) };
      plan.attachments = await attachmentStamp(rows);
      plan.summary = `${plan.action === 'create' ? '新增' : '更新'} ${rows.length} 条记录；附件 ${plan.attachments.length} 个文件`;
    } else if (info.kind === 'form') {
      if (plan.action !== 'submit-form') fail('分享表单 action 必须是 submit-form。');
      const fields = mapFields(info.fields, request.fields);
      const attachments = await mapAttachments(info.fields, request.attachments);
      const values = { ...fields, ...attachments };
      for (const field of info.fields) {
        const shown = visible(field.filter, values);
        if (!shown && (Object.hasOwn(fields, field.name) || Object.hasOwn(attachments, field.name))) fail(`题目“${field.name}”当前隐藏，请从请求中移除。`);
        if (shown && field.required && !field.readonly && empty(values[field.name])) fail(`缺少必填题：${field.name}`);
      }
      if (!Object.keys(fields).length && !Object.keys(attachments).length) fail('请至少填写一个题目。');
      if (Object.keys(attachments).length && !info.baseToken) fail('表单未返回 base_token，不能上传附件。');
      normalized.fields = fields;
      normalized.attachments = attachments;
      plan.payload = { fields, ...(Object.keys(attachments).length ? { attachments } : {}) };
      plan.attachments = await attachmentStamp([{ attachments }]);
      plan.summary = `提交分享表单；${Object.keys(fields).length} 个题目，${plan.attachments.length} 个附件文件`;
    } else {
      if (plan.action !== 'cells') fail('电子表格 action 必须是 cells。');
      if (!info.sheetId) fail('工作簿有多个子表，请填写 sheetId。');
      normalized.sheetId = info.sheetId;
      const cells = cellMatrix(request.values);
      const range = writeRange(request.range, cells, info.sheet);
      if (cells.every(row => row.every(c => !Object.keys(c).length))) fail('values 全部为 null，没有需要填写的单元格。');
      if (request.allowOverwrite !== undefined && typeof request.allowOverwrite !== 'boolean') fail('allowOverwrite 必须是布尔值。');
      normalized.range = range;
      plan.payload = cells;
      plan.range = range;
      plan.summary = `填写 ${info.sheet.name} 的 ${range}；${cells.length} 行 × ${cells[0].length} 列；${request.allowOverwrite === true ? '允许覆盖已有内容' : '遇非空单元格拒写'}`;
    }
    // 表结构或附件在用户确认后变化时，要求重新预览。
    plan.fingerprint = digest({ action: plan.action, request: normalized, schema: [...info.fields].sort((a, b) => String(a.id).localeCompare(String(b.id))), target: [info.baseToken, info.tableId, info.sheetId], attachments: plan.attachments });
    return plan;
  }

  async function submit(plan, { confirm = false } = {}) {
    if (confirm !== true) fail('提交需要显式 confirm: true；请先调用 prepare 查看计划。');
    if (!plan?.fingerprint || !plan.request) fail('请将 prepare 返回的完整计划传给 submit。');
    const current = await prepare(plan.request);
    if (current.fingerprint !== plan.fingerprint) fail('表结构、附件或计划在预览后发生变化，请重新预览并确认。');
    const target = current.target, completed = [];
    try {
      if (target.kind === 'sheets') {
        const args = sheetArgs('+cells-set', target).concat('--range', current.range, `--allow-overwrite=${current.request.allowOverwrite === true}`);
        const result = await client.json(args, { payload: current.payload, flag: '--cells' });
        completed.push({ action: 'cells', range: current.range, result });
        if (dataOf(result).partial_success || dataOf(result).partialSuccess || dataOf(result).status === 'partial_success') fail('写入返回部分成功，请核对结果后再处理。');
        const readback = await client.json(sheetArgs('+cells-get', target).concat('--range', current.range, '--include', 'value,formula'));
        return { ok: true, completed, readback, note: '已执行一次写入并回读目标范围。请核对 readback 中的值；不会自动重试。' };
      }
      if (target.kind === 'form') {
        const args = ['base', '+form-submit', '--share-token', target.shareToken, '--as', 'user', '--format', 'json'];
        if (current.attachments.length) args.push('--base-token', target.baseToken);
        // confirm:true 对应调用方已经审阅并明确确认这次提交。
        const result = await client.json(args.concat('--yes'), { payload: current.payload, filePaths: current.attachments.map(f => f.path) });
        completed.push({ action: 'submit-form', result });
        return { ok: true, completed };
      }
      const command = current.action === 'create' ? '+record-batch-create' : '+record-batch-update';
      const rows = current.request.records;
      let ids = rows.map(r => r.recordId);
      const hasFields = current.action === 'create' || rows.some(r => Object.keys(r.fields).length);
      if (hasFields) {
        const result = await client.json(baseArgs(command, target), { payload: current.payload });
        completed.push({ action: current.action, result });
        if (current.action === 'create') {
          ids = dataOf(result).record_id_list;
          if (!Array.isArray(ids) || ids.length !== rows.length) fail('创建响应未返回完整的 record_id_list，请先核对表内记录，勿重复创建。');
          completed.at(-1).recordIds = ids;
        }
        const ignored = dataOf(result).ignored_fields;
        if ((ignored && Object.keys(ignored).length) || dataOf(result).partial_success || dataOf(result).status === 'partial_success') fail('服务端忽略了部分字段或返回部分成功，请核对已完成结果。');
      }
      for (const [index, row] of rows.entries()) {
        for (const [field, paths] of Object.entries(row.attachments)) {
          const args = baseArgs('+record-upload-attachment', target).concat('--record-id', ids[index], '--field-id', field);
          for (const path of paths) args.push('--file', path);
          const result = await client.json(args, { filePaths: paths });
          completed.push({ action: 'attachment', recordId: ids[index], field, result });
        }
      }
      return { ok: true, recordIds: ids, completed };
    } catch (error) {
      throw new TableToolError(error.message, { state: 'unknown_or_partial', completed, detail: error.detail, hint: '写入可能已生效或部分生效。请根据 completed 和表内状态核对，勿直接重跑新增或上传。' });
    }
  }

  return { inspect, prepare, submit, status: () => client.json(['auth', 'status', '--json', '--verify']), whoami: () => client.json(['whoami', '--as', 'user']) };
}
