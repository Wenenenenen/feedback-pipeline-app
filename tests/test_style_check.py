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
    assert any('中文标点' in r for r in rules('页面白屏，控制台报错'))


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
    assert check_prompt('服务进程崩溃后要能自动续跑。') == []


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


def test_feedback_content_rejects_template_phrases():
    rs = content_rules('评审方实测main.go能跑 整体表现不错 但仍有提升空间 给4分', 4)
    assert any('评审方' in r for r in rs) and any('整体表现不错' in r for r in rs)
    # 版本号和小数不算代码锚点
    assert any('代码锚点' in r for r in content_rules('node版本v22.23 误差0.08毫秒 给5分', 5))
