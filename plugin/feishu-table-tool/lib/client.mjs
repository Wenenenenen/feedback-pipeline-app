import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { mkdtemp, mkdir, writeFile, readFile, copyFile, rm, chmod, access } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, basename, dirname } from 'node:path';

const exec = promisify(execFile);

export class TableToolError extends Error {
  constructor(message, details = {}) {
    super(message);
    this.name = 'TableToolError';
    Object.assign(this, details);
  }
}

function parseOutput(text) {
  try { return JSON.parse(text.trim()); } catch {}
  // 部分 CLI 在结构化结果之前输出通知；不把这些通知误当成业务结果。
  for (const line of text.trim().split(/\r?\n/).reverse()) {
    try { return JSON.parse(line); } catch {}
  }
  return undefined;
}

export class LarkClient {
  constructor({ cliPath = process.env.LARK_CLI_BIN, profile, timeout = 120_000 } = {}) {
    this.cliPath = cliPath;
    this.profile = profile;
    this.timeout = timeout;
  }

  async executable() {
    if (this.cliPath) return this.cliPath;
    if (process.platform === 'darwin') {
      try { await access('/opt/homebrew/bin/lark-cli'); return '/opt/homebrew/bin/lark-cli'; } catch {}
    }
    return 'lark-cli';
  }

  async json(args, { payload, flag = '--json', filePaths = [], artifact } = {}) {
    const directory = await mkdtemp(join(tmpdir(), 'feishu-fill-'));
    await chmod(directory, 0o700);
    try {
      const staged = new Map();
      for (const [index, path] of [...new Set(filePaths)].entries()) {
        const name = join('files', String(index), basename(path));
        await mkdir(dirname(join(directory, name)), { recursive: true, mode: 0o700 });
        await copyFile(path, join(directory, name));
        await chmod(join(directory, name), 0o600);
        staged.set(path, `./${name}`);
      }
      const argv = args.map(arg => staged.get(arg) || arg);
      if (payload !== undefined) {
        const body = structuredClone(payload);
        if (body.attachments) {
          for (const [key, paths] of Object.entries(body.attachments)) {
            body.attachments[key] = paths.map(path => staged.get(path) || path);
          }
        }
        await writeFile(join(directory, 'payload.json'), JSON.stringify(body), { mode: 0o600 });
        argv.push(flag, '@payload.json');
      }
      if (artifact) argv.push('--output', artifact);
      if (this.profile) argv.unshift('--profile', this.profile);
      let stdout;
      try {
        ({ stdout } = await exec(await this.executable(), argv, {
          cwd: directory,
          encoding: 'utf8',
          timeout: this.timeout,
          maxBuffer: 16 * 1024 * 1024,
          shell: false,
          env: { ...process.env, LARKSUITE_CLI_NO_UPDATE_NOTIFIER: '1', LARKSUITE_CLI_NO_SKILLS_NOTIFIER: '1' },
        }));
      } catch (error) {
        const envelope = parseOutput(error.stderr || '') || parseOutput(error.stdout || '');
        const detail = envelope?.error;
        throw new TableToolError(detail?.message || (error.code === 'ENOENT'
          ? '找不到 lark-cli，请先安装，或设置 LARK_CLI_BIN 为完整路径。'
          : `CLI 调用失败（${error.code ?? error.signal ?? 'unknown'}）`), {
          exitCode: error.code, detail,
          hint: detail?.subtype === 'storage'
            ? '请在当前 macOS 用户的终端中运行，确保进程可访问钥匙串。无需重置已有授权。'
            : detail?.hint,
        });
      }
      const result = parseOutput(stdout);
      if (!result || result.ok === false) {
        throw new TableToolError(result?.error?.message || 'CLI 没有返回可解析的 JSON。', { detail: result?.error });
      }
      if (artifact) {
        const contents = await readFile(join(directory, artifact), 'utf8');
        result.artifact = contents.split(/\r?\n/).filter(Boolean).map(line => JSON.parse(line));
      }
      return result;
    } finally {
      await rm(directory, { recursive: true, force: true });
    }
  }
}
