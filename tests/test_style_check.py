# -*- coding: utf-8 -*-
from app.style_check import check_feedback, check_feedback_content, check_prompt

GOOD = ('prompt要求断流重连后观众端不断开 我在obs里停推再开 flv.js页面直接报mediaerror卡死 '
        '看了下server/relay.go里断流时直接close了所有订阅者的conn 根本没做等待重连 '
        '过程上模型写完relay.go只跑了go build就说完成了 没有真的推流测过一次')


def rules(text):
    return [i['rule'] for i in check_feedback(text)]


def test_good_text_passes():
    assert check_feedback(GOOD) == []


def test_inner_symbols_allowed():
    assert check_feedback('跑src/utils/date.py报valueerror 版本v22.23.2 响度-16lufs 端口localhost:20001') == []


def test_chinese_punctuation():
    # 只允许，。、；四种简单中文标点，其余中文标点仍拦下
    assert check_feedback('页面白屏，控制台报错；刷新、重启都没用。') == []
    assert check_feedback('最终提交里src/parser.py:82，parse_response没判空。') == []
    for bad in '：？！“（《【…':
        assert any('中文标点' in r for r in rules(f'页面白屏{bad}控制台报错')), bad


def test_uppercase():
    assert any('大写字母' in r for r in rules('报ValueError了'))


def test_trailing_period_and_comma():
    assert any('词尾英文标点' in r for r in rules('最后跑了npm.'))
    assert any('英文标点 ,' in r for r in rules('页面白屏,控制台报错'))


def test_brackets_and_quotes():
    assert any('英文标点 (' in r for r in rules('调用parse(x)报错'))


def test_space_between_cn_and_en():
    # 两侧都加空格属于修饰空格
    assert any('空格' in r for r in rules('我点了 play 按钮'))
    assert any('空格' in r for r in rules('延迟大概 3 秒'))
    # 一侧的空格是断句，放行
    assert check_feedback('页面卡住 npm run dev也起不来') == []
    assert check_feedback('页面卡了 3秒后才出画面') == []


def test_markdown():
    assert any('markdown' in r for r in rules('- 第一点\n- 第二点'))
    assert any('markdown' in r for r in rules('这里**很差**'))


def test_banned_words():
    assert any('首先' in r for r in rules('首先模型没读文件'))


def test_toolchain_noise_flagged():
    """git bash 路径转换这类工具链噪声不能算作模型的过程问题，质检会打回。"""
    text = GOOD + ' 模型看到请求异常就说是git bash路径转换导致的 给2分 src/app.js'
    found = [i['rule'] for i in check_feedback_content(text, 2)]
    assert any('工具链噪声' in r for r in found)
    assert not any('工具链噪声' in i['rule'] for i in check_feedback_content(GOOD + ' 给2分 src/app.js', 2))


def test_prompt_only_checks_banned_words():
    assert check_prompt('帮我用 Go 写一个 RTMP 服务，延迟 2 秒内。') == []
    assert check_prompt('值得注意的是，要支持多房间。')


def test_prompt_rejects_forced_slang():
    assert any('烦死' in i['rule'] for i in check_prompt('每次手动导数据烦死了，帮我写个工具。'))
    assert any('很爽' in i['rule'] for i in check_prompt('做完用起来很爽就行。'))
    assert any('yyds' in i['rule'] for i in check_prompt('ffmpeg YYDS，就用它。'))
    # 技术描述里的「崩溃」不算刻意口语
    assert check_prompt('帮我做个服务，进程崩溃后要能自动续跑。') == []


