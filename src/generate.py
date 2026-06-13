"""
src/generate.py  v8.1
改动：取消中文全文翻译，节省Token；保留词汇/长难句/话题
保留：多分片探测、分片缓存、JSON强清洗、缺失逗号修复、API重试、括号补全
"""
import os, re, json, requests, sys, time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from html.parser import HTMLParser

DEEPSEEK_API_KEY = os.environ.get('DEEPSEEK_API_KEY', '')
DEEPSEEK_URL     = 'https://api.deepseek.com/v1/chat/completions'
OUTPUT_DIR       = Path('output')
# 分片缓存目录：存放单个segment的临时文件
SEGMENT_CACHE_DIR = OUTPUT_DIR / 'seg_cache'
OUTPUT_DIR.mkdir(exist_ok=True)
SEGMENT_CACHE_DIR.mkdir(exist_ok=True)

CST = timezone(timedelta(hours=8))
# 最大探测Segment数量（根据源站实际情况调整，建议10以内）
MAX_SEGMENT_NUM = 10
# 单片段最大字符限制，进一步控Token
MAX_SEG_TEXT_LEN = 38000


def get_target_date() -> str:
    d = os.environ.get('TARGET_DATE', '').strip()
    if d:
        # 兼容 2026-6-5 / 2026-06-05 格式，自动标准化
        try:
            target_dt = datetime.strptime(d, '%Y-%m-%d')
            std_date = target_dt.strftime('%Y-%m-%d')
            print(f'  使用指定日期：{std_date}')
            return std_date
        except ValueError:
            print(f'⚠️ 传入日期「{d}」格式非法，自动使用最近工作日')

    # 未指定/格式错误，使用北京时间当日
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
            elif r.status_code == 404:
                print(f'  {candidate} 文稿不存在 (404)')
            else:
                print(f'  {candidate} 返回 {r.status_code}')
        except Exception as e:
            print(f'  {candidate} 请求失败：{e}')
    raise RuntimeError(f'回溯 {max_lookback} 天内未找到有效 CNN 文稿')


def get_all_valid_segments(date_str: str) -> list[int]:
    """自动探测当日所有有效的 segment 编号"""
    valid_segs = []
    headers = {'User-Agent': 'Mozilla/5.0'}
    print(f'\n[探测当日所有 Segment] 最大探测数: {MAX_SEGMENT_NUM}')
    for seg in range(1, MAX_SEGMENT_NUM + 1):
        seg_str = f"{seg:02d}"
        url = f'https://transcripts.cnn.com/show/ctmo/date/{date_str}/segment/{seg_str}'
        try:
            r = requests.get(url, headers=headers, timeout=15)
            if r.status_code == 200 and len(r.text) > 500:
                valid_segs.append(seg)
                print(f'  ✓ Segment {seg_str} 有效')
            else:
                print(f'  × Segment {seg_str} 无内容/无效，停止探测')
                break
        except Exception:
            print(f'  × Segment {seg_str} 请求异常，停止探测')
            break
    if not valid_segs:
        raise RuntimeError(f'{date_str} 未探测到任何有效 Segment')
    return valid_segs


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


def fetch_transcript(date_str: str, seg: int) -> tuple[str, dict]:
    """抓取单个 Segment 内容"""
    seg_str = f"{seg:02d}"
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
    }
    url = f'https://transcripts.cnn.com/show/ctmo/date/{date_str}/segment/{seg_str}'
    print(f'  抓取 Segment {seg_str}: {url}')
    try:
        resp = requests.get(url, headers=headers, timeout=30)
        if resp.status_code != 200:
            print(f'  Segment {seg_str}: HTTP {resp.status_code}，抓取失败')
            return '', {}

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

        # 截断超长文本，控制Token
        if len(body_text) > MAX_SEG_TEXT_LEN:
            body_text = body_text[:MAX_SEG_TEXT_LEN]
            print(f'      超长文本已截断至 {MAX_SEG_TEXT_LEN} 字符')

        if len(body_text) > 300:
            seg_info = {
                'seg': seg,
                'url': url,
                'title': segment_title,
                'text': body_text
            }
            print(f'  Segment {seg_str}: 提取成功 ({len(body_text)} 字符)')
            return body_text, seg_info
        else:
            print(f'  Segment {seg_str}: 内容过短，抓取失败')
            return '', {}
    except Exception as e:
        print(f'  Segment {seg_str} 异常: {e}')
        return '', {}


