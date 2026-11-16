---
name: qa-record
description: 把验收给出的录屏步骤写成可自动执行的录屏脚本，在虚拟屏上试跑通过后返回，供流水线后台录屏。
---

# qa-record 生成自动录屏脚本

## 输入

`<stdin>` 中给出：题目与 User Prompt 原文、交付完整性与 Feedback、可录的证据、录屏前准备（已经执行完，服务已在后台运行）及其输出末尾、录屏步骤、录完收尾、录屏目录、可用端口、浏览器窗口尺寸、时长上限、录屏脚本的写入位置、试跑命令、试跑截图目录。

开始前先读本目录的 `AGENTS.md`（权限边界、WSL 命令写法）。

## 录屏环境

- 流水线已在 WSL 中起好一块 1280x720 的虚拟屏（不是 Windows 桌面），浏览器是 WSL 里的 chromium，有标签栏和地址栏，产物服务也在 WSL 里，页面用 `http://localhost:<端口>` 打开。
- 画面没有真显卡：WebGL 走软件渲染，帧率偏低；WebGPU 多数情况不可用。
- 屏幕上有两个铺满全屏的窗口：浏览器（产物页面）和终端（xterm，按 Windows Terminal 的配色与字体，提示符与新开 WSL 终端一致，当前目录就是录屏目录），同一时间只显示其中一个。指针是 Windows 样式的系统指针，由流水线驱动真实的鼠标移动和点击，悬停在链接上会变成手形。录制的就是整块虚拟屏，声音来自浏览器和终端命令。
- 录屏前准备已经执行完，不要在脚本里再安装依赖、启动或结束服务；录完收尾由流水线执行。

## 脚本接口

脚本是一个 ES module：

```js
// 可选：开录前执行，不录。通常只用来打开产物页面并等它加载完，让录屏从产物页面开始。
export async function prepare(ctx) {
  await ctx.page.goto('http://localhost:20100/');
  await ctx.page.waitForLoadState('networkidle');
}

// 正式录制的部分
export default async function run(ctx) {
  ctx.step(1, '导入素材后点渲染，画面全黑');
  await ctx.click('text=导入');
  // ...
}
```

`ctx` 提供：

- `ctx.page`：产物页面（Playwright Page），`ctx.context`：浏览器上下文；
- `ctx.click(目标, {dx, dy, button, clickCount, hover})`：系统指针平滑移过去再真实点击；目标可以是选择器字符串、Locator 或页面坐标 `{x, y}`；点按钮、链接、下拉框、画布一律用它，不要用 `locator.click()` 或 `page.mouse`（那样画面上的指针不会动）；
- `ctx.moveTo(目标, {dx, dy, ms})`：只移动指针（悬停提示、悬停高亮）；
- `ctx.type(目标, 文字)`：点中输入框后逐字输入；
- `ctx.scroll(格数)`：在浏览器里滚动鼠标滚轮，正数向下；
- `ctx.term.run(命令, {timeout 秒, pause 毫秒})`：切到终端窗口，逐字打出命令并回车，等命令结束返回 `{code}`，命令失败不抛错；`ctx.term.show()` 只切到终端，`ctx.showBrowser()` 切回浏览器（之后的 click、moveTo 也会自动切回）；
- `ctx.exec(命令, {timeout 秒})`：在录屏目录后台执行命令（不出现在画面上），返回 `{code, output}`，只用来在脚本里核对状态；
- `ctx.step(序号, 说明)`：标记当前录到第几步，写进日志；
- `ctx.sleep(毫秒)`：停顿，让画面上的结果停留看清；
- `ctx.shot(名字)`：整块屏幕截图到试跑截图目录（含终端与指针；试跑时用来核对画面，正式录制时也会保存）；
- `ctx.cwd`：录屏目录的 WSL 路径，`ctx.dryRun`：是否试跑。

文件选择：先等文件选择框再点按钮，用录屏目录下的 WSL 路径：

```js
const [fc] = await Promise.all([ctx.page.waitForEvent('filechooser'), ctx.click('text=导入文件')]);
await fc.setFiles(`${ctx.cwd}/.qa/broken.wav`);
```

## 步骤

1. 读 Feedback 与录屏步骤，弄清每一步要在画面上展示的现象。录屏步骤是按人工录屏写的（Windows 浏览器、Windows 文件路径），换成上面的接口：打开页面用 `localhost`，文件路径换成录屏目录下的 WSL 路径，终端命令用 `ctx.term.run`（终端里已经在录屏目录下，命令用相对路径）。
2. 先在录屏目录里读产物代码，按页面上真实存在的元素写选择器（优先 `text=`、`role=`、`#id`），不要猜。
3. 写脚本到输入给出的录屏脚本位置，按「试跑命令」执行。试跑在同一块虚拟屏上进行，不录制；输出每行一个 JSON 事件，`error` 事件给出失败原因，出错时会在截图目录留下 `error.png`。
4. 在关键位置调用 `ctx.shot`，试跑后打开截图核对画面上确实出现了 Feedback 写的问题，不对就改脚本重新试跑，直到试跑成功且截图符合预期。
5. 试跑会改变服务与录屏目录的状态（上传过的文件、写过的数据），流水线正式录制前会重新克隆、重新准备，脚本不要依赖试跑留下的状态。

## 要求

- `run` 部分在时长上限减 5 秒内结束，按录屏步骤的顺序逐步执行，每步开头调用 `ctx.step`；每个现象出现后 `ctx.sleep(1500)` 到 `3000` 让画面停留，整体节奏不要太快也不要拖。
- 只录 Feedback 里写到的内容，失败项照录；不在录屏里跑临时脚本、注入控制台脚本或改页面状态，证据放在页面上的正常操作里。录屏步骤本身要求使用控制台脚本定位精确像素时，用 `ctx.page.evaluate` 执行。
- 不调用 `page.pause`、不等待人工输入，不打开新的浏览器，不改动录屏目录里的已提交文件；脚本里不写死试跑生成的临时数据。
- 以下情况无法自动录屏，`manual_needed` 设为 true，`reason` 写清原因，`script` 给空字符串：
  - 现象依赖真显卡（如 WebGPU 渲染、软件渲染下帧率过低导致无法展示问题）且没有替代展示方式；
  - 需要摄像头、麦克风、手柄等外设，或需要操作系统原生窗口（非浏览器、非终端）；
  - 反复试跑仍无法稳定复现录屏步骤中的现象。

## 输出

最终回复只输出符合 schema 的 JSON：

- `script`：完整的录屏脚本内容（试跑通过的版本）；
- `est_seconds`：`run` 部分的预计时长（秒）；
- `manual_needed`：是否需要人工录屏；
- `reason`：需要人工录屏时写原因，否则写一句试跑结论（如「试跑 3 次通过，截图核对三步现象均出现」）。
