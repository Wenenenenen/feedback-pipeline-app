import { stat } from 'node:fs/promises';
import { resolve } from 'node:path';
import { TableToolError } from './client.mjs';

const readonly = new Set(['created_at', 'updated_at', 'created_by', 'updated_by', 'auto_number', 'formula', 'lookup', 'button']);
export const fail = message => { throw new TableToolError(message); };
export const isObject = x => x !== null && typeof x === 'object' && !Array.isArray(x);
export const empty = x => x === undefined || x === null || x === '' || (Array.isArray(x) && !x.length);

export function normalizeField(raw) {
  const type = raw.type;
  const config = isObject(raw[type]) ? raw[type] : raw;
  return {
    id: raw.id || raw.field_id,
    name: raw.name || raw.title,
    type,
    description: raw.description || '',
    required: raw.required === true,
    readonly: readonly.has(type),
    multiple: config.multiple !== false,
    options: (config.options || []).map(x => typeof x === 'string' ? x : x.name),
    dynamicOptions: Boolean(config.dynamic_options_source),
    style: config.style || raw.style || {},
    filter: raw.filter,
  };
}

export function valueFor(field, value) {
  if (field.readonly) fail(`“${field.name}”是只读字段，不能填写。`);
  if (value === null) return null;
  const bad = hint => fail(`“${field.name}”：${hint}`);
  switch (field.type) {
    case 'text':
      if (typeof value !== 'string') bad('请传字符串。');
      return value;
    case 'number':
      if (typeof value !== 'number' || !Number.isFinite(value)) bad('请传有限数字。');
      if (field.style.type === 'rating' && (value < field.style.min || value > field.style.max)) bad(`评分须在 ${field.style.min}–${field.style.max} 之间。`);
      return value;
    case 'checkbox':
      if (typeof value !== 'boolean') bad('请传 true 或 false。');
      return value;
    case 'select': {
      const values = typeof value === 'string' ? [value] : value;
      if (!Array.isArray(values) || values.some(x => typeof x !== 'string')) bad('请传选项名称或名称数组。');
      if (!field.multiple && values.length > 1) bad('单选只能填写一个选项。');
      if (field.options.length && !field.dynamicOptions && values.some(x => !field.options.includes(x))) bad(`只能选：${field.options.join('、')}`);
      return values;
    }
    case 'datetime':
      if (typeof value === 'number' && Number.isFinite(value)) return value;
      if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2}(?:\.\d{1,3})?)?(?:Z|[+-]\d{2}:\d{2})?)?$/.test(value)) bad('请传 YYYY-MM-DD、日期时间字符串或 Unix 毫秒时间戳。');
      {
        const [year, month, day] = value.slice(0, 10).split('-').map(Number);
        const date = new Date(0);
        date.setUTCFullYear(year, month - 1, day);
        if (date.getUTCFullYear() !== year || date.getUTCMonth() !== month - 1 || date.getUTCDate() !== day) bad('日期不存在。');
        const time = value.slice(11).match(/^(\d{2}):(\d{2})(?::(\d{2}))?/);
        if (time && (Number(time[1]) > 23 || Number(time[2]) > 59 || Number(time[3] || 0) > 59)) bad('时间无效。');
      }
      return value;
    case 'user': case 'group_chat': case 'link': {
      if (!Array.isArray(value) || value.some(x => !isObject(x) || typeof x.id !== 'string')) bad('请传 [{"id":"真实ID"}]。');
      if (!field.multiple && value.length > 1) bad('此字段只允许一个对象。');
      const prefix = { user: 'ou_', group_chat: 'oc_', link: 'rec' }[field.type];
      if (value.some(x => !x.id.startsWith(prefix))) bad(`ID 应以 ${prefix} 开头，请使用真实 ID。`);
      return value.map(({ id }) => ({ id }));
    }
    case 'location':
      if (!isObject(value) || !Number.isFinite(value.lng) || !Number.isFinite(value.lat) || Math.abs(value.lng) > 180 || Math.abs(value.lat) > 90) bad('请传有效的 {"lng":经度,"lat":纬度}。');
      return { lng: value.lng, lat: value.lat };
    case 'attachment': bad('附件应放在 attachments 中，传本地文件路径数组。'); break;
    default: bad(`暂不支持字段类型 ${field.type}，未提交任何值。`);
  }
}

