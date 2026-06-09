"""
src/generate.py  v7.1
- 支持抓取任意数量的 segment（自动探测直到 404）
- 取消周末判断，原样提取全部字幕
- 要求 DeepSeek 返回全文翻译 + 词汇（仅考研/六级，不限数量）
- 句子（长难句，不限数量）+ 话题背景（不限数量），无测试题
"""

import os, re, json, requests, sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

DEEPSEEK_API_KEY = os.environ.get('DEEPSEEK_API_KEY', '')
DEEPSEEK_URL     = 'https://api.deepseek.com/v1/chat/completions'
OUTPUT_DIR       = Path('output')
OUTPUT_DIR.mkdir(exist_ok=True)
CST = timezone(timedelta(hours=8))


# ── 日期处理 ──────────────────────────────────────────────────
def get_target_date() -> str:
    d = os.environ.get('TARGET_DATE', '').strip()
    if d and re.match(r'^\d{4}-\d{2}-\d{2}$', d):
        print(f'  使用指定日期：{d}')
        return d
    today = datetime.now(CST)
    # 不再调整周末，直接返回当天日期
    return today.strftime('%Y-%m-%d')


def find_available_date(start_date: str, max_lookback: int = 7) -> str:
    dt = datetime.strptime(start_date, '%Y-%m-%d')
    for i in range(max_lookback):
        candidate_dt = dt - timedelta(days=i)
        candidate = candidate_dt.strftime('%Y-%m-%d')
        # 不再跳过周末，直接尝试请求
        url = f'https://transcripts.cnn.com/show/ctmo/date/{candidate}/segment/01'
        print(f'  检查：{url}')
        try:
            r = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=15)
            if r.status_code == 200 and len(r.text) > 500:
                print(f'  ✓ 找到有效日期：{candidate}')
                return candidate
            print(f'  {candidate} 返回 {r.status_code}')
        except Exception as e:
            print(f'  {candidate} 请求失败：{e}')
    raise RuntimeError(f'回溯 {max_lookback} 天内未找到有效 CNN 文稿')


# ── 文稿抓取 & 清理（支持任意数量 segment）────────────────────────
def sanitize(text: str) -> str:
    text = text.replace('\u2018', "'").replace('\u2019', "'")
    text = text.replace('\u201c', '"').replace('\u201d', '"')
    text = text.replace('\u2014', ' -- ').replace('\u2013', '-')
    text = text.replace('\u00a0', ' ').replace('\u00ad', '')
    text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', text)
    text = re.sub(r' {3,}', '  ', text)
    return text.strip()


def fetch_transcript(date_str: str) -> tuple[str, list[dict]]:
    """
    返回 (合并后全文, 各segment原始段落列表)
    自动探测 segment 编号从 01 开始递增，直到返回 404 且没有新内容为止。
    """
    segments_data = []
    seg = 1
    while True:
        url = f'https://transcripts.cnn.com/show/ctmo/date/{date_str}/segment/{seg:02d}'
        print(f'  尝试 Segment {seg:02d}: {url}')
        try:
            r = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=20)
            if r.status_code == 404:
                print(f'  Segment {seg:02d}: 404，停止抓取')
                break
            r.raise_for_status()
            body = r.text

            # 提取纯文本（保留原始格式，不过滤说话人）
            section = re.search(r'<div[^>]+cnnTranscript[^>]*>(.*?)</div>', body, re.S)
            raw = section.group(1) if section else body
            raw = re.sub(r'<[^>]+>', ' ', raw)
            raw = re.sub(r'&nbsp;', ' ', raw)
            raw = re.sub(r'&amp;', '&', raw)
            raw = re.sub(r'&lt;', '<', raw)
            raw = re.sub(r'&gt;', '>', raw)
            raw = re.sub(r'\s+', ' ', raw).strip()
            raw = sanitize(raw)

            if len(raw) > 300:
                segments_data.append({'seg': seg, 'url': url, 'text': raw})
                print(f'  Segment {seg:02d}: OK（{len(raw)} 字符）')
            else:
                print(f'  Segment {seg:02d}: 内容过短（{len(raw)} 字符），停止抓取')
                break
            seg += 1
        except Exception as e:
            print(f'  Segment {seg:02d} 错误：{e}')
            break

    if not segments_data:
        return '', []

    # 直接合并所有 segment 的文本，保留原始换行和标点
    full = sanitize('\n\n'.join(s['text'] for s in segments_data))
    # 增加长度限制到 20000 字符（约 5000 token）
    return full[:20000], segments_data


# ── JSON 解析容错 ─────────────────────────────────────────────
def parse_json_robust(raw: str) -> dict:
    for attempt in [
        lambda s: json.loads(s),
        lambda s: json.loads(re.sub(r'^```(?:json)?\s*|\s*```$', '', s.strip(), flags=re.MULTILINE).strip()),
    ]:
        try:
            return attempt(raw)
        except (json.JSONDecodeError, Exception):
            pass

    start = raw.find('{')
    end   = raw.rfind('}')
    if start != -1 and end > start:
        chunk = raw[start:end+1]
        try:
            return json.loads(chunk)
        except json.JSONDecodeError as e:
            print(f'  JSON错误 line {e.lineno} col {e.colno}：{repr(raw[max(0,e.pos-60):e.pos+60])}')
            ob = chunk.count('{') - chunk.count('}')
            ob2 = chunk.count('[') - chunk.count(']')
            repaired = chunk + (']' * max(0, ob2)) + ('}' * max(0, ob))
            try:
                return json.loads(repaired)
            except Exception:
                pass

    raise ValueError(f'JSON 解析失败，原始内容前200字：\n{raw[:200]}')


