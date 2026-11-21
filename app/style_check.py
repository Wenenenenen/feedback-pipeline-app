# -*- coding: utf-8 -*-
"""口吻规则检查：Feedback 走全量规则，prompt 只查 AI 化表达。"""
import re

# 书面化或 AI 化表达
BANNED_WORDS = [
    '首先', '其次', '再次', '最后总结', '综上', '综上所述', '总而言之', '总的来说', '值得注意的是',
    '需要注意的是', '需要指出的是', '不难发现', '显而易见', '毋庸置疑', '至关重要', '赋能',
    '一方面', '另一方面', '此外', '与此同时', '由此可见', '整体而言', '总体而言', '具体而言',
]

# prompt 专用：刻意模仿口语的情绪化、网络化词汇（只收不会和技术描述撞车的词，如不收「崩溃」）
PROMPT_SLANG = [
    '烦死', '累死', '气死', '愁死', '急死', '头大', '抓狂', '无语', '裂开', '破防', '心累',
    '绝了', '救命', '真香', '很爽', '太爽', '好爽', '爽死', '爽翻', '哈哈', '啊啊', 'yyds',
]

# prompt 专用：做题环境的套话，环境由流水线保证，不应写进 prompt
PROMPT_ENV_BOILERPLATE = ['从空仓库', 'ubuntu', 'wsl', 'docker', 'windows浏览器', 'windows 浏览器']
# prompt 开头的自然说法：我在做一个 我想做个 帮我做一个（帮我写 帮我用也算）
_PROMPT_OPENING = re.compile(r'^\s*(我在做|我想做|我想要做|帮我)')

CN_PUNCT = set('，。、；：？！“”‘’（）《》【】…—·「」『』〈〉﹏～￥')
# Feedback 允许的简单中文标点：逗号 句号 顿号 分号；其余中文标点仍不允许
FEEDBACK_PUNCT_OK = '，。、；'
# 任何位置都不允许的英文标点
ASCII_ALWAYS_BAD = set(',;!?()[]{}"\'`<>')
# 只允许出现在英文词内部的符号
ASCII_INNER_OK = set('./-_:+#%=@~')

CJK = r'一-鿿㐀-䶿'
# 英文或数字片段两侧都用空格与中文隔开（如「我点了 play 按钮」），说明至少一侧是多余的修饰空格；
# 只有一侧有空格时按断句处理（空格代替标点），不报错
_SPACE_CN_EN = re.compile(rf'[{CJK}] +[^\s{CJK}{FEEDBACK_PUNCT_OK}]+ +(?=[{CJK}])')
# 英文片段：允许的中文标点也是片段边界，src/a.py:82，这类写法不把逗号算进片段
_RUN = re.compile(rf'[^\s{CJK}{FEEDBACK_PUNCT_OK}]+')
_MD_LINE = re.compile(r'^\s*(#{1,6}\s|[-*+]\s|\d+[.)]\s|>\s|```|\|)')


def _excerpt(text: str, i: int, width: int = 12) -> str:
    return text[max(0, i - width): i + width].replace('\n', ' ')


def _issue(rule: str, text: str, i: int) -> dict:
    return {'rule': rule, 'excerpt': _excerpt(text, i)}


def check_banned(text: str) -> list[dict]:
    issues = []
    for w in BANNED_WORDS:
        i = text.find(w)
        if i >= 0:
            issues.append(_issue(f'禁用词 {w}', text, i))
    return issues


