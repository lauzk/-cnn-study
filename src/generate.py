import os
import re
import json
import requests
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from html.parser import HTMLParser

DEEPSEEK_API_KEY = os.environ.get('DEEPSEEK_API_KEY', '')
DEEPSEEK_URL = 'https://api.deepseek.com/v1/chat/completions'
OUTPUT_DIR = Path('output')
OUTPUT_DIR.mkdir(exist_ok=True)
CST = timezone(timedelta(hours=8))

# ... (get_target_date, find_available_date 函数保持不变) ...

# --- 新增 HTML 解析器，用于稳健地提取文本 ---
class TranscriptExtractor(HTMLParser):
    """从混乱的 HTML 中提取文稿内容，保留关键结构"""
    def __init__(self):
        super().__init__()
        self.text_parts = []
        self.in_script = False
        self.skip_tags = {'script', 'style', 'nav', 'header', 'footer', 'aside'}

    def handle_starttag(self, tag, attrs):
        if tag in self.skip_tags:
            self.in_script = True

    def handle_endtag(self, tag):
        if tag in self.skip_tags:
            self.in_script = False

    def handle_data(self, data):
        if not self.in_script:
            stripped = data.strip()
            if stripped and len(stripped) > 20:
                # 清理数据，但保留换行符
                cleaned = re.sub(r'\s+', ' ', stripped).strip()
                if cleaned:
                    self.text_parts.append(cleaned)

    def get_clean_text(self):
        return '\n\n'.join(self.text_parts)

# --- 重写 fetch_transcript 函数，增强抓取逻辑 ---
def fetch_transcript(date_str: str) -> tuple[str, list[dict]]:
    segments_data = []
    seg = 1
    max_segments = 10

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.5',
        'Connection': 'keep-alive',
        'Upgrade-Insecure-Requests': '1',
    }

    for seg in range(1, max_segments + 1):
        url = f'https://transcripts.cnn.com/show/ctmo/date/{date_str}/segment/{seg:02d}'
        print(f'  尝试抓取 Segment {seg:02d}: {url}')
        try:
            response = requests.get(url, headers=headers, timeout=30)
            if response.status_code == 404:
                print(f'  Segment {seg:02d}: 404，停止抓取。')
                break
            if response.status_code != 200:
                print(f'  Segment {seg:02d}: 状态码 {response.status_code}，跳过。')
                continue

            # 方法 1: 寻找特定的 div 区域 (保留原有逻辑)
            body_text = ''
            match = re.search(r'<div[^>]+id=["\']transcriptBody["\'][^>]*>(.*?)</div>', response.text, re.DOTALL)
            if match:
                body_text = match.group(1)
                body_text = re.sub(r'<[^>]+>', ' ', body_text)
                body_text = re.sub(r'\s+', ' ', body_text).strip()
            else:
                # 方法 2: 使用自定义 HTML 解析器提取主要内容
                print(f'      未找到 transcriptBody，尝试使用自定义解析器...')
                parser = TranscriptExtractor()
                parser.feed(response.text)
                body_text = parser.get_clean_text()
                if not body_text or len(body_text) < 300:
                    # 方法 3: 最后的尝试，粗暴地取所有文本
                    print(f'      自定义解析器未提取到足够内容，尝试全文本提取...')
                    text = re.sub(r'<[^>]+>', ' ', response.text)
                    text = re.sub(r'\s+', ' ', text)
                    possible_start = text.find('Aired')
                    if possible_start == -1:
                        possible_start = 0
                    body_text = text[possible_start:possible_start + 15000].strip()

            if len(body_text) > 300:
                segments_data.append({'seg': seg, 'url': url, 'text': body_text})
                print(f'  Segment {seg:02d}: 提取成功，文本长度 {len(body_text)} 字符。')
            else:
                print(f'  Segment {seg:02d}: 提取内容过短 (长度 {len(body_text)})，已无更多内容，停止抓取。')
                break

        except Exception as e:
            print(f'  Segment {seg:02d}: 请求出错 - {e}，停止抓取。')
            break

    if not segments_data:
        return '', []

    full_text = '\n\n'.join(s['text'] for s in segments_data)
    full_text = full_text[:30000]
    return full_text, segments_data

# ... (parse_json_robust, build_prompt, call_deepseek 等函数保持不变) ...
# ... (main 函数保持不变) ...

if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        print(f'\n❌ {e}', file=sys.stderr)
        sys.exit(1)
