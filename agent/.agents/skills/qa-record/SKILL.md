---
name: qa-record
description: 把验收给出的录屏步骤写成可自动执行的录屏脚本，在虚拟屏上试跑通过后返回，供流水线后台录屏。
---

# qa-record 生成自动录屏脚本

## 输入

`<stdin>` 中给出：题目与 User Prompt 原文、交付完整性与 Feedback、可录的证据、录屏前准备（已经执行完，服务已在后台运行）及其输出末尾、录屏步骤、录完收尾、录屏目录、可用端口、浏览器窗口尺寸、时长上限、录屏脚本的写入位置、试跑命令、试跑截图目录。

开始前先读本目录的 `AGENTS.md`（权限边界、WSL 命令写法）。

## 录屏环境

- 试跑与正式录制在两个地方，脚本同一份：
  - 试跑：WSL 里的虚拟屏（尺寸与本机主屏一致，见输入的「录屏环境」），浏览器是 WSL 里的 chromium，终端是按 Windows Terminal 配色的 xterm，文件选择框是 GTK 样式；没有真显卡，WebGL 走软件渲染、帧率偏低，WebGPU 多数情况不可用。
  - 正式录制（输入里写明是「Windows 本机桌面」时）：Windows 主屏上的本机 Chrome、Windows Terminal 里的 WSL、Windows 文件对话框，有真显卡，WebGL、WebGPU 正常；录的是整个主屏（含任务栏）和系统声音。输入写明正式录制也在虚拟屏时，环境与试跑相同。
- 产物服务在 WSL 里，两边都用 `http://localhost:<端口>` 打开。浏览器和终端各占满工作区（去掉任务栏的部分），同一时间只显示其中一个，终端当前目录就是录屏目录。
- 指针、键盘由流水线按人手的方式驱动（弧线、先快后慢、落点在元素中心附近随机，打字有快有慢），录的就是真实的指针与窗口。
- 网页可能有缩放（`devicePixelRatio` 不是 1），也可能和试跑时差几个像素：元素一律用选择器定位；必须点画布上的某个位置时，用页面坐标 `{x, y}` 并按画布的实际尺寸计算，不要写死试跑截图里量出来的屏幕坐标。
- 录屏要看起来是人在操作：画面上发生的每个动作都要由指针、键盘触发（点按钮、点刷新、在弹出的文件选择框里选文件），不能凭空发生。
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

