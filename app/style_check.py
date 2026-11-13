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

CN_PUNCT = set('，。、；：？！“”‘’（）《》【】…—·「」『』〈〉﹏～￥')
# 任何位置都不允许的英文标点
ASCII_ALWAYS_BAD = set(',;!?()[]{}"\'`<>')
# 只允许出现在英文词内部的符号
ASCII_INNER_OK = set('./-_:+#%=@~')

CJK = r'一-鿿㐀-䶿'
# 英文或数字片段两侧都用空格与中文隔开（如「我点了 play 按钮」），说明至少一侧是多余的修饰空格；
# 只有一侧有空格时按断句处理（空格代替标点），不报错
_SPACE_CN_EN = re.compile(rf'[{CJK}] +[^\s{CJK}]+ +(?=[{CJK}])')
_RUN = re.compile(rf'[^\s{CJK}]+')
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

    # 中文标点
    for i, ch in enumerate(text):
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
    '有改进空间', '建议修复', '综合来看',
]

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
    stmts = _SCORE_STMT.findall(text)
    if not stmts:
        issues.append({'rule': '没有写原分依据 结尾要写明为什么给几分', 'excerpt': text[-24:]})
    elif score not in (None, '') and stmts[-1] != str(int(score)):
        m = list(_SCORE_STMT.finditer(text))[-1]
        issues.append(_issue(f'文中给{stmts[-1]}分 与评分{int(score)}分不一致', text, m.start()))
    if not _CODE_ANCHOR.search(low):
        issues.append({'rule': '没有代码锚点 至少写到具体文件 函数或行号', 'excerpt': text[:24]})
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
    return _dedupe(issues)


def _dedupe(issues: list[dict]) -> list[dict]:
    seen, out = set(), []
    for it in issues:
        key = (it['rule'], it['excerpt'])
        if key not in seen:
            seen.add(key)
            out.append(it)
    return out
