"""
src/generate.py  v7.5
- 无截断，保留完整文稿
- 分段保存（segments 包含标题和文本）
- 增强 JSON 解析，支持修复未闭合括号、缺失逗号、裸换行、尾逗号等问题
- 新增 API 调用重试机制
- 强化 Prompt 约束，严格输出标准 JSON
- 解析失败时保存原始响应用于调试
"""
import os, re, json, requests, sys, time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from html.parser import HTMLParser

DEEPSEEK_API_KEY = os.environ.get('DEEPSEEK_API_KEY', '')
DEEPSEEK_URL     = 'https://api.deepseek.com/v1/chat/completions'
OUTPUT_DIR       = Path('output')
OUTPUT_DIR.mkdir(exist_ok=True)
CST = timezone(timedelta(hours=8))


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


class TranscriptExtractor(HTMLParser):
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
                cleaned = re.sub(r'[ \t]+', ' ', data)
                self.text_parts.append(cleaned)

    def get_text(self):
        return '\n\n'.join(self.text_parts)


def fetch_transcript(date_str: str) -> tuple[str, list[dict]]:
    """
    只抓取第一个 segment (01)，完整保留原文的换行和段落结构。
    """
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
    }
    seg = 1
    url = f'https://transcripts.cnn.com/show/ctmo/date/{date_str}/segment/{seg:02d}'
    print(f'  抓取 Segment {seg:02d}: {url}')
    try:
        resp = requests.get(url, headers=headers, timeout=30)
        if resp.status_code != 200:
            print(f'  Segment {seg:02d}: HTTP {resp.status_code}，抓取失败')
            return '', []

        # 提取标题
        title_match = re.search(r'<title>(.*?)</title>', resp.text, re.DOTALL | re.IGNORECASE)
        raw_title = title_match.group(1).strip() if title_match else ''
        time_match = re.search(r'(Aired\s+[\d:]+[ap]m?\s*ET)', resp.text, re.IGNORECASE)
        segment_title = time_match.group(1) if time_match else raw_title

        # 优先提取 transcriptBody 区域
        match = re.search(r'<(div|section)[^>]*id=["\']transcriptBody["\'][^>]*>(.*?)</\1>', resp.text, re.DOTALL | re.IGNORECASE)
        if not match:
            match = re.search(r'<(div|section)[^>]*class=["\'][^"\']*cnnTranscript[^"\']*["\'][^>]*>(.*?)</\1>', resp.text, re.DOTALL | re.IGNORECASE)

        if match:
            inner = match.group(2)
            # 将 <br> 转换为换行符，保留段落结构
            inner = re.sub(r'<br\s*/?>', '\n', inner)
            # 将块级结束标签转换为双换行
            inner = re.sub(r'</(p|div|section|h\d)>', '\n\n', inner, flags=re.IGNORECASE)
            # 移除其余 HTML 标签
            body_text = re.sub(r'<[^>]+>', '', inner)
            # 清理多余空格
            body_text = re.sub(r'[ \t]+', ' ', body_text)
            # 压缩连续换行
            body_text = re.sub(r'\n{3,}', '\n\n', body_text)
            body_text = body_text.strip()
            print(f'      策略1成功，长度 {len(body_text)}')
        else:
            # 回退：自定义解析器
            print(f'      策略1失败，尝试自定义解析器...')
            parser = TranscriptExtractor()
            parser.feed(resp.text)
            body_text = parser.get_text()
            if body_text:
                body_text = re.sub(r'[ \t]+', ' ', body_text)
                body_text = re.sub(r'\n{3,}', '\n\n', body_text).strip()
                print(f'      策略2成功，长度 {len(body_text)}')

        if len(body_text) > 300:
            segments_data = [{
                'seg': seg,
                'url': url,
                'title': segment_title,
                'text': body_text
            }]
            print(f'  Segment {seg:02d}: 提取成功 ({len(body_text)} 字符)')
            return body_text, segments_data
        else:
            print(f'  Segment {seg:02d}: 内容过短 ({len(body_text)} 字符)，抓取失败')
            return '', []
    except Exception as e:
        print(f'  Segment {seg:02d} 异常: {e}')
        return '', []


