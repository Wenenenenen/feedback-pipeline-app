import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, writeFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createTableTool, visible, valueFor } from '../lib/index.mjs';

const URL = 'https://example.feishu.cn/base/baseExample';
const FORM = 'https://example.feishu.cn/share/base/form/shrExample';
const SHEETS = 'https://example.feishu.cn/sheets/sheetExample';
const fields = [
  { id: 'fldName', name: '姓名', type: 'text' },
  { id: 'fldScore', name: '评分', type: 'number', style: { type: 'rating', min: 1, max: 5 } },
  { id: 'fldState', name: '状态', type: 'select', multiple: false, options: [{ name: '困难' }, { name: '地狱' }] },
  { id: 'fldFile', name: '附件', type: 'attachment' },
  { id: 'fldCalc', name: '计算结果', type: 'formula' },
];
const envelope = data => ({ ok: true, identity: 'user', data });

class Fake {
  constructor() { this.calls = []; this.fields = structuredClone(fields); this.tables = [{ id: 'tblOne', name: '数据表' }]; this.questions = []; }
  async json(args, options = {}) {
    this.calls.push({ args, options });
    switch (args[1]) {
      case '+url-resolve': return envelope(args.includes(FORM) ? { share_token: 'shrExample', resource_type: 'form' } : { base_token: 'baseExample', resource_type: 'bitable' });
      case '+table-list': return envelope({ tables: this.tables });
      case '+field-list': return envelope({ fields: this.fields });
      case '+form-detail': return envelope({ base_token: 'baseExample', questions: this.questions });
      case '+workbook-info': return envelope({ sheets: this.sheets || [{ sheet_id: 'shOne', title: '明细', resource_type: 'sheet', is_hidden: false, row_count: 100, column_count: 10 }] });
      case '+record-get': return { ...envelope({ records_count: this.missing ? 0 : 1 }), artifact: this.missing ? [] : [{ record_id: 'recExisting' }] };
      case '+record-batch-create': return envelope({ record_id_list: options.payload.create_records.map((_, i) => `recNew${i}`) });
      case '+record-batch-update': return envelope({});
      case '+record-upload-attachment': if (this.uploadFails) throw new Error('模拟附件失败'); return envelope({ uploaded: true });
      case '+form-submit': return envelope({ can_submit_again: false });
      case '+cells-set': return envelope(this.partial ? { status: 'partial_success' } : { updated_cells_count: 2 });
      case '+cells-get': return envelope({ ranges: [{ cells: [[{ value: '001' }, { value: 12 }]], row_indices: [1], col_indices: [0, 1] }] });
      default: throw new Error(`Unexpected command ${args}`);
    }
  }
  writes() { return this.calls.filter(c => ['+record-batch-create', '+record-batch-update', '+record-upload-attachment', '+form-submit', '+cells-set'].includes(c.args[1])); }
}

const createRequest = fields => ({ url: URL, records: [{ fields }] });

test('预览只读；必须显式确认；成功后不重复创建', async () => {
  const client = new Fake(), tool = createTableTool({ client });
  const plan = await tool.prepare(createRequest({ 姓名: '张三', 状态: '困难' }));
  assert.deepEqual(plan.payload.create_records[0]['状态'], ['困难']);
  assert.equal(client.writes().length, 0);
  await assert.rejects(tool.submit(plan), /confirm/);
  assert.equal(client.writes().length, 0);
  const result = await tool.submit(plan, { confirm: true });
  assert.deepEqual(result.recordIds, ['recNew0']);
  assert.equal(client.writes().length, 1);
});

test('只读字段、错误选项、评分越界、空数据均在写入前拒绝', async () => {
  const client = new Fake(), tool = createTableTool({ client });
  for (const [values, message] of [[{ 计算结果: 'x' }, /只读/], [{ 状态: '不存在' }, /只能选/], [{ 评分: 9 }, /评分/], [{}, /至少/], [{ 姓名: 42 }, /字符串/]]) {
    await assert.rejects(tool.prepare(createRequest(values)), message);
  }
  assert.equal(client.writes().length, 0);
});

test('同名字段必须消歧，不能用字段名静默覆盖另一列', async () => {
  const client = new Fake();
  client.fields.push({ id: 'fldOther', name: '姓名', type: 'text' });
  const tool = createTableTool({ client });
  await assert.rejects(tool.prepare(createRequest({ 姓名: 'a' })), /同名/);
  await assert.rejects(tool.prepare(createRequest({ fldName: 'a' })), /同名/);
  // 即使用 ID 指定，CLI 按名称映射也会歧义；应拒绝而不是猜测。
  assert.equal(client.writes().length, 0);
});

test('多表/多子表不默认选择第一张；显式 ID 可以选择', async () => {
  const client = new Fake(); client.tables.push({ id: 'tblTwo', name: '数据表2' });
  const tool = createTableTool({ client });
  const info = await tool.inspect(URL);
  assert.equal(info.tableId, undefined);
  await assert.rejects(tool.prepare(createRequest({ 姓名: 'a' })), /多张/);
  const plan = await tool.prepare({ ...createRequest({ 姓名: 'a' }), tableId: 'tblTwo' });
  assert.equal(plan.target.tableId, 'tblTwo');
  client.sheets = [{ sheet_id: 's1', title: 'A' }, { sheet_id: 's2', title: 'B' }];
  await assert.rejects(tool.prepare({ url: SHEETS, range: 'A2', values: [['x']] }), /多个子表/);
});