- `ctx.page`：产物页面（Playwright Page），`ctx.context`：浏览器上下文；`run` 里只用它读页面状态、等待元素或文字出现，不用它做动作；
- `ctx.click(目标, {dx, dy, button, clickCount, hover})`：系统指针移过去再真实点击；目标可以是选择器字符串、Locator 或页面坐标 `{x, y}`；选择器目标的落点在中心附近随机，需要精确落点时给 `dx`、`dy`（相对中心）或页面坐标；点按钮、链接、画布、自己画的下拉菜单一律用它，不要用 `locator.click()` 或 `page.mouse`（那样画面上的指针不会动）；
- `ctx.select(目标, 选项)`：原生下拉框（`<select>`）选一项：点开下拉框，指针移到列表里的目标项上点击，与人一样；选项写 value 或显示文字，也可以写 `{value}`、`{label}`、`{index}`；没选中会抛错。原生下拉框一律用它，不要用 `ctx.click` 点开后按 `End`、`Home` 或回车（画面上会直接跳到目标项，不像人操作），也不要用 `selectOption`；
- `ctx.moveTo(目标, {dx, dy, ms})`：只移动指针（悬停提示、悬停高亮），落点规则同上；
- `ctx.type(目标, 文字)`：点中输入框后逐字输入，节奏有快有慢；超过 200 字的长文本与人一样直接粘贴；
- `ctx.key(按键)`：系统按键，xdotool 写法，如 `ctrl+a`、`Return`、`Delete`，发给当前窗口；
- `ctx.hold(按键, 毫秒或等待函数, {timeout 秒})`：按住按键一段时间再松开，如 `ctx.hold('Right', 1500)` 让角色向右走 1.5 秒；第二个参数也可以是等待函数，等页面上出现某个现象就松开，如 `ctx.hold('a', () => ctx.page.locator('#count').filter({ hasText: '1 个' }).waitFor())`，超过 `timeout`（默认 10）秒没出现会报错；与真键盘一样，按住约半秒后开始自动重复（keydown 的 repeat 为 true），按住期间再按别的键原来的键就不再重复；
- `ctx.keyDown(按键)`、`ctx.keyUp(按键)`：分开按下与松开，用于按住一个键的同时再按别的键（如按住 `Right` 时按一下 `j` 出招），按下的键一定要松开（脚本出错时执行器也会兜底松开）；
- `ctx.drag(起点, 终点, {button, ms, from})`：拖动，指针移到起点、按下、沿人手的轨迹移到终点、松开；起点、终点的写法同 `moveTo`（`from` 是起点的 `{dx, dy}`），终点可以是数组（依次经过多个点），也可以写 `{dx, dy}` 表示从当前位置按页面像素偏移，如 `ctx.drag('#view', { dx: 240, dy: 0 })` 在画布上横向拖动旋转视角；`button` 取 `left`、`right`、`middle`（默认 left），`ms` 是每段移动的时长；拖动画墙、框选、拖滑块、转视角都用它；
- `ctx.upload(目标, 文件路径)`：点上传按钮（`<input type=file>` 或会打开它的按钮、label），在弹出的系统文件选择框里选中文件；文件路径写录屏目录下的 WSL 路径，正式录制时流水线会先把它放进「下载」下以分支命名的文件夹，对话框打开在那里，指针直接移到文件上双击；页面要求一次多选一组文件（输入框带 `multiple`）时第二个参数传数组，如 `` [`${ctx.cwd}/.qa/g/a.png`, `${ctx.cwd}/.qa/g/manifest.json`] ``，正式录制时在文件列表里先点第一个、相邻的按住 Shift 点最后一个、不相邻的按住 Ctrl 逐个点，再回车，试跑（虚拟屏）时直接设置文件；同一次多选的文件名不能重复，多选不是人工录屏的理由。要选的文件必须是输入框 `accept` 允许的类型（对话框会按类型筛掉别的文件，列表里看不到就只能手打文件名，一看就假）；
- `ctx.download(目标)`：点下载按钮并等下载完成，返回 `{name, path}`；正式录制时 Chrome 按原文件名存进「下载」下以分支命名的文件夹，下载气泡显示文件名与格式图标。`path` 可以直接传给 `ctx.upload` 把下载的文件导回去，或传给 `ctx.openFile` 查看。下载一律用它，不要用 `page.waitForEvent('download')` 和 `download.saveAs`（正式录制时下载不经 Playwright，拿不到文件）；
- `ctx.openFile(文件, {pause})`：用系统程序查看下载的图片或文本：正式录制时在资源管理器里定位到文件，图片双击用系统看图打开，json、txt、csv 等文本右键「打开方式 → 记事本」打开，停留 `pause`（默认 2500）毫秒；文件写 `ctx.download` 的返回值或文件路径。看完调用 `ctx.closeFile()`（按 Alt+F4 关掉），下一个浏览器操作前也会自动关掉。要展示下载内容时一律用它，不要在浏览器地址栏里打开 `file://` 路径；试跑（虚拟屏）时在浏览器新标签页里打开，截图只用来核对流程；
- `ctx.console(表达式, {pause 毫秒, close})`：按 Ctrl+Shift+J 打开开发者工具的控制台，逐字输入一行表达式后回车，返回值停留 `pause`（默认 2500）毫秒后按 F12 关掉（`close: false` 时不关）；只用于录屏步骤里只读地读取精确像素（如 `getImageData`），表达式写成一行、不改页面状态，其他数值一律从页面上读；
- `ctx.reload()`：指针移到浏览器的刷新按钮点击，等页面加载完；`ctx.goto(网址)`：点地址栏、输入网址回车（超过 30 字的长网址粘贴，短网址逐字打），等页面加载完；都可以加 `{waitUntil: 'networkidle'}`；只用来打开产物页面，不用来查看下载的文件；
- `ctx.scroll(格数)`：在浏览器里滚动鼠标滚轮，正数向下；
- `ctx.term.run(命令, {timeout 秒, pause 毫秒})`：切到终端窗口，逐字打出命令并回车（超过 200 字的单行命令粘贴），等命令结束返回 `{code}`，命令失败不抛错；`ctx.term.show()` 只切到终端，`ctx.showBrowser()` 切回浏览器（之后的 click、moveTo 也会自动切回）；
- `ctx.exec(命令, {timeout 秒})`：在录屏目录后台执行命令（不出现在画面上），返回 `{code, output}`，只用来在脚本里核对状态；
- `ctx.step(序号, 说明)`：标记当前录到第几步，写进日志；
- `ctx.sleep(毫秒)`：停顿，让画面上的结果停留看清；
- `ctx.shot(名字)`：整块屏幕截图到试跑截图目录（含终端与指针；只在试跑时截，正式录制时跳过）；
- `ctx.cwd`：录屏目录的 WSL 路径，`ctx.dryRun`：是否试跑。

示例文件：