export function mapFields(schema, fields = {}) {
  if (!isObject(fields)) fail('fields 必须是字段名/字段 ID 到值的对象。');
  const output = Object.create(null);
  for (const [key, value] of Object.entries(fields)) {
    const matches = schema.filter(f => f.id === key || f.name === key);
    if (matches.length !== 1) fail(`字段“${key}”不存在或同名，请使用字段 ID。`);
    const field = matches[0];
    if (schema.filter(f => f.name === field.name).length > 1) fail(`目标存在同名字段“${field.name}”，当前工具不能安全按名称提交。`);
    if (Object.hasOwn(output, field.name)) fail(`字段“${field.name}”被重复指定。`);
    output[field.name] = valueFor(field, value);
  }
  return output;
}

export async function mapAttachments(schema, attachments = {}, cwd = process.cwd()) {
  if (!isObject(attachments)) fail('attachments 必须是字段名到本地路径数组的对象。');
  const output = Object.create(null);
  for (const [key, paths] of Object.entries(attachments)) {
    const matches = schema.filter(f => f.id === key || f.name === key);
    if (matches.length !== 1 || matches[0].type !== 'attachment') fail(`“${key}”不是唯一的附件字段。`);
    if (schema.filter(f => f.name === matches[0].name).length > 1) fail(`附件字段“${key}”同名，不能安全提交。`);
    if (!Array.isArray(paths) || paths.length < 1 || paths.length > 50 || paths.some(p => typeof p !== 'string' || !p)) fail(`“${key}”需要 1–50 个本地文件路径。`);
    const actual = [];
    for (const path of paths) {
      const full = resolve(cwd, path);
      let info;
      try { info = await stat(full); } catch { fail(`找不到附件：${path}`); }
      if (!info.isFile() || info.size > 2 * 1024 ** 3) fail(`附件必须是常规文件且不超过 2GB：${path}`);
      actual.push(full);
    }
    if (Object.hasOwn(output, matches[0].name)) fail(`附件字段“${matches[0].name}”被重复指定。`);
    output[matches[0].name] = actual;
  }
  return output;
}

// 仅解释飞书文档中有定义的显示条件；未知条件拒绝提交，避免误填隐藏题。
export function visible(filter, values) {
  if (!filter || (isObject(filter) && !Object.keys(filter).length)) return true;
  if (!Array.isArray(filter.conditions) || !['and', 'or'].includes(filter.conjunction)) fail('表单包含暂不支持的显示条件，请在飞书原页面填写。');
  const results = filter.conditions.map(condition => {
    const actual = values[condition.field_name];
    const expected = Array.isArray(condition.value) ? condition.value : [condition.value];
    const list = Array.isArray(actual) ? actual : [actual];
    const same = list.length === expected.length && list.every(a => expected.some(b => JSON.stringify(a) === JSON.stringify(b)));
    const contains = list.some(a => expected.some(b => JSON.stringify(a) === JSON.stringify(b)));
    switch (condition.operator) {
      case 'isEmpty': return empty(actual);
      case 'isNotEmpty': return !empty(actual);
      case 'is': return same;
      case 'isNot': return !same;
      case 'contains': return typeof actual === 'string' ? expected.every(x => actual.includes(String(x))) : contains;
      case 'doesNotContain': return typeof actual === 'string' ? expected.every(x => !actual.includes(String(x))) : !contains;
      case 'isGreater': return !empty(actual) && Number(actual) > Number(expected[0]);
      case 'isGreaterEqual': return !empty(actual) && Number(actual) >= Number(expected[0]);
      case 'isLess': return !empty(actual) && Number(actual) < Number(expected[0]);
      case 'isLessEqual': return !empty(actual) && Number(actual) <= Number(expected[0]);
      default: fail(`暂不支持表单条件 ${condition.operator}，请在飞书原页面填写。`);
    }
  });
  return filter.conjunction === 'and' ? results.every(Boolean) : results.some(Boolean);
}