test('确认后字段变化或计划被改动，停止执行', async () => {
  const client = new Fake(), tool = createTableTool({ client });
  const plan = await tool.prepare(createRequest({ 姓名: '原内容' }));
  const tampered = structuredClone(plan); tampered.request.records[0].fields['姓名'] = '不同内容';
  await assert.rejects(tool.submit(tampered, { confirm: true }), /变化/);
  client.fields[0].name = '新列名';
  await assert.rejects(tool.submit(plan, { confirm: true }), /不存在|变化/);
  assert.equal(client.writes().length, 0);
});

test('服务端字段列表顺序变化不误判成结构变化', async () => {
  const client = new Fake(), tool = createTableTool({ client });
  const plan = await tool.prepare(createRequest({ 姓名: 'a' }));
  client.fields.reverse();
  assert.equal((await tool.submit(plan, { confirm: true })).ok, true);
});

test('更新校验记录存在，绝不把不存在的 ID 当作成功', async () => {
  const client = new Fake(), tool = createTableTool({ client });
  const request = { url: URL, action: 'update', records: [{ recordId: 'recExisting', fields: { 姓名: '新名字' } }] };
  client.missing = true;
  await assert.rejects(tool.prepare(request), /不存在/);
  client.missing = false;
  const plan = await tool.prepare(request);
  await tool.submit(plan, { confirm: true });
  assert.equal(client.writes()[0].args[1], '+record-batch-update');
  const read = client.calls.find(c => c.args[1] === '+record-get');
  assert.equal(read.args[read.args.indexOf('--format') + 1], 'ndjson');
});

test('附件失败保留已创建 ID，无自动重试；提前拒绝不存在的文件', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'table-test-'));
  try {
    const path = join(dir, '报告.pdf'); await writeFile(path, 'fixture');
    const client = new Fake(), tool = createTableTool({ client });
    const request = { url: URL, records: [{ fields: { 姓名: 'a' }, attachments: { 附件: [path] } }] };
    const plan = await tool.prepare(request);
    client.uploadFails = true;
    await assert.rejects(tool.submit(plan, { confirm: true }), error => {
      assert.equal(error.state, 'unknown_or_partial');
      assert.deepEqual(error.completed[0].recordIds, ['recNew0']);
      return true;
    });
    assert.equal(client.writes().filter(c => c.args[1] === '+record-batch-create').length, 1);
    assert.equal(client.writes().filter(c => c.args[1] === '+record-upload-attachment').length, 1);
    await assert.rejects(tool.prepare({ ...request, records: [{ attachments: { 附件: [join(dir, 'missing')] } }] }), /找不到附件/);
  } finally { await rm(dir, { recursive: true, force: true }); }
});

test('表单检查必填、隐藏条件；仅在确认后添加 CLI 确认参数', async () => {
  const client = new Fake();
  client.questions = [
    { id: 'q1', title: '需要反馈', type: 'select', select: { multiple: false, options: [{ name: '是' }, { name: '否' }] }, required: true },
    { id: 'q2', title: '反馈', type: 'text', required: true, filter: { conjunction: 'and', conditions: [{ field_name: '需要反馈', operator: 'is', value: ['是'] }] } },
  ];
  const tool = createTableTool({ client });
  await assert.rejects(tool.prepare({ url: FORM, fields: { 需要反馈: '是' } }), /必填/);
  await assert.rejects(tool.prepare({ url: FORM, fields: { 需要反馈: '否', 反馈: '不该提交' } }), /隐藏/);
  const plan = await tool.prepare({ url: FORM, fields: { 需要反馈: '否' } });
  assert.equal(client.writes().length, 0);
  await tool.submit(plan, { confirm: true });
  assert.ok(client.writes()[0].args.includes('--yes'));
  assert.throws(() => visible({ conjunction: 'and', conditions: [{ operator: 'unknown' }] }, {}), /暂不支持/);
});

test('电子表格保持文本前导零，约束写入边界，默认拒绝覆盖并回读', async () => {
  const client = new Fake(), tool = createTableTool({ client });
  const request = { url: SHEETS, range: 'A2', values: [['001', 12]] };
  const plan = await tool.prepare(request);
  assert.equal(plan.range, 'A2:B2');
  assert.deepEqual(plan.payload[0][0], { value: '001', cell_styles: { number_format: '@' } });
  const result = await tool.submit(plan, { confirm: true });
  assert.ok(client.writes()[0].args.includes('--allow-overwrite=false'));
  assert.ok(result.readback);
  await assert.rejects(tool.prepare({ ...request, range: 'A2:A2' }), /尺寸/);
  await assert.rejects(tool.prepare({ ...request, range: 'J100' }), /超出/);
  await assert.rejects(tool.prepare({ ...request, values: [['=SUM(A1:A2)']] }), /公式/);
  await assert.rejects(tool.prepare({ ...request, values: [[1], [1, 2]] }), /列数/);
  await assert.rejects(tool.prepare({ ...request, values: [[null]] }), /没有需要/);
});

test('电子表格部分成功返回可核对的已完成回执', async () => {
  const client = new Fake(); client.partial = true;
  const tool = createTableTool({ client });
  const plan = await tool.prepare({ url: SHEETS, range: 'A2', values: [[1]] });
  await assert.rejects(tool.submit(plan, { confirm: true }), e => e.completed.length === 1 && e.state === 'unknown_or_partial');
});

test('日期检查不依赖宿主时区，拒绝无效日期和时间', () => {
  const f = { type: 'datetime', name: '日期' };
  assert.equal(valueFor(f, '2026-11-16T10:30:00+08:00'), '2026-11-16T10:30:00+08:00');
  assert.throws(() => valueFor(f, '2026-02-30'), /不存在/);
  assert.throws(() => valueFor(f, '2026-11-16 25:00'), /无效/);
});
