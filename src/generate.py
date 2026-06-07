"""
src/generate.py
每日自动抓取 CNN This Morning 文稿，调用 DeepSeek API 生成精读学习内容
- 周末/节假日自动回退到最近的工作日
- 支持手动指定日期
"""

import os, re, json, requests, sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ── 配置 ──────────────────────────────────────────────────────
DEEPSEEK_API_KEY = os.environ.get('DEEPSEEK_API_KEY', '')
DEEPSEEK_URL     = 'https://api.deepseek.com/v1/chat/completions'
OUTPUT_DIR       = Path('output')
OUTPUT_DIR.mkdir(exist_ok=True)

CST = timezone(timedelta(hours=8))  # 北京时间


# ── 日期处理 ──────────────────────────────────────────────────
def get_target_date() -> str:
    """
    优先级：
    1. 环境变量 TARGET_DATE（手动指定）
    2. 今天（北京时间），如果是周末自动回退到上周五
    """
    d = os.environ.get('TARGET_DATE', '').strip()
    if d and re.match(r'^\d{4}-\d{2}-\d{2}$', d):
        print(f'  使用指定日期：{d}')
        return d
    today = datetime.now(CST)
    # 周六(5)→ 回退1天到周五，周日(6)→ 回退2天到周五
    weekday = today.weekday()
    if weekday == 5:
        today -= timedelta(days=1)
    elif weekday == 6:
        today -= timedelta(days=2)
    result = today.strftime('%Y-%m-%d')
    if weekday >= 5:
        print(f'  今天是周末，自动使用最近工作日：{result}')
    return result


def find_available_date(start_date: str, max_lookback: int = 7) -> str:
    """
    从 start_date 开始往前找，最多回溯 max_lookback 天，
    返回 CNN 有文稿的最近日期
    """
    dt = datetime.strptime(start_date, '%Y-%m-%d')
    for i in range(max_lookback):
        candidate = (dt - timedelta(days=i)).strftime('%Y-%m-%d')
        # 跳过周末
        if (dt - timedelta(days=i)).weekday() >= 5:
            print(f'  {candidate} 是周末，跳过')
            continue
        url = f'https://transcripts.cnn.com/show/ctmo/date/{candidate}/segment/01'
        print(f'  检查是否有文稿：{url}')
        try:
            r = requests.head(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=10)
            if r.status_code == 200:
                print(f'  ✓ 找到有效日期：{candidate}')
                return candidate
            else:
                print(f'  {candidate} 返回 {r.status_code}，继续往前找')
        except Exception as e:
            print(f'  {candidate} 请求失败：{e}')
    raise RuntimeError(f'回溯 {max_lookback} 天内未找到有效的 CNN 文稿')


# ── 抓取文稿 ──────────────────────────────────────────────────
def fetch_transcript(date_str: str) -> str:
    """尝试抓取 segment/01 ~ 03，合并有效文本"""
    combined = []
    for seg in [1, 2, 3]:
        url = f'https://transcripts.cnn.com/show/ctmo/date/{date_str}/segment/{seg:02d}'
        print(f'  Fetching: {url}')
        try:
            r = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=20)
            if r.status_code == 404:
                print(f'  Segment {seg}: 404，跳过')
                continue
            r.raise_for_status()
            body = r.text
            # 尝试提取正文区域
            section = re.search(r'<div[^>]+cnnTranscript[^>]*>(.*?)</div>', body, re.S)
            text = section.group(1) if section else body
            text = re.sub(r'<[^>]+>', ' ', text)
            text = re.sub(r'&nbsp;', ' ', text)
            text = re.sub(r'&amp;', '&', text)
            text = re.sub(r'&lt;', '<', text)
            text = re.sub(r'&gt;', '>', text)
            text = re.sub(r'\s+', ' ', text).strip()
            if len(text) > 300:
                combined.append(text)
                print(f'  Segment {seg}: OK（{len(text)} 字符）')
            else:
                print(f'  Segment {seg}: 内容过短，跳过')
        except Exception as e:
            print(f'  Segment {seg} 错误：{e}')

    if not combined:
        return ''

    full = '\n\n'.join(combined)
    return full[:9000]


# ── DeepSeek Prompt ───────────────────────────────────────────
SYSTEM = """你是专业的英语精读教学助手，专注于新闻英语教学。
输出面向考研六级以上学习者，重点覆盖：
1. 新闻政治高频词汇
2. 含嵌套从句/插入语/习语的长难句
3. 背景知识（帮助理解新闻语境）
必须输出合法JSON，不输出任何其他内容，不使用Markdown代码块。"""

