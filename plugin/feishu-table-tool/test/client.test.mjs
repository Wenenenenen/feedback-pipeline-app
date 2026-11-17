import test from 'node:test';
import assert from 'node:assert/strict';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { mkdtemp, writeFile, readFile, stat, rm, access } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { tmpdir } from 'node:os';
import { LarkClient } from '../lib/client.mjs';

const exec = promisify(execFile);
const script = `#!/usr/bin/env node
import {readFileSync, writeFileSync, statSync} from 'node:fs';
const args = process.argv.slice(2);
const p = args.indexOf('--json');
const body = p >= 0 ? JSON.parse(readFileSync(args[p+1].slice(1),'utf8')) : undefined;
if (process.env.FAKE_AUDIT) {
  const before = (()=>{try{return JSON.parse(readFileSync(process.env.FAKE_AUDIT,'utf8'))}catch{return []}})();
  before.push(args); writeFileSync(process.env.FAKE_AUDIT,JSON.stringify(before));
}
if(args[0]==='error') {
  console.error(JSON.stringify({ok:false,error:{type:'authorization',subtype:'missing_scope',message:'缺少权限',hint:'请申请对应 scope'}})); process.exit(3);
}
if(args.includes('+url-resolve')) {console.log(JSON.stringify({ok:true,data:{base_token:'baseExample',resource_type:'bitable'}}));}
else if(args.includes('+table-list')) {console.log(JSON.stringify({ok:true,data:{tables:[{id:'tblOne',name:'表'}]}}));}
else if(args.includes('+field-list')) {console.log(JSON.stringify({ok:true,data:{fields:[{id:'fldName',name:'姓名',type:'text'}]}}));}
else if(args.includes('+record-batch-create')) {console.log(JSON.stringify({ok:true,data:{record_id_list:['recNew']}}));}
else {
  const files=body?.attachments?.['附件']||[];
  console.log(JSON.stringify({ok:true,data:{args,body,cwd:process.cwd(),mode:statSync('payload.json').mode & 0o777,files:files.map(p=>({path:p,text:readFileSync(p,'utf8')}))}},null,2));
}
`;

async function fixture() {
  const directory = await mkdtemp(join(tmpdir(), 'cli-fixture-'));
  const cliPath = join(directory, 'fake-cli.mjs');
  await writeFile(cliPath, script, { mode: 0o700 });
  return { directory, cliPath, client: new LarkClient({ cliPath }) };
}

test('通过真实子进程传递私有 JSON 文件，保留特殊字符并在结束后清理', async () => {
  const f = await fixture();
  try {
    const sentinel = join(f.directory, 'should-not-exist');
    const content = `中文\n\"引号\" $(touch ${sentinel}) \`touch ${sentinel}\``;
    const response = await f.client.json(['base', '+test'], { payload: { fields: { 文本: content } } });
    assert.equal(response.data.body.fields['文本'], content);
    assert.deepEqual(response.data.args, ['base', '+test', '--json', '@payload.json']);
    assert.equal(response.data.mode, 0o600);
    await assert.rejects(access(response.data.cwd));
    await assert.rejects(access(sentinel));
  } finally { await rm(f.directory, { recursive: true, force: true }); }
});

test('附件复制到 CLI 工作目录，保留文件名；不修改原文件', async () => {
  const f = await fixture();
  try {
    const path = join(f.directory, '原始报告.txt'); await writeFile(path, '附件内容');
    const response = await f.client.json(['base', '+test'], { payload: { fields: {}, attachments: { 附件: [path] } }, filePaths: [path] });
    assert.equal(response.data.files[0].text, '附件内容');
    assert.ok(response.data.files[0].path.endsWith('原始报告.txt'));
    assert.equal(await readFile(path, 'utf8'), '附件内容');
    await assert.rejects(access(response.data.cwd));
  } finally { await rm(f.directory, { recursive: true, force: true }); }
});

test('非零退出码保留结构化权限错误；不把失败当成成功', async () => {
  const f = await fixture();
  try {
    await assert.rejects(f.client.json(['error'], { payload: {} }), error => error.exitCode === 3 && error.message === '缺少权限' && error.hint.includes('scope'));
  } finally { await rm(f.directory, { recursive: true, force: true }); }
});

test('CLI submit 不带 --commit 只预览；--output 冲突在写入前失败', async () => {
  const f = await fixture();
  try {
    const request = join(f.directory, 'job.json'), audit = join(f.directory, 'audit.json');
    await writeFile(request, JSON.stringify({ url: 'https://example.feishu.cn/base/baseExample', records: [{ fields: { 姓名: '真实子进程测试' } }] }));
    const env = { ...process.env, LARK_CLI_BIN: f.cliPath, FAKE_AUDIT: audit };
    const program = resolve('bin/feishu-fill.mjs');
    await assert.rejects(exec(process.execPath, [program, 'submit', '--input', request], { env }), /Command failed/);
    assert.ok(!(JSON.parse(await readFile(audit, 'utf8'))).some(args => args.includes('+record-batch-create')));
    const existing = join(f.directory, 'receipt.json'); await writeFile(existing, 'keep');
    await assert.rejects(exec(process.execPath, [program, 'submit', '--input', request, '--commit', '--output', existing], { env }));
    assert.equal(await readFile(existing, 'utf8'), 'keep');
    assert.ok(!(JSON.parse(await readFile(audit, 'utf8'))).some(args => args.includes('+record-batch-create')));
    const { stdout } = await exec(process.execPath, [program, 'submit', '--input', request, '--commit'], { env });
    assert.equal(JSON.parse(stdout).ok, true);
    assert.equal((JSON.parse(await readFile(audit, 'utf8'))).filter(args => args.includes('+record-batch-create')).length, 1);
  } finally { await rm(f.directory, { recursive: true, force: true }); }
});
