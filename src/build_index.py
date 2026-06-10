import json
import re
from pathlib import Path

OUTPUT_DIR = Path('output')
TEMPLATE = Path('src/template.html').read_text(encoding='utf-8')


def extract_date_from_filename(filename: str) -> str:
    match = re.search(r'(\d{4}-\d{2}-\d{2})', filename)
    return match.group(1) if match else ""


def main():
    all_files = sorted(OUTPUT_DIR.glob('*.json'), key=lambda f: f.name, reverse=True)
    if not all_files:
        print('⚠️ 未找到任何 JSON 文件，请先运行 generate.py')
        return

    # 构建目录条目 HTML
    directory_entries = []
    for file_path in all_files:
        date = extract_date_from_filename(file_path.name)
        if not date:
            continue
        try:
            data = json.loads(file_path.read_text(encoding='utf-8'))
            title = ''
            if data.get('segments') and len(data['segments']) > 0:
                title = data['segments'][0].get('title', '')
            if not title:
                title = date
        except Exception:
            title = date
        directory_entries.append(f'''
        <div class="directory-item" data-date="{date}">
            <div class="item-header">
                <span class="item-date">{date}</span>
                <span class="item-title">{escape_html(title)}</span>
            </div>
        </div>
        ''')

    directory_html = '\n'.join(directory_entries) if directory_entries else '<div class="empty">暂无内容</div>'

    # 构建 ALL_DATA 对象
    all_data = {}
    for f in all_files:
        try:
            date = extract_date_from_filename(f.name)
            if date:
                all_data[date] = json.loads(f.read_text(encoding='utf-8'))
        except Exception as e:
            print(f'  跳过损坏文件 {f.name}: {e}')

    all_data_js = 'const ALL_DATA = ' + json.dumps(all_data, ensure_ascii=False, separators=(',', ':')) + ';'
    latest_date = extract_date_from_filename(all_files[0].name) if all_files else ''

    html = TEMPLATE \
        .replace('<!-- DIRECTORY_ENTRIES -->', directory_html) \
        .replace('/* ALL_DATA_PLACEHOLDER */', all_data_js) \
        .replace('<!-- LATEST_DATE -->', latest_date)

    Path('index.html').write_text(html, encoding='utf-8')
    print(f'✅ index.html 已生成，包含 {len(all_files)} 个日期')


def escape_html(s):
    return str(s).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


if __name__ == '__main__':
    main()