def check_feedback(text: str) -> list[dict]:
    """返回问题列表，空列表表示通过。"""
    issues: list[dict] = []
    if not text or not text.strip():
        return [{'rule': 'feedback 为空', 'excerpt': ''}]

    # markdown 结构
    pos = 0
    for line in text.split('\n'):
        if _MD_LINE.match(line) or '**' in line:
            issues.append(_issue('使用了 markdown', text, pos))
        pos += len(line) + 1

    # 中文标点（，。、；四种允许）
    for i, ch in enumerate(text):
        if ch in FEEDBACK_PUNCT_OK:
            continue
        if ch in CN_PUNCT or 0xFF01 <= ord(ch) <= 0xFF5E:
            issues.append(_issue(f'中文标点 {ch}', text, i))

    # 大写字母
    for m in re.finditer(r'[A-Z]', text):
        issues.append(_issue(f'大写字母 {m.group()}', text, m.start()))

    # 英文标点：逐个英文片段检查
    for m in _RUN.finditer(text):
        run, base = m.group(), m.start()
        has_alnum = any(c.isalnum() for c in run)
        for j, ch in enumerate(run):
            if ch.isalnum():
                continue
            if ch in ASCII_ALWAYS_BAD:
                issues.append(_issue(f'英文标点 {ch}', text, base + j))
            elif ch in ASCII_INNER_OK:
                if not has_alnum:
                    issues.append(_issue(f'独立的英文符号 {ch}', text, base + j))
                elif j == len(run) - 1 and ch in '.:':
                    # 词尾的句点或冒号属于句读
                    issues.append(_issue(f'词尾英文标点 {ch}', text, base + j))
            elif not ch.isalnum() and ord(ch) < 128:
                issues.append(_issue(f'英文符号 {ch}', text, base + j))

    # 中文与英文、数字之间的空格
    for m in _SPACE_CN_EN.finditer(text):
        issues.append(_issue('英文或数字两侧多加了空格', text, m.start()))

    issues.extend(check_banned(text))
    return _dedupe(issues)


# Feedback 专用：质检会直接打回的占位指代、审稿模板口吻和放之四海皆准的空话
FEEDBACK_BANNED = [
    '见feedback原文', '见原文', '见文件与行号', '见上文', '同上', '评审方', '验收方', '本轮任务',
    '原始轨迹', '验收结论', '整体表现不错', '整体表现良好', '表现优秀', '仍有提升空间', '有待提升',
    '有改进空间', '建议修复', '还需修复', '综合来看',
]

# Feedback 专用：千篇一律的固定开头，直接从最关键的事实写起
_FIXED_OPENING = re.compile(r'^\s*我让(模型|它|ai|claude)')

# Feedback 专用：工具链或 shell 环境噪声，质检认定不能算作模型的过程问题
TOOLCHAIN_NOISE = ['git bash', 'gitbash', 'msys', '路径转换']
# Feedback 篇幅上限：写透一两个关键问题即可，与 style.md「篇幅与取舍」一致
FEEDBACK_MAX_CHARS = 400

# 代码锚点：带扩展名的文件（可带路径与行号），如 src/parser.py:82 wav.js
_CODE_ANCHOR = re.compile(r'[a-z0-9_\-/]*[a-z0-9_]\.[a-z][a-z0-9]{0,5}(?::\d+)?(?![a-z0-9])')
# 原分依据：结尾写明「给N分」
_SCORE_STMT = re.compile(r'给\s?([1-5])\s?分')


def check_feedback_content(text: str, score=None) -> list[dict]:
    """内容门禁：原分依据、代码锚点、占位指代与模板空话；与口吻检查分开，便于单独测试。"""
    text = text or ''
    if not text.strip():
        return []
    issues = []
    low = text.lower()
    for w in FEEDBACK_BANNED:
        i = low.find(w)
        if i >= 0:
            issues.append(_issue(f'质检会打回的表达 {w}', text, i))
    if _FIXED_OPENING.match(low):
        issues.append({'rule': '固定开头 不要以我让模型做某事开头 直接从最关键的事实写起',
                       'excerpt': text.strip()[:24]})
    for w in TOOLCHAIN_NOISE:
        i = low.find(w)
        if i >= 0:
            issues.append(_issue(f'工具链噪声 {w} 不能算作模型的过程问题', text, i))
    stmts = _SCORE_STMT.findall(text)
    if not stmts:
        issues.append({'rule': '没有写原分依据 结尾要写明为什么给几分', 'excerpt': text[-24:]})
    elif score not in (None, '') and stmts[-1] != str(int(score)):
        m = list(_SCORE_STMT.finditer(text))[-1]
        issues.append(_issue(f'文中给{stmts[-1]}分 与评分{int(score)}分不一致', text, m.start()))
    if not _CODE_ANCHOR.search(low):
        issues.append({'rule': '没有代码锚点 至少写到具体文件 函数或行号', 'excerpt': text[:24]})
    n = len(text.strip())
    if n > FEEDBACK_MAX_CHARS:
        issues.append({'rule': f'篇幅{n}字 超过{FEEDBACK_MAX_CHARS}字 只写最关键的一两个问题',
                       'excerpt': text[FEEDBACK_MAX_CHARS - 12:FEEDBACK_MAX_CHARS + 12]})
    return issues