def deep_clean_json_str(raw: str) -> str:
    txt = re.sub(r'^```(?:json)?\s*|\s*```$', '', raw.strip(), flags=re.MULTILINE).strip()
    txt = txt.replace('\r\n', '\n').replace('\r', '\n')

    # 精准转义字符串内裸换行
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

    # 核心修复：补全缺失逗号 解决 Expecting ',' delimiter
    txt = re.sub(r'}\s*\{', '}, {', txt)
    txt = re.sub(r']\s*\{', '], {', txt)
    txt = re.sub(r'}\s*"', '}, "', txt)
    txt = re.sub(r']\s*"', '], "', txt)

    # 移除末尾多余逗号
    txt = re.sub(r',\s*([}\]])', r'\1', txt)
    # 清理不可见控制字符
    txt = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', txt)
    return txt


def parse_json_robust(raw: str) -> dict:
    cleaned = deep_clean_json_str(raw)

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as e:
        print(f'  初次解析失败: {e}')

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
    if end_idx == -1:
        end_idx = cleaned.rfind('}')
        if end_idx == -1:
            raise ValueError('未找到 JSON 结束符 "}"')
        left_cnt = cleaned[start:end_idx+1].count('{')
        right_cnt = cleaned[start:end_idx+1].count('}')
        if left_cnt > right_cnt:
            cleaned = cleaned[:end_idx+1] + '}' * (left_cnt - right_cnt)
            end_idx = len(cleaned) - 1

    candidate = cleaned[start:end_idx+1]
    candidate = deep_clean_json_str(candidate)

    try:
        return json.loads(candidate)
    except json.JSONDecodeError as e:
        debug_file = OUTPUT_DIR / 'last_api_response.txt'
        debug_file.write_text(raw, encoding='utf-8')
        print(f'  原始响应已保存至: {debug_file}')
        print(f'  解析错误位置: {e}')
        raise ValueError(f'JSON 最终解析失败: {e}')


SYSTEM = """你是专业英语精读教学助手，专注新闻英语。
目标学习者：考研/六级/专四/专八词汇或短语。
硬性规则：
1. 仅输出纯JSON，无额外文字、注释、代码块；
2. 字符串内换行转义为 \\n，禁止裸换行；
3. 字段名统一使用标准双引号；
4. 数组/对象末尾禁止多余逗号。"""


def build_prompt(transcript: str, date_str: str, source_url: str) -> str:
    # 前置转义特殊字符
    safe_text = transcript.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n')
    # 【重点】已移除 full_translation 全文翻译字段，减少输出Token
    return f"""CNN This Morning 逐字稿（{date_str}）：
{safe_text}

输出标准JSON，严格使用以下结构，字段不可修改、不可新增：
{{
  "date": "{date_str}",
  "source_url": "{source_url}",
  "vocabulary": [
    {{"word":"单词","phonetic":"/音标/","pos":"词性","level":"考研/六级/专四/专八","cn":"释义","en":"英释","excerpt":"原句","example_cn":"翻译"}}
  ],
  "sentences": [
    {{"en":"原句","cn":"翻译","structure":"结构","analysis":"解析"}}
  ],
  "topics": [
    {{"title":"话题","content":"背景","keywords":"关键词"}}
  ]
}}

要求：
1. vocabulary 提取至少30个对应等级词汇短语；
2. sentences 解析本段所有长难句；
3. topics 输出本段相关背景话题；
4. 不生成全文逐段翻译，严格按照上方JSON结构输出。"""


