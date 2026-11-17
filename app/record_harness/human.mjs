// 人手式操作的节奏与轨迹，两个平台的执行器共用
export const sleep = ms => new Promise(r => setTimeout(r, ms));
// 随机数：均匀分布与标准正态分布（模拟人手的不精确）
export const rand = (a, b) => a + Math.random() * (b - a);
export function gauss() {
  let u = 0;
  let v = 0;
  while (!u) u = Math.random();
  while (!v) v = Math.random();
  return Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * v);
}
export const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));

// 指针从 from 移到 to 的轨迹：略带弧度、先加速后减速（最小加加速度曲线）、带细微抖动，
// 远距离时偶尔略过目标再拉回。返回 [[x, y, 到下一点前的停顿毫秒], ...]，每 10 毫秒左右一个点。
export function humanPath(from, to, { ms, W, H } = {}) {
  const x = clamp(Math.round(to.x), 0, W - 1);
  const y = clamp(Math.round(to.y), 0, H - 1);
  const sx = from.x;
  const sy = from.y;
  const dist = Math.hypot(x - sx, y - sy);
  if (dist < 2) return [];
  const dur = ms ?? clamp((160 + 120 * Math.log2(dist / 25 + 1)) * rand(0.85, 1.2), 180, 1100);
  // 垂直于直线方向的偏移，形成弧线
  const nx = -(y - sy) / dist;
  const ny = (x - sx) / dist;
  const bend = dist * rand(0.02, 0.1) * (Math.random() < 0.5 ? -1 : 1);
  const c1 = { x: sx + (x - sx) * 0.3 + nx * bend, y: sy + (y - sy) * 0.3 + ny * bend };
  const k = rand(0.2, 0.7);
  const c2 = { x: sx + (x - sx) * 0.75 + nx * bend * k, y: sy + (y - sy) * 0.75 + ny * bend * k };
  const over = dist > 250 && Math.random() < 0.45;
  const ex = over ? x + (x - sx) / dist * rand(4, 10) + gauss() * 1.5 : x;
  const ey = over ? y + (y - sy) / dist * rand(4, 10) + gauss() * 1.5 : y;
  const steps = Math.max(6, Math.round(dur / 10));
  const wait = dur / steps;
  const pts = [];
  for (let i = 1; i <= steps; i++) {
    const t = i / steps;
    const e = t * t * t * (10 - 15 * t + 6 * t * t);
    const u = 1 - e;
    const bx = u * u * u * sx + 3 * u * u * e * c1.x + 3 * u * e * e * c2.x + e * e * e * ex;
    const by = u * u * u * sy + 3 * u * u * e * c1.y + 3 * u * e * e * c2.y + e * e * e * ey;
    const shake = i < steps ? 0.6 : 0;
    const px = clamp(Math.round(bx + gauss() * shake), 0, W - 1);
    const py = clamp(Math.round(by + gauss() * shake), 0, H - 1);
    const last = pts[pts.length - 1];
    // 同一位置的点合并成更长的停顿
    if (last && last[0] === px && last[1] === py) last[2] += wait;
    else pts.push([px, py, wait]);
  }
  if (over) {
    // 略过之后停顿一下再慢慢拉回目标
    pts[pts.length - 1][2] += rand(40, 90);
    const [lx, ly] = pts[pts.length - 1];
    const n = Math.round(rand(6, 10));
    for (let i = 1; i <= n; i++) {
      const t = i / n;
      const e = t * t * (3 - 2 * t);
      pts.push([Math.round(lx + (x - lx) * e), Math.round(ly + (y - ly) * e), 16]);
    }
  }
  pts[pts.length - 1] = [x, y, pts[pts.length - 1][2]];
  return pts.map(([px, py, w]) => [px, py, Math.round(w)]);
}

// 逐字输入时每个字之后的停顿（毫秒）：有快有慢，几个字一组，空格后偶尔停一下
export function typingDelays(text, base = 85) {
  const chars = [...String(text)];
  const out = [];
  let group = 0;
  for (const ch of chars) {
    let d;
    if (group > 0) {
      d = base * rand(0.35, 0.8);
      group--;
    } else {
      d = base * rand(0.8, 1.6);
      group = Math.floor(Math.random() * 3);
    }
    if (ch === ' ' && Math.random() < 0.3) d += rand(100, 300);
    else if (Math.random() < 0.04) d += rand(200, 450);
    out.push(Math.round(d));
  }
  return out;
}