- 问题出在导入之后（导入的文件只是前置条件）时，在 `prepare` 里先加载好再开录，录屏直接从加载好的页面开始：页面有「加载示例」一类按钮就点它，没有就用 `` await ctx.page.setInputFiles('#file', `${ctx.cwd}/samples/demo.raw`) ``。
- 选文件、导入这一步本身就是问题现场时，在 `run` 里用 `ctx.upload`，录下弹出文件选择框选文件的过程：

```js
await ctx.upload('text=导入文件', `${ctx.cwd}/.qa/broken.wav`);
```

- 脚本里不要监听 `filechooser`（`waitForEvent('filechooser')`、`page.on('filechooser')`）：Playwright 会拦截，文件选择框就不弹出来了。

## 步骤

1. 读 Feedback 与录屏步骤，弄清每一步要在画面上展示的现象。录屏步骤是按人工录屏写的（Windows 浏览器、Windows 文件路径），换成上面的接口：打开页面用 `localhost`，文件路径换成录屏目录下的 WSL 路径，终端命令用 `ctx.term.run`（终端里已经在录屏目录下，命令用相对路径）。
2. 先在录屏目录里读产物代码，按页面上真实存在的元素写选择器（优先 `text=`、`role=`、`#id`），不要猜。
3. 写脚本到输入给出的录屏脚本位置，按「试跑命令」执行。试跑在同一块虚拟屏上进行，不录制；输出每行一个 JSON 事件，`error` 事件给出失败原因，出错时会在截图目录留下 `error.png`。
4. 在关键位置调用 `ctx.shot`，试跑后打开截图核对画面上确实出现了 Feedback 写的问题，不对就改脚本重新试跑，直到试跑成功且截图符合预期。
5. 试跑会改变服务与录屏目录的状态（上传过的文件、写过的数据），流水线正式录制前会重新克隆、重新准备，脚本不要依赖试跑留下的状态。

## 要求

- `run` 部分在时长上限减 5 秒内结束，按录屏步骤的顺序逐步执行，每步开头调用 `ctx.step`；每个现象出现后 `ctx.sleep(1500)` 到 `3000` 让画面停留，整体节奏不要太快也不要拖。
- 只录 Feedback 里写到的内容，失败项照录；不在录屏里跑临时脚本、注入控制台脚本或改页面状态，证据放在页面上的正常操作里。录屏步骤本身要求使用控制台脚本定位精确像素时，用 `ctx.console` 在画面上执行（不要用 `ctx.page.evaluate`，那样画面上看不到返回值）。
- 下载与查看下载内容：用 `ctx.download` 下载，要让画面看到下载的图片或 JSON 时用 `ctx.openFile` 在系统看图、记事本里打开；不要在浏览器里打开 `file://` 地址，也不要为了展示文件内容跑终端命令。
- `run` 里不用 `page.reload`、`page.goto`、`locator.click`、`locator.fill`、`selectOption`、`setInputFiles`、`page.keyboard`、`page.mouse`、`page.waitForEvent('download')` 做动作（画面上看不到对应的操作，一看就是脚本），分别换成 `ctx.reload`、`ctx.goto`、`ctx.click`、`ctx.type`、`ctx.select`、`ctx.upload`、`ctx.key`、`ctx.hold`、`ctx.drag`；也不要在脚本里直接调 `xdotool`（正式录制在本机桌面时没有 xdotool，会直接失败）；`prepare` 不录，可以随意用。
- 不调用 `page.pause`、不等待人工输入，不打开新的浏览器，不改动录屏目录里的已提交文件；脚本里不写死试跑生成的临时数据。
- 以下情况无法自动录屏，`manual_needed` 设为 true，`reason` 写清原因，`script` 给空字符串：
  - 正式录制也在虚拟屏、现象又依赖真显卡（如 WebGPU 渲染、软件渲染下帧率过低导致无法展示问题）且没有替代展示方式；正式录制在本机桌面时有真显卡，不按这条判人工：操作步骤照常写，试跑时看不到的渲染结果，相关的检查用 `if (!ctx.dryRun)` 包起来只在正式录制时做，`reason` 里注明哪些现象试跑时无法核对；
  - 需要摄像头、麦克风、手柄等外设，或需要操作系统原生窗口（非浏览器、非终端）；拖动、按住按键、边按住边按别的键都有接口，不是人工录屏的理由；
  - 反复试跑仍无法稳定复现录屏步骤中的现象。

## 输出

最终回复只输出符合 schema 的 JSON：

- `script`：完整的录屏脚本内容（试跑通过的版本）；
- `est_seconds`：`run` 部分的预计时长（秒）；
- `manual_needed`：是否需要人工录屏；
- `reason`：需要人工录屏时写原因，否则写一句试跑结论（如「试跑 3 次通过，截图核对三步现象均出现」）。