def build_prompt(transcript: str, date_str: str, source_url: str) -> str:
    return f"""以下是CNN This Morning新闻逐字稿（{date_str}）：

{transcript}

请严格按照以下JSON结构输出精读学习内容：

{{
  "date": "{date_str}",
  "source_url": "{source_url}",
  "summary": "约150字中文摘要，涵盖文稿中所有主要新闻话题，每个话题一句话",

  "transcript_highlights": [
    {{
      "speaker": "说话人角色（如 ANCHOR / REPORTER / GUEST）",
      "text": "原文重要段落（逐字稿，保持原文，不少于3句话）",
      "cn": "对应中文翻译",
      "note": "语境说明（可选，一句话）"
    }}
  ],

  "vocabulary": [
    {{
      "word": "单词或短语",
      "phonetic": "/音标/",
      "pos": "词性",
      "level": "考研/六级/专四/专八/时事词汇",
      "cn": "中文释义（含常用搭配）",
      "en": "英文释义",
      "example": "原文例句（完整句子）",
      "example_cn": "例句中文翻译"
    }}
  ],

  "sentences": [
    {{
      "en": "原文长难句（完整句子）",
      "cn": "准确中文翻译",
      "structure": "句子结构标注（主句/从句/插入语等）",
      "analysis": "语法要点：嵌套结构/习语/修辞手法"
    }}
  ],

  "topics": [
    {{
      "title": "话题标题",
      "content": "约120字中文背景知识，含关键英文术语及解释",
      "keywords": "关键词1 · 关键词2 · 关键词3"
    }}
  ],

  "quiz": [
    {{
      "type": "vocab 或 sentence",
      "question": "题目（含原文引用语境）",
      "options": ["A. ...", "B. ...", "C. ...", "D. ..."],
      "answer": 0,
      "explanation": "详细解析：正确答案原因 + 干扰项排除"
    }}
  ]
}}

严格要求：
- transcript_highlights：选取4-5段最重要的原文段落（逐字稿），含中文对照
- vocabulary：恰好12个词，优先考研六级 + 新闻政治词汇，不选基础词
- sentences：恰好5句，优先含多重从句/插入语/习语的复杂句
- topics：恰好4个，对应文稿中4个主要新闻话题
- quiz：恰好6道（前3道考词汇，后3道考句意/背景理解）
- 所有内容必须来自文稿原文，不得虚构"""


# ── 调用 DeepSeek ─────────────────────────────────────────────
def call_deepseek(prompt: str) -> dict:
    if not DEEPSEEK_API_KEY:
        raise RuntimeError('DEEPSEEK_API_KEY 未设置，请在 GitHub Secrets 中添加。')

    print('  调用 DeepSeek API...')
    resp = requests.post(
        DEEPSEEK_URL,
        headers={
            'Authorization': f'Bearer {DEEPSEEK_API_KEY}',
            'Content-Type': 'application/json'
        },
        json={
            'model': 'deepseek-chat',
            'max_tokens': 4096,
            'temperature': 0.2,
            'messages': [
                {'role': 'system', 'content': SYSTEM},
                {'role': 'user',   'content': prompt}
            ]
        },
        timeout=120
    )
    resp.raise_for_status()
    raw = resp.json()['choices'][0]['message']['content']
    clean = re.sub(r'```json|```', '', raw).strip()
    return json.loads(clean)


# ── 主流程 ────────────────────────────────────────────────────
def main():
    print('\n=== CNN精读生成器 ===')

    # 1. 确定目标日期（含周末自动回退）
    requested_date = get_target_date()

    # 2. 检查缓存
    out_path = OUTPUT_DIR / f'{requested_date}.json'
    if out_path.exists():
        print(f'✓ 已有缓存：{out_path}，跳过生成。')
        return

    # 3. 验证 CNN 是否有该日期文稿，若无则继续往前找
    print(f'\n[1/3] 查找有效文稿日期（从 {requested_date} 开始）...')
    actual_date = find_available_date(requested_date, max_lookback=7)

    # 如果实际日期有缓存也跳过
    out_path = OUTPUT_DIR / f'{actual_date}.json'
    if out_path.exists():
        print(f'✓ 实际日期 {actual_date} 已有缓存，跳过。')
        return

    source_url = f'https://transcripts.cnn.com/show/ctmo/date/{actual_date}/segment/01'
    print(f'目标日期：{actual_date}')
    print(f'输出路径：{out_path}')

    # 4. 抓取文稿
    print(f'\n[2/3] 抓取 CNN 文稿...')
    transcript = fetch_transcript(actual_date)
    if not transcript:
        raise RuntimeError(f'{actual_date} 文稿抓取失败或内容为空')
    print(f'      文稿长度：{len(transcript)} 字符')

    # 5. 调用 DeepSeek 生成内容
    print(f'\n[3/3] 生成精读学习内容...')
    prompt = build_prompt(transcript, actual_date, source_url)
    data   = call_deepseek(prompt)
    print(f'      生成成功：{len(data.get("vocabulary",[]))} 词汇，{len(data.get("sentences",[]))} 难句')

    # 6. 保存
    out_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding='utf-8'
    )
    print(f'\n✅ 已保存：{out_path}')


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        print(f'\n❌ 错误：{e}', file=sys.stderr)
        sys.exit(1)