# 过程问题的写法：要写模型在过程中做了什么，不写成分析轨迹得到的内容
_TRAJ_ANALYSIS = ['轨迹第', '完整轨迹', '原轨', '轨迹物理']
# 「过程上……。产物上……」是固定模板，篇篇一样会显得模板化
_TEMPLATE_PAIR = ('过程上', '产物上')


def check_feedback_voice(text: str) -> list[dict]:
    """过程写法检查：不写轨迹分析口吻，不套过程上 产物上的固定模板。

    只用于新写的 Feedback（验收与自动查验）；返修只按质检意见改，不因这条重写已提交的内容。
    """
    text = text or ''
    issues = []
    for w in _TRAJ_ANALYSIS:
        i = text.find(w)
        if i >= 0:
            issues.append(_issue(f'过程写成了轨迹分析 {w} 改成模型在过程中做了什么 行号最多带一个且不写轨迹二字', text, i))
            break
    if all(w in text for w in _TEMPLATE_PAIR):
        issues.append(_issue('套用了过程上 产物上的固定模板 过程那句直接写模型做了什么 产物那句落到文件和代码',
                             text, text.find(_TEMPLATE_PAIR[0])))
    return issues


def check_prompt(text: str) -> list[dict]:
    """prompt 允许正常标点与代码，只查 AI 化表达。"""
    # 除 AI 化表达外，prompt 还要拦截刻意模仿口语的情绪化词汇
    text = text or ''
    issues = check_banned(text)
    lowered = text.lower()
    for w in PROMPT_SLANG:
        i = lowered.find(w)
        if i >= 0:
            issues.append(_issue(f'刻意口语 {w}', text, i))
    for w in PROMPT_ENV_BOILERPLATE:
        i = lowered.find(w)
        if i >= 0:
            issues.append(_issue(f'环境套话 {w} 不用写进prompt', text, i))
    if text.strip() and not _PROMPT_OPENING.match(text):
        issues.append({'rule': '开头不自然 应以我在做一个 我想做个 帮我做一个开头', 'excerpt': text.strip()[:24]})
    return _dedupe(issues)


# 只起包裹作用的符号：反引号、中文引号与书名号，去掉不改变意思
_WRAP_MARKS = re.compile(r'[`“”‘’「」『』《》]')
# 函数名后的空括号，style.md 要求函数名不带括号
_EMPTY_CALL = re.compile(r'([A-Za-z0-9_])\(\)')
# 英文或数字片段两侧都用空格与中文隔开时，去掉两侧空格（与 _SPACE_CN_EN 的判定一致）
_SPACE_CN_EN_FIX = re.compile(rf'([{CJK}]) +([^\s{CJK}{FEEDBACK_PUNCT_OK}]+) +(?=[{CJK}])')
# 命令里的 shell 噪声：环境变量前缀、重定向、截取输出的管道，去掉不影响说的是哪条命令
_SHELL_NOISE = [
    re.compile(r'(?<![A-Za-z0-9_])[A-Z][A-Z0-9_]+=(?:\$\([^)]*\)|\S+)\s+'),  # NODE_PATH=$(npm root -g) 前缀
    re.compile(r'\s*\d?>\s*/dev/null'),                                       # >/dev/null
    re.compile(r'\s*\d?>&\d'),                                                # 2>&1
    re.compile(r'\s*\|\s*(?:tail|head)(?:\s+-n)?\s+-?\d+'),                   # | tail -3
]
# 数字之间的破折号或波浪号表示范围，换成「至」
_RANGE_DASH = re.compile(r'(?<=\d)\s*[—–～]+\s*(?=\d)')
# 两侧都是中文的斜杠表示并列，换成顿号；两侧都有空格的独立斜杠换成空格断句
_CJK_SLASH = re.compile(rf'(?<=[{CJK}])\s*/\s*(?=[{CJK}])')
# 独立的省略号（不连着英文或数字），如 vitest run ... 和
_LONE_ELLIPSIS = re.compile(r'(?<![A-Za-z0-9_])\.{3}(?![A-Za-z0-9_])|…+')


