#!/usr/bin/env node
import { parseArgs } from 'node:util';
import { readFile, writeFile } from 'node:fs/promises';
import { createInterface } from 'node:readline/promises';
import { stdin, stdout, stderr } from 'node:process';
import { createTableTool, valueFor, visible } from '../lib/index.mjs';

const help = `飞书表格填写工具（使用当前用户身份）

用法：
  node bin/feishu-fill.mjs fill [表格链接]       交互填写、预览、确认后提交
  node bin/feishu-fill.mjs inspect <链接>       读取数据表/子表/字段
  node bin/feishu-fill.mjs template <链接>      生成 JSON 填写模板
  node bin/feishu-fill.mjs preview --input job.json   只校验与预览
  node bin/feishu-fill.mjs submit --input job.json --commit  预览后提交
  node bin/feishu-fill.mjs status               验证当前登录状态

选项：
  --table <表ID或名称>   选择 Base 数据表
  --sheet <子表ID或名称> 选择电子表格子表
  --input <路径>        JSON 请求文件
  --output <路径>       保存只读信息/模板/预览/提交回执（不覆盖已有文件）
  --profile <名称>      指定已配置的 CLI 应用
  --commit              明确确认本次 JSON 请求可以写入
  --help                查看帮助

CLI 路径可通过 LARK_CLI_BIN 指定。无需安装额外 npm 依赖。
`;

async function choose(rl, items, label) {
  items.forEach((item, i) => stderr.write(`${i + 1}. ${item.name} (${item.id})\n`));
  while (true) {
    const answer = await rl.question(`选择${label}编号：`);
    const index = Number(answer) - 1;
    if (Number.isInteger(index) && index >= 0 && index < items.length) return items[index].id;
    stderr.write('请输入列表中的编号。\n');
  }
}

async function promptField(rl, field, tool) {
  stderr.write(`\n${field.name} [${field.type}]${field.required ? '（必填）' : ''}\n`);
  if (field.description) stderr.write(`${field.description}\n`);
  if (field.options.length) field.options.forEach((option, i) => stderr.write(`  ${i + 1}. ${option}\n`));
  while (true) {
    let text = await rl.question('填写（回车跳过；:null 清空；:file 路径 读取长文本）：');
    if (text === '') return { skip: true };
    try {
      if (text.startsWith(':file ')) text = await readFile(text.slice(6).trim(), 'utf8');
      else if (text === ':null') return { value: null };
      else if (text.startsWith('::')) text = text.slice(1);
      let value = text;
      if (field.type === 'attachment') {
        const paths = text.trim().startsWith('[') ? JSON.parse(text) : [text];
        if (!Array.isArray(paths) || !paths.length || paths.some(p => typeof p !== 'string')) throw new Error('请填本地路径，或 JSON 路径数组。');
        return { attachment: paths };
      }
      if (field.type === 'number') {
        if (!text.trim()) throw new Error('请输入数字。');
        value = Number(text);
      } else if (field.type === 'checkbox') {
        if (!['true', 'false', '是', '否', '1', '0'].includes(text.trim())) throw new Error('请输入 true/false 或 是/否。');
        value = ['true', '是', '1'].includes(text.trim());
      } else if (field.type === 'select') {
        const tokens = text.split(',').map(s => s.trim());
        if (field.options.length && tokens.every(s => /^\d+$/.test(s))) value = tokens.map(s => {
          const option = field.options[Number(s) - 1];
          if (option === undefined) throw new Error('选项编号超出范围。');
          return option;
        });
        else value = text.trim().startsWith('[') ? JSON.parse(text) : [text];
      } else if (field.type === 'user' && text === ':me') {
        const me = await tool.whoami();
        const id = (me.data ?? me).onBehalfOf?.openId;
        if (!id) throw new Error('无法取得当前用户 openId，请填写真实 ID 数组。');
        value = [{ id }];
      } else if (['user', 'group_chat', 'link', 'location'].includes(field.type)) {
        value = JSON.parse(text);
      }
      return { value: valueFor(field, value) };
    } catch (error) { stderr.write(`${error.message}\n`); }
  }
}

function guide(fields) {
  return fields.map(f => ({ id: f.id, name: f.name, type: f.type, readonly: f.readonly, required: f.required, multiple: f.multiple, options: f.options, description: f.description }));
}