def test_prompt_opening_and_env_boilerplate():
    """开头要像真实用户直接说要做什么；做题环境的套话不应写进 prompt。"""
    for ok in ('我在做一个剖面工具，', '我想做个直播推流服务，', '帮我做一个对轨工具，'):
        assert check_prompt(ok + '要支持多房间。') == [], ok
    rules = [i['rule'] for i in check_prompt('我负责树脂打印模型的出厂检查，需要看清空腔。')]
    assert any('开头不自然' in r for r in rules)
    text = ('我想做个剖面工具。请从空仓库做本地网页，用Python在Ubuntu-24.04的WSL启动，'
            'Windows浏览器访问localhost，不用Docker。')
    rules = [i['rule'] for i in check_prompt(text)]
    for w in ('从空仓库', 'ubuntu', 'wsl', 'docker', 'windows浏览器'):
        assert any(f'环境套话 {w}' in r for r in rules), w
    # 离线这类属于需求本身的约束可以写
    assert check_prompt('帮我做一个现场用的工具，现场没网，装好以后要能离线用。') == []


def content_rules(text, score=None):
    return [i['rule'] for i in check_feedback_content(text, score)]


def test_feedback_content_passes_when_complete():
    text = ('模型修改后只跑了正常路径 没有运行tests/test_error_handling.py 最终提交里src/parser.py:82的'
            'parse_response遇到空响应直接调用response.json 批量导入在该条记录处整体中止 问题仍在 给3分')
    assert check_feedback_content(text, 3) == []
    assert check_feedback(text) == []


def test_feedback_content_requires_score_and_anchor():
    rs = content_rules('导入功能仍会报错 错误处理不完整 建议修复', 3)
    assert any('原分依据' in r for r in rs)
    assert any('代码锚点' in r for r in rs)
    assert any('建议修复' in r for r in rs)
    # 文中分数与评分不一致
    assert any('不一致' in r for r in content_rules('wav.js没校验数据长度 给4分', 3))


def test_feedback_length_limit():
    """超过 400 字的 Feedback 要求精简；400 字以内不拦。"""
    base = ('模型修改后只跑了正常路径 没有运行tests/test_error_handling.py 最终提交里src/parser.py:82的'
            'parse_response遇到空响应直接调用response.json 批量导入在该条记录处整体中止 问题仍在 ')
    ok = base + '给3分'
    assert not any('篇幅' in r for r in content_rules(ok, 3))
    long = base * 4 + '给3分'
    assert len(long) > 400 and any('篇幅' in r for r in content_rules(long, 3))


def test_feedback_content_rejects_template_phrases():
    rs = content_rules('评审方实测main.go能跑 整体表现不错 但仍有提升空间 给4分', 4)
    assert any('评审方' in r for r in rs) and any('整体表现不错' in r for r in rs)
    # 版本号和小数不算代码锚点
    assert any('代码锚点' in r for r in content_rules('node版本v22.23 误差0.08毫秒 给5分', 5))


def test_feedback_fixed_opening_flagged():
    """不要每篇都以我让模型做某事开头；正文里出现我让模型不拦。"""
    text = ('我让模型做局部频谱修复 最终提交里dsp.js:36的decodewav截短data块不报错 '
            '模型只测了非音频导入 问题仍在 编辑和导入校验还需修复 给3分')
    rs = content_rules(text, 3)
    assert any('固定开头' in r for r in rs) and any('还需修复' in r for r in rs)
    ok = ('模型只测了非音频导入 最终提交里dsp.js:36的decodewav截短data块不报错 '
          '截断文件也被接收 问题仍在 给3分')
    assert content_rules(ok, 3) == []


def test_normalize_feedback_fixes_format_only():
    """三方模型常见的格式问题机械规整后通过口吻检查，意思不变。"""
    from app.style_check import normalize_feedback
    raw = ('产物上，`decal-core.js:35` 的 `buildDecals()` 没有校验，奇异worldToBox导入后页面显示“32F · E=1 · 环1”，'
           '我点了 Play 按钮也没用，给3分。')
    fixed = normalize_feedback(raw)
    assert check_feedback(fixed) == []
    assert fixed == ('产物上，decal-core.js:35的builddecals没有校验，奇异worldtobox导入后页面显示32f e=1 环1，'
                     '我点了play按钮也没用，给3分。')
    # 已合规的文字不变
    assert normalize_feedback(GOOD) == GOOD


