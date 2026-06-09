"""
src/generate.py  v7.3
- 增强抓取：多策略提取文稿内容（id=transcriptBody, 自定义解析器, 全文回退）
- 保留原文换行和段落结构
- 自动抓取所有 segment（无限循环直到 404）
- 取消周末判断，原样提取全部字幕
- 要求 DeepSeek 返回全文翻译 + 词汇（仅考研/六级，不限数量）
- 句子（长难句，不限数量）+ 话题背景（不限数量），无测试题
"""

import os, re, json, requests, sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from html.parser import HTMLParser

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
    return today.strftime('%Y-%m-%d')


def find_available_date(start_date: str, max_lookback: int = 7) -> str:
    dt = datetime.strptime(start_date, '%Y-%m-%d')
    for i in range(max_lookback):
        candidate_dt = dt - timedelta(days=i)
        candidate = candidate_dt.strftime('%Y-%m-%d')
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


# ── 增强型文稿抓取（多策略）────────────────────────────────────
class TranscriptExtractor(HTMLParser):
    """从HTML中提取文本，跳过脚本/样式等标签"""
    def __init__(self):
        super().__init__()
        self.text_parts = []
        self.skip = False
        self.skip_tags = {'script', 'style', 'nav', 'header', 'footer', 'aside', 'meta', 'link'}

    def handle_starttag(self, tag, attrs):
        if tag in self.skip_tags:
            self.skip = True

    def handle_endtag(self, tag):
        if tag in self.skip_tags:
            self.skip = False

    def handle_data(self, data):
        if not self.skip:
            data = data.strip()
            if len(data) > 20:
                # 清理多余空格但保留换行（后面再处理）
                cleaned = re.sub(r'[ \t]+', ' ', data)
                self.text_parts.append(cleaned)

    def get_text(self):
        return '\n\n'.join(self.text_parts)


def fetch_transcript(date_str: str) -> tuple[str, list[dict]]:
    """
    多策略抓取：优先找 transcriptBody，其次用自定义解析器，最后回退全文提取
    """
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
    }
    segments_data = []
    seg = 1
    max_segments = 15  # 最多尝试15个

    for seg in range(1, max_segments+1):
        url = f'https://transcripts.cnn.com/show/ctmo/date/{date_str}/segment/{seg:02d}'
        print(f'  尝试 Segment {seg:02d}: {url}')
        try:
            resp = requests.get(url, headers=headers, timeout=30)
            if resp.status_code == 404:
                print(f'  Segment {seg:02d}: 404，停止抓取')
                break
            if resp.status_code != 200:
                print(f'  Segment {seg:02d}: HTTP {resp.status_code}，跳过')
                continue

            body_text = ""

            # 策略1：查找 id="transcriptBody" 或 class="cnnTranscript"
            match = re.search(r'<(div|section)[^>]*id=["\']transcriptBody["\'][^>]*>(.*?)</\1>', resp.text, re.DOTALL | re.IGNORECASE)
            if not match:
                match = re.search(r'<(div|section)[^>]*class=["\'][^"\']*cnnTranscript[^"\']*["\'][^>]*>(.*?)</\1>', resp.text, re.DOTALL | re.IGNORECASE)
            if match:
                inner = match.group(2)
                # 移除HTML标签，但保留换行
                inner = re.sub(r'<br\s*/?>', '\n', inner)
                inner = re.sub(r'</?(p|div|section|h\d|span)[^>]*>', '\n', inner)
                body_text = re.sub(r'<[^>]+>', ' ', inner)
                body_text = re.sub(r'[ \t]+', ' ', body_text)
                body_text = re.sub(r'\n\s*\n', '\n\n', body_text).strip()
                print(f'      策略1 (transcriptBody) 成功，长度 {len(body_text)}')

            if len(body_text) < 300:
                # 策略2：自定义HTML解析器，提取主要内容
                print(f'      策略1失败，尝试自定义解析器...')
                parser = TranscriptExtractor()
                parser.feed(resp.text)
                body_text = parser.get_text()
                if body_text:
                    # 进一步清理
                    body_text = re.sub(r'[ \t]+', ' ', body_text)
                    body_text = re.sub(r'\n{3,}', '\n\n', body_text).strip()
                    print(f'      策略2成功，长度 {len(body_text)}')

            if len(body_text) < 300:
                # 策略3：全文本回退，找关键位置 "Aired" 附近
                print(f'      策略2失败，尝试全文回退...')
                raw_text = re.sub(r'<[^>]+>', ' ', resp.text)
                raw_text = re.sub(r'\s+', ' ', raw_text)
                # 寻找 "Aired" 作为正文开始
                aired_pos = raw_text.find('Aired')
                if aired_pos != -1:
                    body_text = raw_text[aired_pos:aired_pos+12000].strip()
                else:
                    body_text = raw_text[:12000].strip()
                print(f'      策略3完成，长度 {len(body_text)}')

            if len(body_text) > 300:
                segments_data.append({'seg': seg, 'url': url, 'text': body_text})
                print(f'  Segment {seg:02d}: 提取成功 ({len(body_text)} 字符)')
            else:
                print(f'  Segment {seg:02d}: 提取内容过短 ({len(body_text)} 字符)，停止抓取')
                break

        except Exception as e:
            print(f'  Segment {seg:02d} 异常: {e}')
            break

    if not segments_data:
        return '', []

    full_text = '\n\n'.join(s['text'] for s in segments_data)
    # 限制总长度（避免超过API token限制）
    if len(full_text) > 30000:
        full_text = full_text[:30000] + '\n...[truncated]'
    return full_text, segments_data


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
            'max_tokens': 8192,
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
    print('\n=== CNN精读生成器 v7.3（增强抓取 + 保留换行 + 全量segment） ===')

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

    print(f'\n[2/3] 抓取文稿（自动探测所有 segment，保留换行）...')
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
