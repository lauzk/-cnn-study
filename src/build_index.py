import json
import re
from pathlib import Path

OUTPUT_DIR = Path('output')
TEMPLATE = Path('src/template.html').read_text(encoding='utf-8')


def extract_date_from_filename(filename: str) -> str:
    """从文件名中提取日期 (YYYY-MM-DD)"""
    match = re.search(r'(\d{4}-\d{2}-\d{2})', filename)
    return match.group(1) if match else ""


def main():
    # 获取所有 JSON 文件并按日期倒序排序
    all_files = sorted(OUTPUT_DIR.glob('*.json'), key=lambda f: f.name, reverse=True)

    # 构建目录条目
    directory_entries = []
    for file_path in all_files:
        date = extract_date_from_filename(file_path.name)
        if not date:
            continue

        try:
            # 读取文件获取标题信息
            data = json.loads(file_path.read_text(encoding='utf-8'))
            # 从 segments 中提取标题，如果没有则使用日期
            title = ''
            if data.get('segments') and len(data['segments']) > 0:
                title = data['segments'][0].get('title', '')
            if not title:
                title = date
        except Exception:
            title = date

        # 构建目录项的 HTML
        directory_entries.append(f'''
        <div class="directory-item" data-date="{date}">
            <div class="item-header">
                <span class="item-date">{date}</span>
                <span class="item-title">{title}</span>
            </div>
            <div class="item-segments">
                <!-- segments 占位，详情页面加载后填充 -->
            </div>
        </div>
        ''')

    # 生成目录 HTML
    if directory_entries:
        directory_html = '\n'.join(directory_entries)
    else:
        directory_html = '<div class="directory-empty">暂无内容，请先运行生成任务。</div>'

    # 生成所有数据的 JS 变量
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

    # 填充模板
    html = TEMPLATE \
        .replace('<!-- DIRECTORY_ENTRIES -->', directory_html) \
        .replace('/* ALL_DATA_PLACEHOLDER */', all_data_js) \
        .replace('<!-- LATEST_DATE -->', latest_date)

    Path('index.html').write_text(html, encoding='utf-8')
    print(f'✅ index.html 已重建（包含 {len(all_files)} 个日期）')


if __name__ == '__main__':
    main()
