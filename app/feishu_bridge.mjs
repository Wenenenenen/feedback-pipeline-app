// 飞书多维表格桥接：后端从 stdin 传入一个 JSON 请求，这里调用 plugin/feishu-table-tool 完成读写，stdout 输出一行 JSON 结果。
// 只用当前 lark-cli 已登录的用户身份；按 UID 列（甲方序号）找记录，有则更新、无则新增，不另存飞书记录 ID。
import { stat } from 'node:fs/promises';
import { basename } from 'node:path';
import { LarkClient } from '../plugin/feishu-table-tool/lib/client.mjs';
import { createTableTool, TableToolError } from '../plugin/feishu-table-tool/lib/index.mjs';

const client = new LarkClient({ timeout: Number(process.env.FEISHU_CLI_TIMEOUT_MS) || 300_000 });
const tool = createTableTool({ client });
const dataOf = r => r.data ?? r;
const fail = message => { throw new TableToolError(message); };

async function readStdin() {
  let text = '';
  for await (const chunk of process.stdin) text += chunk;
  return JSON.parse(text);
}

// 读表结构：只返回后端做字段映射需要的信息
async function inspect(req) {
  const info = await tool.inspect(req.url, { tableId: req.tableId });
  if (info.kind !== 'base') fail('飞书链接需要指向多维表格（/base/ 或 /wiki/ 下的多维表格）。');
  if (!info.tableId) fail(`这个多维表格有 ${info.tables.length} 张数据表，请复制具体数据表的链接（带 table= 参数）。`);
  return {
    baseToken: info.baseToken, tableId: info.tableId,
    tableName: info.tables.find(t => t.id === info.tableId)?.name || '',
    fields: info.fields.map(({ id, name, type, multiple, readonly, options, style }) => ({ id, name, type, multiple, readonly, options, style })),
  };
}

// 分页列出记录，只投影需要的列；filter 为 [[字段, 运算符, 值]] 形式的条件
async function listRecords(target, fieldNames, conditions) {
  const rows = [];
  for (let offset = 0; ; offset += 200) {
    const args = ['base', '+record-list', '--base-token', target.baseToken, '--table-id', target.tableId,
      '--as', 'user', '--format', 'json', '--limit', '200', '--offset', String(offset)];
    for (const name of fieldNames) args.push('--field-id', name);
    if (conditions) args.push('--filter-json', JSON.stringify({ logic: 'and', conditions }));
    const data = dataOf(await client.json(args));
    const names = data.fields || [];
    (data.data || []).forEach((values, i) => {
      rows.push({ recordId: data.record_id_list[i], cells: Object.fromEntries(names.map((n, j) => [n, values[j]])) });
    });
    if (!data.has_more || !(data.data || []).length) return rows;
  }
}

// 列出表中全部记录的 UID，供页面标出哪些题已写入飞书
async function list(req) {
  const target = await inspect(req);
  const rows = await listRecords(target, [req.uidField], null);
  return { records: rows.map(r => ({ recordId: r.recordId, uid: r.cells[req.uidField] ?? null })) };
}

// 附件比对：飞书里该格恰好是同名同大小的一个文件时不重传；否则上传新文件后移除旧文件（先传后删，失败时不会留下空格）
async function attachmentDiff(existing, paths) {
  const current = Array.isArray(existing) ? existing : [];
  const wanted = await Promise.all(paths.map(async p => ({ path: p, name: basename(p), size: (await stat(p)).size })));
  const same = current.length === wanted.length
    && wanted.every(w => current.some(c => c.name === w.name && Number(c.size) === w.size));
  return same ? null : { upload: paths, remove: current.map(c => c.file_token).filter(Boolean) };
}

async function upsert(req) {
  if (!Number.isFinite(req.uid)) fail('UID 必须是数字（甲方序号）。');
  const target = await inspect(req);
  const uidField = target.fields.find(f => f.name === req.uidField);
  if (!uidField) fail(`飞书表里没有「${req.uidField}」列。`);
  const attachmentNames = Object.keys(req.attachments || {});
  const found = await listRecords(target, [req.uidField, ...attachmentNames], [[req.uidField, '==', req.uid]]);
  if (found.length > 1) fail(`飞书表里 UID ${req.uid} 有 ${found.length} 条记录（${found.map(r => r.recordId).join('、')}），请先删掉多余的再写入。`);
  const base = { url: req.url, tableId: target.tableId };

  if (!found.length) {
    const plan = await tool.prepare({ ...base, action: 'create', records: [{ fields: { ...req.fields, [req.uidField]: req.uid }, attachments: req.attachments || {} }] });
    const receipt = await tool.submit(plan, { confirm: true });
    return { action: 'create', recordId: receipt.recordIds?.[0], uploaded: attachmentNames, kept: [] };
  }

  const recordId = found[0].recordId;
  const upload = {}, remove = {}, kept = [];
  for (const name of attachmentNames) {
    const diff = await attachmentDiff(found[0].cells[name], req.attachments[name]);
    if (!diff) { kept.push(name); continue; }
    upload[name] = diff.upload;
    if (diff.remove.length) remove[name] = diff.remove;
  }
  // 没有字段要写且附件都沿用时不调用插件（插件要求至少一个字段或附件）
  if (Object.keys(req.fields || {}).length || Object.keys(upload).length) {
    const plan = await tool.prepare({ ...base, action: 'update', records: [{ recordId, fields: req.fields || {}, attachments: upload }] });
    await tool.submit(plan, { confirm: true });
  }
  for (const [name, tokens] of Object.entries(remove)) {
    const args = ['base', '+record-remove-attachment', '--base-token', target.baseToken, '--table-id', target.tableId,
      '--record-id', recordId, '--field-id', name, '--as', 'user', '--format', 'json', '--yes'];
    for (const token of tokens) args.push('--file-token', token);
    await client.json(args);
  }
  return { action: 'update', recordId, uploaded: Object.keys(upload), kept };
}

const OPS = { inspect, list, upsert, status: () => tool.status() };

try {
  const req = await readStdin();
  const op = OPS[req.op];
  if (!op) fail(`未知操作：${req.op}`);
  process.stdout.write(JSON.stringify({ ok: true, ...(await op(req)) }) + '\n');
} catch (error) {
  process.stdout.write(JSON.stringify({
    ok: false, error: error.message, hint: error.hint || '', state: error.state || '',
    completed: error.completed || [],
  }) + '\n');
  process.exitCode = 1;
}