async function interactive(tool, url) {
  if (!stdin.isTTY) throw new Error('交互填写需要终端；程序中请使用 prepare/submit，或用 --input JSON 文件。');
  const rl = createInterface({ input: stdin, output: stderr });
  try {
    let defaultUrl = '';
    try { defaultUrl = JSON.parse(await readFile(new URL('../config.json', import.meta.url), 'utf8')).defaultUrl || ''; } catch {}
    if (!url) url = (await rl.question(`飞书表格链接${defaultUrl ? '（回车使用已配置示例表）' : ''}：`)) || defaultUrl;
    let info = await tool.inspect(url);
    const request = { url };
    if (info.kind === 'base' && !info.tableId) {
      if (!info.tables.length) throw new Error('没有可访问的数据表。');
      request.tableId = await choose(rl, info.tables, '数据表');
      info = await tool.inspect(url, request);
    }
    if (info.kind === 'sheets' && !info.sheetId) {
      const grid = info.sheets.filter(s => (s.resource_type || 'sheet') === 'sheet' && !s.is_hidden);
      if (!grid.length) throw new Error('没有可填写的可见网格子表。');
      request.sheetId = await choose(rl, grid, '子表');
      info = await tool.inspect(url, request);
    }
    if (info.kind === 'sheets') {
      request.action = 'cells';
      request.sheetId = info.sheetId;
      request.range = await rl.question('填写范围（如 A2:C2）：');
      const source = await rl.question('二维 JSON 数据，或 @本地JSON文件路径：');
      request.values = JSON.parse(source.startsWith('@') ? await readFile(source.slice(1), 'utf8') : source);
      request.allowOverwrite = (await rl.question('是否允许覆盖已有内容？输入 覆盖 确认，回车保护原数据：')) === '覆盖';
    } else {
      let recordId;
      if (info.kind === 'base') {
        request.tableId = info.tableId;
        const mode = await rl.question('1. 新增记录  2. 更新已有记录（回车选择 1）：');
        if (!['', '1', '2'].includes(mode)) throw new Error('操作编号无效，未提交。');
        request.action = mode === '2' ? 'update' : 'create';
        if (request.action === 'update') recordId = (await rl.question(`recordId${info.recordId ? `（回车 ${info.recordId}）` : ''}：`)) || info.recordId;
      } else request.action = 'submit-form';
      const editable = info.fields.filter(f => !f.readonly);
      stderr.write('\n可填写字段：\n');
      editable.forEach((f, i) => stderr.write(`${i + 1}. ${f.name} [${f.type}]\n`));
      stderr.write('人员字段可填 :me；复杂字段使用 JSON；附件填写本地文件路径。\n');
      const selection = await rl.question('填写哪些字段？输入逗号分隔编号，回车/all 表示全部：');
      const indexes = !selection || selection === 'all' ? editable.map((_, i) => i) : selection.split(',').map(x => Number(x.trim()) - 1);
      if (indexes.some(i => !Number.isInteger(i) || i < 0 || i >= editable.length)) throw new Error('字段编号无效。');
      const fields = Object.create(null), attachments = Object.create(null);
      for (const index of [...new Set(indexes)].sort((a, b) => a - b)) {
        const field = editable[index];
        if (info.kind === 'form' && !visible(field.filter, { ...fields, ...attachments })) continue;
        const answer = await promptField(rl, field, tool);
        if (answer.skip) continue;
        if (answer.attachment) attachments[field.name] = answer.attachment;
        else fields[field.name] = answer.value;
      }
      if (info.kind === 'base') request.records = [{ ...(recordId ? { recordId } : {}), fields, attachments }];
      else Object.assign(request, { fields, attachments });
    }
    const plan = await tool.prepare(request);
    stderr.write(`\n${plan.summary}\n${JSON.stringify(plan.request, null, 2)}\n`);
    const answer = await rl.question('输入 提交 确认写入；回车取消：');
    if (answer !== '提交') return { ok: true, cancelled: true, message: '已取消，没有提交。' };
    return tool.submit(plan, { confirm: true });
  } finally { rl.close(); }
}

async function main() {
  const { values, positionals } = parseArgs({ allowPositionals: true, options: {
    input: { type: 'string' }, output: { type: 'string' }, table: { type: 'string' }, sheet: { type: 'string' },
    commit: { type: 'boolean', default: false }, profile: { type: 'string' }, help: { type: 'boolean' },
  } });
  if (values.help) { stdout.write(help); return; }
  let [command = 'fill', url] = positionals;
  if (command.startsWith('https://')) { url = command; command = 'fill'; }
  if (!['fill', 'status', 'inspect', 'template', 'preview', 'submit'].includes(command)) throw new Error(`未知命令：${command}\n${help}`);
  // 在任何可能的写入之前验证回执路径，不因“文件已存在”诱使调用方重跑新增。
  if (values.output) await writeFile(values.output, '', { encoding: 'utf8', flag: 'wx', mode: 0o600 });
  const tool = createTableTool({ profile: values.profile });
  let result;
  if (command === 'fill') result = await interactive(tool, url);
  else if (command === 'status') result = await tool.status();
  else if (command === 'inspect' || command === 'template') {
    if (!url) throw new Error('请提供表格链接。');
    const info = await tool.inspect(url, { tableId: values.table, sheetId: values.sheet });
    if (command === 'inspect') result = info;
    else if (info.kind === 'base') result = { url, tableId: info.tableId || '请选择数据表ID', action: 'create', records: [{ fields: {}, attachments: {} }], _guide: guide(info.fields), _tables: info.tables };
    else if (info.kind === 'form') result = { url, action: 'submit-form', fields: {}, attachments: {}, _guide: guide(info.fields) };
    else result = { url, sheetId: info.sheetId || '请选择子表ID', action: 'cells', range: 'A2:B2', values: [['填写文本', 0]], allowOverwrite: false, _sheets: info.sheets };
  } else {
    if (!values.input) throw new Error('请使用 --input 指定 JSON 请求文件。');
    const request = JSON.parse(await readFile(values.input, 'utf8'));
    const plan = await tool.prepare(request);
    if (command === 'preview') result = plan;
    else {
      stderr.write(`${plan.summary}\n${JSON.stringify(plan.request, null, 2)}\n`);
      if (!values.commit) throw new Error('仅完成预览，没有写入。确认后在同一命令追加 --commit。');
      result = await tool.submit(plan, { confirm: true });
    }
  }
  const json = JSON.stringify(result, null, 2) + '\n';
  if (values.output) {
    try { await writeFile(values.output, json, { encoding: 'utf8', mode: 0o600 }); }
    catch { stderr.write(json); throw new Error('操作结果已输出，但回执文件保存失败；请核对结果，勿重复提交。'); }
  }
  stdout.write(json);
}

main().catch(error => {
  stderr.write(JSON.stringify({ ok: false, error: { message: error.message, hint: error.hint, state: error.state, completed: error.completed, detail: error.detail } }, null, 2) + '\n');
  process.exitCode = 1;
});
