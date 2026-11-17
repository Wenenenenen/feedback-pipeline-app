# 飞书表格填写工具

输入飞书链接，读取真实字段，填写并预览，确认后提交。既可以在终端交互使用，也可以在 Node.js 程序中调用。工具沿用当前用户已配置的 `lark-cli` 登录状态，无需在代码中填写 App Secret 或 token。

## 首次使用：安装并配置飞书 CLI

本工具依赖 **Node.js 22+（包含 npm/npx）** 和 **已配置、已登录的飞书 CLI**。换到另一台电脑或运行环境时，需要在该环境完成以下步骤。通过 npm 安装 CLI 不需要 Go 或 Python。

### 1. 检查 Node.js 和 npm

在终端运行：

```bash
node --version
npm --version
```

Node.js 主版本应为 22 或更高。若提示命令不存在，请先安装 Node.js 22+，重新打开终端后再检查。

### 2. 安装飞书 CLI

```bash
npm install -g @larksuite/cli
lark-cli --version
```

第二条命令能显示版本号即表示 CLI 安装成功。本工具已与 `lark-cli 1.0.100` 验证。

如果还需要让 AI Agent 操作飞书，在该 Agent 的项目目录中安装官方配套 Skill：

```bash
npx -y skills add https://open.feishu.cn --skill -y
```

此 Skill 供 AI Agent 使用；本工具通过 Node.js 调用 CLI，运行本工具时不需要加载 Skill。

### 3. 配置飞书应用

首次配置运行：

```bash
lark-cli config init --new
```

终端会显示配置链接和二维码。保持终端运行，打开链接或用飞书扫码，在浏览器中完成应用配置；完成后等待终端显示配置成功。已配置的机器可以跳过此步，无需重复创建应用。

### 4. 登录并授权

```bash
lark-cli auth login --recommend
```

打开终端显示的授权链接，或使用飞书扫码，登录**具有目标表格填写权限的账号**并确认授权。保持终端运行，直到显示授权完成。链接过期时重新运行登录命令。

### 5. 验证登录状态

```bash
lark-cli auth status --json --verify
lark-cli whoami --as user
```

确认用户身份的 `identities.user.status` 为 `ready`、`identities.user.tokenStatus` 为 `valid`，并且验证结果 `verified` 为 `true`；`whoami` 显示的姓名应是预期填写账号。只有应用身份就绪、用户身份未登录时，还不能代表用户填写其表格。

若终端找不到 `lark-cli`，先重新打开终端，再检查安装位置：

```bash
npm prefix -g
command -v lark-cli
```

在 macOS/Linux 中，可执行文件通常在 npm 全局目录的 `bin` 子目录。将该目录加入 PATH，或通过 `LARK_CLI_BIN` 指定 `lark-cli` 的完整路径。本机路径为 `/opt/homebrew/bin/lark-cli`。