def parse_json_robust(raw: str) -> dict:
    """超强容错JSON解析：修复代码块、裸换行、缺失逗号、尾逗号、括号不匹配"""
    # 移除 markdown 代码块标记
    cleaned = re.sub(r'^```(?:json)?\s*|\s*```$', '', raw.strip(), flags=re.MULTILINE).strip()

    # 1. 转义字符串内裸换行（JSON 不允许字符串直接含 \n）
    cleaned = re.sub(r'(?<=")[^"]*\n[^"]*(?=")', lambda m: m.group(0).replace('\n', '\\n'), cleaned)
    # 2. 移除列表/对象末尾多余逗号
    cleaned = re.sub(r',\s*([}\]])', r'\1', cleaned)
    # 3. 简单补全缺失分隔逗号
    cleaned = re.sub(r'("[\s\S]*?")(?=[]}])', r'\1,', cleaned)

    # 第一次尝试解析
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # 定位完整 JSON 大括号块
    start = cleaned.find('{')
    if start == -1:
        raise ValueError('未找到 JSON 起始字符 "{"')

    stack = []
    end = -1
    for i, ch in enumerate(cleaned[start:], start):
        if ch == '{':
            stack.append(i)
        elif ch == '}':
            if stack:
                stack.pop()
                if not stack:
                    end = i
    # 兜底取最后一个 }
    if end == -1:
        end = cleaned.rfind('}')
        if end == -1:
            raise ValueError('未找到 "}"')
        # 补齐缺失右括号
        left_cnt = cleaned[start:end+1].count('{')
        right_cnt = cleaned[start:end+1].count('}')
        missing = left_cnt - right_cnt
        if missing > 0:
            cleaned = cleaned[:end+1] + '}' * missing
            end = len(cleaned) - 1

    candidate = cleaned[start:end+1]
    # 二次清洗候选片段
    candidate = re.sub(r'(?<=")[^"]*\n[^"]*(?=")', lambda m: m.group(0).replace('\n', '\\n'), candidate)
    candidate = re.sub(r',\s*([}\]])', r'\1', candidate)

    try:
        return json.loads(candidate)
    except json.JSONDecodeError as e:
        debug_file = OUTPUT_DIR / 'last_api_response.txt'
        debug_file.write_text(raw, encoding='utf-8')
        print(f'  已保存原始响应到 {debug_file}')
        print(f'  提取的 JSON 片段（前 500）:\n{candidate[:500]}')
        print(f'  提取的 JSON 片段（后 500）:\n{candidate[-500:]}')
        raise ValueError(f'JSON解析失败，错误位置: {e}')


SYSTEM = """你是专业英语精读教学助手，专注新闻英语。
目标学习者：考研六级以上。
输出强制规则：
1. 仅输出合法标准JSON，禁止Markdown、解释、额外文字
2. JSON字符串内双引号用 \\" 转义
3. 例句中的双引号改为单引号
4. 字符串内换行必须转义为 \\n，禁止裸换行
5. 列表、对象末尾不允许多余逗号"""


def build_prompt(transcript: str, date_str: str, source_url: str) -> str:
    # 前置转义关键字符
    safe_text = transcript.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n')
    return f"""CNN This Morning 逐字稿（{date_str}）：
{safe_text}

严格输出**标准合法JSON**，可被Python json.loads直接解析，不添加任何额外内容。
必须包含以下全部字段与结构，字段名称、层级不能修改：
{{
  "date": "{date_str}",
  "source_url": "{source_url}",
  "full_translation": [
    {{
      "paragraph": "原文段落（保持原文顺序，每个自然段一个条目）",
      "translation": "对应中文翻译"
    }}
  ],
  "vocabulary": [
    {{
      "word": "单词或短语",
      "phonetic": "/音标/",
      "pos": "词性",
      "level": "考研/六级/专四/专八",
      "cn": "中文释义（含搭配）",
      "en": "英文释义",
      "excerpt": "包含该词的原文片段（10-20词，单引号代替双引号）",
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

补充要求：
1. full_translation：按自然段逐段翻译，不遗漏内容
2. vocabulary：提取考研/六级/专四/专八词汇短语，至少30个
3. sentences：整理全文所有长难句
4. topics：整理相关背景话题，至少10个
5. 禁止新增、删除、重命名任何字段"""


def call_deepseek(prompt: str, max_retries: int = 3) -> dict:
    if not DEEPSEEK_API_KEY:
        raise RuntimeError('DEEPSEEK_API_KEY 未设置')

    for retry in range(max_retries):
        try:
            print(f'  调用 DeepSeek API (重试 {retry+1}/{max_retries})...')
            resp = requests.post(
                DEEPSEEK_URL,
                headers={
                    'Authorization': f'Bearer {DEEPSEEK_API_KEY}',
                    'Content-Type': 'application/json'
                },
                json={
                    'model': 'deepseek-chat',
                    'max_tokens': 16384,
                    'temperature': 0.1,
                    'response_format': {'type': 'json_object'},
                    'messages': [
                        {'role': 'system', 'content': SYSTEM},
                        {'role': 'user', 'content': prompt}
                    ]
                },
                timeout=180
            )
            resp.raise_for_status()
            raw_content = resp.json()['choices'][0]['message']['content']
            print(f'  API返回：{len(raw_content)} 字符')
            return parse_json_robust(raw_content)
        except Exception as e:
            print(f'  第 {retry+1} 次调用失败：{str(e)}')
            if retry >= max_retries - 1:
                raise
            time.sleep(2 ** retry)


def main():
    print('\n=== CNN精读生成器 v7.5（无截断 + 分段展示 + 增强JSON解析 + 接口重试） ===')
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

    print(f'\n[2/3] 抓取文稿（保留原始段落结构）...')
    full_text, segments_data = fetch_transcript(actual_date)
    if not full_text:
        raise RuntimeError(f'{actual_date} 文稿抓取失败')
    print(f'      抓取到 {len(segments_data)} 个 segment，文稿总长度：{len(full_text)} 字符')

    print(f'\n[3/3] 生成精读内容...')
    prompt = build_prompt(full_text, actual_date, source_url)
    data = call_deepseek(prompt)

    # 补充原始文稿与分段信息
    data['raw_transcript'] = full_text
    data['segments'] = segments_data
    data['date'] = actual_date
    data['source_url'] = source_url

    print(f'      词汇数：{len(data.get("vocabulary",[]))}  难句：{len(data.get("sentences",[]))}  话题：{len(data.get("topics",[]))}')
    out_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'\n✅ 已保存：{out_path}')


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        print(f'\n❌ {e}', file=sys.stderr)
        sys.exit(1)