def call_deepseek(prompt: str, max_retries: int = 3) -> dict:
    if not DEEPSEEK_API_KEY:
        raise RuntimeError('DEEPSEEK_API_KEY 未设置')
    for retry in range(max_retries):
        try:
            print(f'  调用 API (重试 {retry+1}/{max_retries})...')
            resp = requests.post(
                DEEPSEEK_URL,
                headers={
                    'Authorization': f'Bearer {DEEPSEEK_API_KEY}',
                    'Content-Type': 'application/json'
                },
                json={
                    'model': 'deepseek-chat',
                    'max_tokens': 16384,
                    'temperature': 0.0,  # 更低随机性，保证JSON格式稳定
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
            print(f'  API返回字符数: {len(raw_content)}')
            return parse_json_robust(raw_content)
        except Exception as e:
            print(f'  第 {retry+1} 次失败: {str(e)}')
            if retry >= max_retries - 1:
                raise
            time.sleep(2 ** retry)


def merge_segment_data(date_str: str, seg_list: list[int]) -> dict:
    """合并当日所有分片数据，已移除 full_translation 字段"""
    print(f'\n[合并分片数据] 共 {len(seg_list)} 个 Segment')
    merged = {
        "date": date_str,
        "source_url": "",
        "raw_transcript": "",
        "segments": [],
        # 不再保留 full_translation，节省存储
        "vocabulary": [],
        "sentences": [],
        "topics": []
    }

    for seg in seg_list:
        seg_file = SEGMENT_CACHE_DIR / f"{date_str}_seg{seg:02d}.json"
        if not seg_file.exists():
            print(f'  跳过缺失分片: {seg_file.name}')
            continue
        try:
            seg_data = json.loads(seg_file.read_text(encoding='utf-8'))
            # 拼接基础内容
            merged["raw_transcript"] += seg_data.get("raw_transcript", "") + "\n\n"
            merged["segments"].extend(seg_data.get("segments", []))
            # 合并核心精读内容
            merged["vocabulary"].extend(seg_data.get("vocabulary", []))
            merged["sentences"].extend(seg_data.get("sentences", []))
            merged["topics"].extend(seg_data.get("topics", []))
            # 主URL取第一个有效链接
            if not merged["source_url"] and seg_data.get("source_url"):
                merged["source_url"] = seg_data["source_url"]
        except Exception as e:
            print(f'  合并分片 {seg} 异常: {e}')
    return merged


def main():
    print('\n=== CNN精读生成器 v8.1 取消全文翻译 · 多分片版 ===')
    requested_date = get_target_date()
    final_out = OUTPUT_DIR / f'{requested_date}.json'

    # 最终文件已存在，直接退出
    if final_out.exists():
        print(f'✓ 当日完整文件已存在: {final_out}')
        return

    # 1. 查找有效日期
    print(f'\n[1/4] 回溯查找有效文稿日期')
    actual_date = find_available_date(requested_date)
    final_out = OUTPUT_DIR / f'{actual_date}.json'
    if final_out.exists():
        print(f'✓ 当日完整文件已存在: {final_out}')
        return

    # 2. 探测当日所有有效Segment
    print(f'\n[2/4] 探测 {actual_date} 全部有效片段')
    valid_segs = get_all_valid_segments(actual_date)
    print(f'✅ 探测到有效片段列表: {[f"s{s:02d}" for s in valid_segs]}')

    # 3. 逐个分片处理、缓存
    print(f'\n[3/4] 逐个分片抓取+生成精读')
    for seg in valid_segs:
        seg_str = f"{seg:02d}"
        seg_cache_file = SEGMENT_CACHE_DIR / f"{actual_date}_seg{seg_str}.json"
        # 分片已缓存，跳过
        if seg_cache_file.exists():
            print(f'✓ 分片 s{seg_str} 已存在，跳过')
            continue

        # 抓取单段文本
        text, seg_info = fetch_transcript(actual_date, seg)
        if not text or not seg_info:
            print(f'⚠️ 分片 s{seg_str} 抓取失败，跳过当前分片')
            continue

        # 调用API生成分片精读
        prompt = build_prompt(text, actual_date, seg_info["url"])
        seg_data = call_deepseek(prompt)
        # 补充分片原始数据
        seg_data["raw_transcript"] = text
        seg_data["segments"] = [seg_info]

        # 保存分片缓存
        seg_cache_file.write_text(json.dumps(seg_data, ensure_ascii=False, indent=2), encoding='utf-8')
        print(f'✅ 分片 s{seg_str} 已缓存: {seg_cache_file.name}\n')

    # 4. 合并所有分片，输出当日最终文件
    print(f'\n[4/4] 合并所有分片，生成最终文件')
    total_data = merge_segment_data(actual_date, valid_segs)
    final_out.write_text(json.dumps(total_data, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'\n🎉 全部完成！最终文件: {final_out}')
    print(f'  合并词汇总数: {len(total_data["vocabulary"])}')
    print(f'  合并长难句总数: {len(total_data["sentences"])}')
    print(f'  合并话题总数: {len(total_data["topics"])}')


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        print(f'\n❌ 任务失败: {e}', file=sys.stderr)
        sys.exit(1)