CLI 的安装和授权还可参考[飞书官方指南](https://open.larkoffice.com/document/mcp_open_tools/feishu-cli-let-ai-actually-do-your-work-in-feishu.md)。

## 直接开始

当前这台 Mac 已完成上面的安装、配置和授权，可以直接启动。**本工具本身没有额外 npm 依赖，无需在工具目录执行 `npm install`。**

在 Finder 中双击 **启动填写.command**。也可以在本目录打开终端：

```bash
npm start
```

然后按提示：

1. 粘贴表格链接。回车会使用 `config.json` 中你提供的示例表。
2. 如有多张数据表或子表，选择目标。
3. 对多维表格选择“新增”或“更新”。更新需要真实的 `recordId`。
4. 选择要填写的字段，逐项输入。普通电子表格填写 A1 范围和二维 JSON。
5. 查看实际提交目标和值。输入 **提交** 才会写入，回车取消。

多维表格的文本、数字、单选、多选、日期、复选框、人员、群聊、关联、位置和附件字段均有对应值处理。公式、自动编号、创建/修改人、创建/修改时间、查找引用及按钮字段不提供填写入口。

## 常见输入

| 类型 | 输入方式 |
| --- | --- |
| 文本 | 直接输入；长文本输入 `:file /绝对路径/feedback.txt` |
| 数字 | `4`、`12.5`，评分会校验配置的上下限 |
| 单选/多选 | 选项编号，如 `1` 或 `1,3`；含逗号的选项名使用 JSON 数组 |
| 日期 | `2026-11-16` 或 `2026-11-16T10:30:00+08:00` |
| 复选框 | `true` / `false` 或 `是` / `否` |
| 人员 | `:me` 填当前用户，或 `[{"id":"真实的ou_用户ID"}]` |
| 关联/群聊 | `[{"id":"真实的rec记录ID"}]` / `[{"id":"真实的oc_群聊ID"}]` |
| 位置 | `{"lng":116.397428,"lat":39.90923}`，须使用真实坐标 |
| 附件 | 本地路径，或 `["/路径/report.pdf","/路径/photo.png"]` |
| 跳过 | 直接回车；更新时不会改动该字段 |
| 清空 Base 普通字段 | `:null`，字段本身需允许清空；附件不支持此操作 |

`:file` 后的路径不加外层引号；需要输入字面量 `:null` 时使用 `::null`。

## 在程序中调用

新建 `my-program.mjs`，与本工具文件夹放在同一目录，使用相对路径导入：

```javascript
import { createTableTool } from './feishu-table-tool/lib/index.mjs';

const tool = createTableTool();
const url = '你的飞书表格完整链接';

// 只读：查看数据表、字段 ID、字段类型和选项。
const info = await tool.inspect(url);
console.log(info);

// 只读：用目标表真实存在的字段构造计划。
const plan = await tool.prepare({
  url,
  tableId: info.tableId, // 多表时显式指定
  action: 'create',
  records: [{
    fields: { '真实文本字段名': '实际业务内容' },
    attachments: {},
  }],
});

console.log(plan.summary, plan.request);

// 由你的程序在用户明确确认后执行：
// const receipt = await tool.submit(plan, { confirm: true });
```

`prepare` 和 `inspect` 不写远端数据。`submit` 必须接收 `prepare` 返回的完整计划，并显式提供 `confirm: true`。提交时会重新读取字段；计划、字段定义或附件文件变化时，要求重新预览。支持可选配置：`createTableTool({cliPath, profile, timeout})`，timeout 单位为毫秒，默认 120000。

### 多维表格请求

新增：

```json
{
  "url": "你的Base链接",
  "tableId": "真实tbl数据表ID",
  "action": "create",
  "records": [
    {
      "fields": {"真实文本列名": "内容", "真实单选列名": ["现有选项"]},
      "attachments": {"真实附件列名": ["/本地路径/报告.pdf"]}
    }
  ]
}
```

更新已有记录：改为 `"action":"update"`，每条记录增加 `"recordId":"真实rec记录ID"`，只放需要修改的字段。工具先检查记录存在且当前用户可读。一次最多 200 条，同一表的多批请求应串行执行。

### 普通电子表格请求

```json
{
  "url": "你的电子表格链接",
  "sheetId": "inspect返回的真实子表ID",
  "action": "cells",
  "range": "A2:C2",
  "values": [["00123", 95, true]],
  "allowOverwrite": false
}
```

`values` 是二维数组。字符串按文本写入，保留前导零；数字和布尔保持原类型。`null` 表示跳过该格，**不会清空**。多选单元格可传数组，例如 `[[["A","B"]]]`。自定义数字格式可传 `[[{"value":0.4,"format":"0.0%"}]]`。不写入公式或修改表结构。默认遇非空单元格拒写；需要覆盖时由调用方明确设置 `allowOverwrite:true`。写入后返回目标范围的 `readback`，供核对实际结果，不把“接口接受”冒充“业务校验通过”。

### 分享表单请求

```json
{
  "url": "https://你的域名/share/base/form/真实分享token",
  "action": "submit-form",
  "fields": {"真实题目标题": "实际回答"},
  "attachments": {}
}
```

会先读取题目、必填项和显示条件。隐藏题不能填写；不支持的条件会停止提交。表单的高风险确认参数仅在调用方明确确认后传递给 CLI。

## JSON 文件与命令行

在工具目录运行，下列命令中的链接替换为目标表：

```bash
# 查看真实字段
node bin/feishu-fill.mjs inspect '完整表格链接'

# 生成空白模板及字段指南；多表可加 --table 或 --sheet
node bin/feishu-fill.mjs template '完整表格链接' --output my-job.json

# 编辑 my-job.json：只把要写的真实字段放入 fields / attachments
# _guide 是说明，不会被提交
node bin/feishu-fill.mjs preview --input my-job.json

# 明确确认当前文件内容可提交；不带 --commit 只预览
node bin/feishu-fill.mjs submit --input my-job.json --commit --output receipt.json
```

所有 `--output` 文件都拒绝覆盖已有文件。附件相对路径相对于启动工具时的当前目录解析。多维表格模板默认 `fields:{}`，避免误把示例值写入真实表；填写前可参照 `_guide`。`examples/job.json` 是你提供的表的空白请求，`examples/verified-schema.json` 是只读读取的字段快照，每次使用仍会重新获取真实字段。

如果希望使用简短命令，可选执行 `npm link`，之后使用 `feishu-fill fill '表格链接'`。无需注册 npm 账号或发布软件包。

## 权限与执行结果

能填写的范围是 **当前身份有相应资源编辑权限，且飞书开放接口允许写入的表**。浏览器可填写不一定代表 API 已开放，例如分享范围、租户政策、字段权限或仅允许收集表单提交的资源可能不同。工具不扩大权限，不切换成机器人绕过错误。

Base 字段的 `required` 只根据 API 元数据判断；描述文字、公式中的业务必填规则仍由表本身判断。你提供的表包含“格式校验”公式，这一列由飞书计算，工具不会覆盖，也不会仅凭 API 成功声称该业务规则已通过。动态单选的最终可选范围由服务端检查；同名字段、未知字段类型和复杂的未支持条件会明确报错。

附件使用本地文件，保留文件名；每字段 1–50 个文件、单文件不超过 2GB。现有附件只追加，不移除。创建记录与后续附件上传不是一个原子事务。如果附件失败，错误中的 `completed` 会保留已创建记录 ID 等回执。超时或部分成功时不要直接重跑新增，否则可能重复记录；先查看表内状态，再只补缺失步骤。工具不自动重试写入。

程序需在当前 macOS 用户下运行并能访问钥匙串。若 IDE/自动化沙箱报钥匙串不可用，请从普通终端运行，或调整该运行环境的权限。工具没有复制登录凭证，也没有降低钥匙串的保护。服务器、容器和另一台电脑需要在其运行环境单独安装并配置 CLI。可用 `LARK_CLI_BIN=/opt/homebrew/bin/lark-cli` 指定本机路径。

## 验证

```bash
npm test
```

测试使用隔离的模拟 CLI，不向飞书写入。真实表的验证范围详见 `VALIDATION.md`。
