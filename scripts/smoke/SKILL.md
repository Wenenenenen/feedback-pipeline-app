---
name: smoke-echo
description: 流水线冒烟测试用 skill，按输入执行两条探测命令并把结果按 JSON 返回。
---

# smoke-echo

这是流水线冒烟测试，严格按以下步骤执行，不要做其他事情：

1. 执行输入中「探测命令」给出的命令，记录输出中的 node 版本号。
2. 读取输入中「探测文件」给出的 Windows 路径文件的第一行。
3. 最终回复只输出 JSON：
   - `skill`：固定为 `smoke-echo`
   - `stdin_text`：输入中「问候」一行的原文
   - `node_version`：第 1 步得到的版本号，失败则写错误信息
   - `unc_first_line`：第 2 步读到的第一行，失败则写错误信息