def normalize_feedback(text: str) -> str:
    """机械规整 Feedback 的格式问题，只改字不改意思。

    三方模型验收时常忽略 style.md 的格式要求（反引号包代码、英文大写、中文引号、英文两侧加空格），
    这些都能确定性地改掉；改不掉的问题（如命令里的管道符、括号）留给自动查验拦下。
    命令里的环境变量前缀、重定向、截取输出的管道，报错原文里的英文括号和引号，数字范围的破折号，
    中文之间的斜杠和独立的省略号，也都按固定规则改掉；超长、禁用词等需要改写的留给模型自查和查验。
    """
    if not text:
        return text
    s = text
    # 先去掉 shell 噪声（大小写转换之前，环境变量前缀靠大写识别）
    for p in _SHELL_NOISE:
        s = p.sub('', s)
    # 命令之间的 && 换成「和」
    s = re.sub(r'\s*&&\s*', '和', s)
    # 反引号与中文之间的修饰空格先去掉，再去掉反引号本身
    s = re.sub(rf'(?<=[{CJK}{FEEDBACK_PUNCT_OK}]) +(?=`)', '', s)
    s = re.sub(rf'(?<=`) +(?=[{CJK}{FEEDBACK_PUNCT_OK}])', '', s)
    s = _WRAP_MARKS.sub('', s)
    s = _EMPTY_CALL.sub(r'\1', s)
    # 英文引号直接去掉，英文括号换成空格断句，如 undefined (reading 'length') 写成 undefined reading length
    s = re.sub(r'[\'"]', '', s)
    s = re.sub(r'[()]', ' ', s)
    s = _RANGE_DASH.sub('至', s)
    s = _CJK_SLASH.sub('、', s)
    s = re.sub(r'(?<=\S) +/ +(?=\S)', ' ', s)
    s = _LONE_ELLIPSIS.sub(' ', s)
    # 中点分隔符换成空格断句
    s = s.replace('·', ' ')
    # 英文全部小写
    s = re.sub(r'[A-Z]', lambda m: m.group().lower(), s)
    # 反复替换直到稳定：相邻片段共用中间的中文字符，一次替换会漏掉后一个
    while True:
        t = _SPACE_CN_EN_FIX.sub(r'\1\2', s)
        if t == s:
            break
        s = t
    # 标点前后不加空格，连续空格合并
    s = re.sub(rf' +(?=[{FEEDBACK_PUNCT_OK}])', '', s)
    s = re.sub(rf'(?<=[{FEEDBACK_PUNCT_OK}]) +', '', s)
    s = re.sub(r' {2,}', ' ', s)
    return s.strip()


def _dedupe(issues: list[dict]) -> list[dict]:
    seen, out = set(), []
    for it in issues:
        key = (it['rule'], it['excerpt'])
        if key not in seen:
            seen.add(key)
            out.append(it)
    return out


def check_all(text: str, score=None, voice: bool = True) -> list[dict]:
    """Feedback 的全部门禁：口吻格式、内容，验收时再加过程写法；后端查验与模型自查共用。"""
    if not text:
        return []
    out = check_feedback(text) + check_feedback_content(text, score)
    return out + check_feedback_voice(text) if voice else out


def _cli(argv: list[str]) -> int:
    """模型自查用：python style_check.py <feedback 文件> [分数] [--rework]

    文件只放 Feedback 正文（utf-8，带不带 bom 都行）；结果同时写到同目录的 feedback-check.txt，
    避免 Windows 终端转码把中文显示成乱码。返回 0 表示通过。
    """
    import pathlib
    args = [a for a in argv if not a.startswith('--')]
    if not args:
        print('用法 python style_check.py <feedback 文件> [分数] [--rework]')
        return 2
    path = pathlib.Path(args[0])
    score = args[1] if len(args) > 1 and args[1].isdigit() else None
    text = path.read_text(encoding='utf-8-sig').strip()
    issues = check_all(text, score, voice='--rework' not in argv)
    lines = [f'字数 {len(text)}']
    if issues:
        lines.append(f'不通过 共{len(issues)}处 改完再查')
        lines += [f"- {i['rule']}「{i['excerpt']}」" for i in issues]
        fixed = normalize_feedback(text)
        if fixed != text:
            # 机械规整后的写法给模型参考，内容问题仍要自己改
            lines += ['', '格式可以这样规整（只改字不改意思）', fixed]
    else:
        lines.append('通过')
    out = '\n'.join(lines)
    path.with_name('feedback-check.txt').write_text(out + '\n', encoding='utf-8')
    print(out)
    return 1 if issues else 0


if __name__ == '__main__':
    import sys
    sys.exit(_cli(sys.argv[1:]))