def test_normalize_feedback_keeps_unfixable_issues():
    """命令里过滤输出的管道等改不掉的问题保留，交给模型自查和自动查验拦下。"""
    from app.style_check import normalize_feedback
    fixed = normalize_feedback('运行`node a.js | grep ok`返回ALL PASSED')
    assert 'all passed' in fixed
    assert any('英文符号' in r or '英文标点' in r for r in rules(fixed))


def test_normalize_feedback_cleans_shell_and_error_text():
    """三方模型照抄的命令与报错原文：重定向、截取输出的管道、&&、环境变量前缀、括号引号都能规整掉。"""
    from app.style_check import normalize_feedback
    raw = ('收尾只执行 `node tests/planner.test.js >/dev/null 2>&1 && NODE_PATH=$(npm root -g) node tests/e2e.ui.js`，'
           '又跑了`node tools/e2e_smoke.js 2>&1 | tail -3`，返回 `ALL E2E CHECKS PASSED`。'
           '`js/app.js:392` 的 `renderEventEditor` 触发 `Cannot read properties of undefined (reading \'length\')`，'
           '第1269—1270行构建，浏览器测试输出33 / all e2e passed，绘制/擦除也验了，vitest run ...、构建都过了，给4分。')
    fixed = normalize_feedback(raw)
    assert check_feedback(fixed) == []
    assert 'node tests/planner.test.js和node tests/e2e.ui.js' in fixed
    assert 'node tools/e2e_smoke.js，' in fixed
    assert 'rendereventeditor触发cannot read properties of undefined reading length，' in fixed
    assert '第1269至1270行' in fixed and '绘制、擦除' in fixed


def test_voice_flags_trajectory_analysis_and_template():
    """过程写成轨迹分析、套过程上 产物上模板都会被拦下；以模型起句的写法通过。"""
    from app.style_check import check_feedback_voice
    old = ('过程上，轨迹第199行运行node test/verify.js，第200行返回all pass。'
           '产物上，最终提交的sim.js:43在向上取整前减去1e-9，给4分。')
    found = [i['rule'] for i in check_feedback_voice(old)]
    assert any('轨迹分析' in r for r in found)
    assert any('固定模板' in r for r in found)
    new = ('模型没有在页面上编辑临界窗口再看理想层和请求日志，它只跑了固定样例的两段验证脚本就回复全部通过，'
           '这个漏验让层级错误留在了交付里。最终提交的sim.js:43在向上取整前减去1e-9，给4分。')
    assert check_feedback_voice(new) == []


def test_style_md_examples_pass_all_checks():
    """style.md 里的示例本身要能通过后端全部门禁，否则模型照着写也会被拦下。"""
    import pathlib
    import re
    from app.style_check import check_all
    md = (pathlib.Path(__file__).resolve().parents[1] / 'agent' / 'style.md').read_text(encoding='utf-8')
    examples = re.findall(r'^(\d) 分：\n(.+)$', md, re.M)
    assert len(examples) >= 3
    for score, text in examples:
        assert check_all(text, score) == [], (score, check_all(text, score))


def test_cli_writes_result_file(tmp_path):
    """模型自查命令：结果写到同目录 feedback-check.txt，通过返回 0，不通过返回 1 并列出问题。"""
    from app.style_check import _cli
    f = tmp_path / 'feedback.txt'
    f.write_text('﻿模型只跑了go build就回复说已完成，从没推流再断流测过。server/relay.go断流时直接close了订阅者，给2分。',
                 encoding='utf-8')
    assert _cli([str(f), '2']) == 0
    assert '通过' in (tmp_path / 'feedback-check.txt').read_text(encoding='utf-8')
    f.write_text('过程上，轨迹第3行跑了`go build`。产物上，server/relay.go有问题，给2分。', encoding='utf-8')
    assert _cli([str(f), '2']) == 1
    out = (tmp_path / 'feedback-check.txt').read_text(encoding='utf-8')
    assert '不通过' in out and '固定模板' in out
    # 返修模式不查过程写法，只剩格式问题
    assert _cli([str(f), '2', '--rework']) == 1
    assert '固定模板' not in (tmp_path / 'feedback-check.txt').read_text(encoding='utf-8')
