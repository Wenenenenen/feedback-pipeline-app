// 录制合成用的纯计算函数（不依赖浏览器与平台，单独成文件便于测试）

// 结尾静止补帧时长（秒）：按画面变化出帧的采集（本机桌面 gfxcapture）在画面不动时不出帧，最后一次变化之后的停留
// 不会进文件；按停止录制的墙钟时刻补足，太短（误差）或超出常理（时钟不一致）时不补
export function tailPad(stopWall, vStart, vDur) {
  if (!stopWall || !Number.isFinite(vStart) || !Number.isFinite(vDur)) return 0;
  const tail = stopWall - (vStart + vDur);
  return tail >= 0.1 && tail <= 30 ? +tail.toFixed(3) : 0;
}
