"""
src/generate.py  v7.6
- 彻底修复JSON字符串内裸换行解析错误
- 全局字符清洗 + 多层JSON容错
- 保留原有抓取、缓存、重试逻辑
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

        title_match = re.search(r'<title>(.*?)</title>', resp.text, re.DOTALL | re.IGNORECASE)
        raw_title = title_match.group(1).strip() if title_match else ''
        time_match = re.search(r'(Aired\s+[\d:]+[ap]m?\s*ET)', resp.text, re.IGNORECASE)
        segment_title = time_match.group(1) if time_match else raw_title

        match = re.search(r'<(div|section)[^>]*id=["\']transcriptBody["\'][^>]*>(.*?)</\1>', resp.text, re.DOTALL | re.IGNORECASE)
        if not match:
            match = re.search(r'<(div|section)[^>]*class=["\'][^"\']*cnnTranscript[^"\']*["\'][^>]*>(.*?)</\1>', resp.text, re.DOTALL | re.IGNORECASE)

        if match:
            inner = match.group(2)
            inner = re.sub(r'<br\s*/?>', '\n', inner)
            inner = re.sub(r'</(p|div|section|h\d)>', '\n\n', inner, flags=re.IGNORECASE)
            body_text = re.sub(r'<[^>]+>', '', inner)
            body_text = re.sub(r'[ \t]+', ' ', body_text)
            body_text = re.sub(r'\n{3,}', '\n\n', body_text)
            body_text = body_text.strip()
            print(f'      策略1成功，长度 {len(body_text)}')
        else:
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


def deep_clean_json_str(raw: str) -> str:
    """全局深度清洗JSON字符串，解决裸换行、非法字符、多余逗号"""
    # 1. 移除代码块标记
    txt = re.sub(r'^```(?:json)?\s*|\s*```$', '', raw.strip(), flags=re.MULTILINE).strip()

    # 2. 统一换行符并全局转义（核心修复：解决字符串内裸换行）
    txt = txt.replace('\r\n', '\n').replace('\r', '\n')
    # 对双引号包裹的内容，强制把 \n 转为 \\n
    in_quote = False
    result = []
    for char in txt:
        if char == '"':
            in_quote = not in_quote
            result.append(char)
        elif char == '\n' and in_quote:
            result.append('\\n')
        else:
            result.append(char)
    txt = ''.join(result)

    # 3. 移除列表/对象末尾多余逗号
    txt = re.sub(r',\s*([}\]])', r'\1', txt)
    # 4. 清理不可见控制字符
    txt = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', txt)
    return txt


def parse_json_robust(raw: str) -> dict:
    cleaned = deep_clean_json_str(raw)

    # 首次解析
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # 截取外层完整 JSON 对象
    start = cleaned.find('{')
    if start == -1:
        raise ValueError('未找到 JSON 起始字符 "{"')

    stack = []
    end_idx = -1
    for idx, ch in enumerate(cleaned[start:], start):
        if ch == '{':
            stack.append(idx)
        elif ch == '}':
            if stack:
                stack.pop()
                if not stack:
                    end_idx = idx
                    break

    # 兜底处理括号
    if end_idx == -1:
        end_idx = cleaned.rfind('}')
        if end_idx == -1:
            raise ValueError('未找到 JSON 结束符 "}"')
        left = cleaned[start:end_idx+1].count('{')
        right = cleaned[start:end_idx+1].count('}')
        if left > right:
            cleaned = cleaned[:end_idx+1] + '}' * (left - right)
            end_idx = len(cleaned) - 1

    candidate = cleaned[start:end_idx+1]
    # 二次清洗
    candidate = deep_clean_json_str(candidate)

    try:
        return json.loads(candidate)
    except json.JSONDecodeError as e:
        debug_file = OUTPUT_DIR / 'last_api_response.txt'
        debug_file.write_text(raw, encoding='utf-8')
        print(f'  已保存原始响应到 {debug_file}')
        print(f'  解析片段前500:\n{candidate[:500]}')
        raise ValueError(f'JSON解析失败: {e}')


SYSTEM = """你是专业英语精读教学助手，专注新闻英语。
目标学习者：考研六级以上。
# 硬性输出规则（必须遵守）
1. 仅输出纯JSON，无任何额外文字、注释、Markdown代码块；
2. 所有字符串内**换行必须转义为 \\n**，禁止裸换行；
3. 字符串内双引号用 \\" 转义，例句改用单引号；
4. 数组/对象末尾**禁止多余逗号**；
5. 严格使用标准双引号包裹字段名，不使用单引号。"""


def build_prompt(transcript: str, date_str: str, source_url: str) -> str:
    # 前置对原文做全转义，避免污染JSON结构
    safe_text = transcript.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n')
    return f"""CNN This Morning 逐字稿（{date_str}）：
{safe_text}

请严格输出**标准可解析JSON**，结构、字段名完全遵循下方模板，不得增删改：
{{
  "date": "{date_str}",
  "source_url": "{source_url}",
  "full_translation": [
    {{
      "paragraph": "原文段落",
      "translation": "对应中文翻译"
    }}
  ],
  "vocabulary": [
    {{
      "word": "单词/短语",
      "phonetic": "/音标/",
      "pos": "词性",
      "level": "考研/六级/专四/专八",
      "cn": "中文释义",
      "en": "英文释义",
      "excerpt": "原文片段",
      "example_cn": "片段翻译"
    }}
  ],
  "sentences": [
    {{
      "en": "原文长难句",
      "cn": "中文翻译",
      "structure": "句子结构",
      "analysis": "语法分析"
    }}
  ],
  "topics": [
    {{
      "title": "话题标题",
      "content": "背景知识",
      "keywords": "关键词"
    }}
  ]
}}

业务要求：
1. full_translation 按原文段落逐段翻译；
2. vocabulary 提取至少30个考研/六级/专四/专八词汇；
3. sentences 整理全部长难句；
4. topics 整理至少10个相关背景话题；
5. 所有段落换行统一使用转义符 \\n，严禁直接换行。"""


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
    print('\n=== CNN精读生成器 v7.6（彻底修复JSON换行解析问题） ===')
    requested_date = get_target_date()
    out_path = OUTPUT_DIR / f'{requested_date}.json'
    if out_path.exists():
        print(f'✓ 缓存已存在：{out_path}')
        return

    print(f'\n[1/3] 查找有效文稿（从 {requested_date} 开始）...')
    actual_date = find_available_date(requested_date, max_lookback=7)
    out_path = OUTPUT_DIR / f'{actual_date}.json'
    if out_path.exists():
        print(f'✓ {actual_date} 缓存已存在')
        return

    source_url = f'https://transcripts.cnn.com/show/ctmo/date/{actual_date}/segment/01'
    print(f'目标日期：{actual_date}  输出：{out_path}')

    print(f'\n[2/3] 抓取文稿...')
    full_text, segments_data = fetch_transcript(actual_date)
    if not full_text:
        raise RuntimeError(f'{actual_date} 文稿抓取失败')
    print(f'      抓取到 {len(segments_data)} 个 segment，文稿总长度：{len(full_text)} 字符')

    print(f'\n[3/3] 生成精读内容...')
    prompt = build_prompt(full_text, actual_date, source_url)
    data = call_deepseek(prompt)

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