# ── Prompt（不限制句子和话题个数，只提取考研/六级词汇）─────────────────
SYSTEM = """你是专业英语精读教学助手，专注新闻英语。
目标学习者：考研六级以上。
输出规则：
1. 必须输出合法JSON，不使用Markdown代码块
2. JSON字符串中的双引号用 \\\" 转义
3. 例句中的双引号改为单引号"""

def build_prompt(transcript: str, date_str: str, source_url: str) -> str:
    safe = transcript.replace('\\', '\\\\').replace('"', '\\"')
    return f"""CNN This Morning 逐字稿（{date_str}）：

{safe}

输出以下JSON（所有字段必须存在）：

{{
  "date": "{date_str}",
  "source_url": "{source_url}",
  "full_translation": "整篇文稿的逐句中文翻译，保留原文换行和说话人标记（如 ANCHOR: ...）",

  "vocabulary": [
    {{
      "word": "单词或短语",
      "phonetic": "/音标/",
      "pos": "词性",
      "level": "考研/六级",   # 只允许这两个值
      "cn": "中文释义（含搭配）",
      "en": "英文释义",
      "excerpt": "包含该词的原文片段（10-20词，用于高亮定位，单引号代替双引号）",
      "example_cn": "该片段中文翻译"
    }}
  ],

  "sentences": [
    {{
      "en": "原文长难句（完整句子）",
      "cn": "准确中文翻译",
      "structure": "句子结构（主句/从句/插入语等）",
      "analysis": "语法要点/习语/修辞分析"
    }}
  ],

  "topics": [
    {{
      "title": "话题标题",
      "content": "120字中文背景知识，含关键英文术语",
      "keywords": "词1 · 词2 · 词3"
    }}
  ]
}}

严格要求：
- vocabulary：只提取考研和六级水平的词汇或短语，忽略其他难度。不限个数，至少12个，上不封顶
- sentences：提取文稿中的所有长难句（结构复杂、含从句或特殊语法），不限个数，有多少提取多少
- topics：提取所有值得展开的背景话题，不限个数，至少4个，上不封顶
- 不需要 summary 字段
- 不需要 quiz 字段
- 必须包含 full_translation，逐句对应原文，保证完整"""


# ── 调用 DeepSeek ─────────────────────────────────────────────
def call_deepseek(prompt: str) -> dict:
    if not DEEPSEEK_API_KEY:
        raise RuntimeError('DEEPSEEK_API_KEY 未设置')
    print('  调用 DeepSeek API...')
    resp = requests.post(
        DEEPSEEK_URL,
        headers={'Authorization': f'Bearer {DEEPSEEK_API_KEY}', 'Content-Type': 'application/json'},
        json={
            'model': 'deepseek-chat',
            'max_tokens': 8192,      # 增加 token 以容纳全文翻译和更多内容
            'temperature': 0.1,
            'response_format': {'type': 'json_object'},
            'messages': [
                {'role': 'system', 'content': SYSTEM},
                {'role': 'user',   'content': prompt}
            ]
        },
        timeout=150
    )
    resp.raise_for_status()
    raw = resp.json()['choices'][0]['message']['content']
    print(f'  API返回：{len(raw)} 字符')
    return parse_json_robust(raw)


# ── 主流程 ────────────────────────────────────────────────────
def main():
    print('\n=== CNN精读生成器 v7.1（自动抓取所有 segment + 不限词汇/句子/话题） ===')

    requested_date = get_target_date()
    out_path = OUTPUT_DIR / f'{requested_date}.json'
    if out_path.exists():
        print(f'✓ 缓存已存在：{out_path}')
        return

    print(f'\n[1/3] 查找有效文稿（从 {requested_date} 开始，不跳过周末）...')
    actual_date = find_available_date(requested_date, max_lookback=7)

    out_path = OUTPUT_DIR / f'{actual_date}.json'
    if out_path.exists():
        print(f'✓ {actual_date} 缓存已存在')
        return

    source_url = f'https://transcripts.cnn.com/show/ctmo/date/{actual_date}/segment/01'
    print(f'目标日期：{actual_date}  输出：{out_path}')

    print(f'\n[2/3] 抓取文稿（自动探测所有 segment）...')
    full_text, segments_data = fetch_transcript(actual_date)
    if not full_text:
        raise RuntimeError(f'{actual_date} 文稿抓取失败')
    print(f'      抓取到 {len(segments_data)} 个 segment，文稿总长度：{len(full_text)} 字符')

    print(f'\n[3/3] 生成精读内容（仅考研/六级词汇）...')
    prompt = build_prompt(full_text, actual_date, source_url)
    data   = call_deepseek(prompt)

    # 注入原始完整文稿（原样保留）
    data['raw_transcript'] = full_text
    data['date']           = actual_date
    data['source_url']     = source_url

    print(f'      词汇数：{len(data.get("vocabulary",[]))}  难句：{len(data.get("sentences",[]))}  话题：{len(data.get("topics",[]))}')

    out_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'\n✅ 已保存：{out_path}')


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        print(f'\n❌ {e}', file=sys.stderr)
        sys.exit(1)
