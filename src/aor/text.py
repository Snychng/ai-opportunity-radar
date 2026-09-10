"""不依赖模型的多语文本线索；不把文字体系识别当成翻译。"""
from __future__ import annotations

import html
import re
import unicodedata
from typing import Any

STOP_WORDS = frozenset('a an the and or of to for in on is are with from by i we you it this that need tool'.split())
CJK_RUN = re.compile(r'[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]+')


def clean_text(value: Any, limit: int = 4000) -> str:
    if not isinstance(value, (str, int, float)):
        return ''
    return re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', html.unescape(str(value)))).strip()[:limit]


def tokens(text: str) -> set[str]:
    """Unicode 词项加中日韩连续片段二元组，保留单字与附加符号。"""
    text = unicodedata.normalize('NFKC', clean_text(text)).casefold()
    result: set[str] = set()
    for run in CJK_RUN.findall(text):
        result.update(run[i:i + 2] for i in range(max(1, len(run) - 1)))
    remainder = CJK_RUN.sub(' ', text)
    # 附加符号属于单词，如印地语元音；不能用 ASCII-only 正则切掉。
    word = ''
    for char in remainder + ' ':
        if unicodedata.category(char)[0] in {'L', 'M', 'N'}:
            word += char
        elif word:
            if word not in STOP_WORDS:
                result.add(word)
            word = ''
    return result


def scripts(text: str) -> set[str]:
    """返回文字体系集合，供无词面交集时区分跨语种未知。"""
    result = set()
    for char in text:
        if not unicodedata.category(char).startswith('L'):
            continue
        name = unicodedata.name(char, '')
        result.add('CJK' if 'CJK' in name or 'IDEOGRAPH' in name else name.split(' ', 1)[0])
    return result


def language(text: str | None, explicit: Any = None) -> str:
    explicit_text = clean_text(explicit, 20)
    if explicit_text:
        return explicit_text.lower().replace('_', '-')
    text = text or ''
    for pattern, code in ((r'[\u3040-\u30ff]', 'ja'), (r'[\uac00-\ud7af]', 'ko'),
                          (r'[\u3400-\u9fff]', 'zh')):
        if re.search(pattern, text):
            return code
    # 拉丁、西里尔等文字被多种语言共用，不推断具体国家语言。
    return 'unknown'
