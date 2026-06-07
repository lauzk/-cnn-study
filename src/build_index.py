"""
src/build_index.py
读取 output/*.json，重建 index.html（GitHub Pages 静态前端）
每次 generate.py 运行后自动调用
"""

import json
from pathlib import Path
from datetime import datetime

OUTPUT_DIR = Path('output')
TEMPLATE   = Path('src/template.html').read_text(encoding='utf-8')

def main():
    # 收集所有日期
    dates = sorted(
        [f.stem for f in OUTPUT_DIR.glob('*.json')],
        reverse=True
    )

    if not dates:
        print('No output files found, skipping index rebuild.')
        return

    latest_date = dates[0]
    latest_data = json.loads((OUTPUT_DIR / f'{latest_date}.json').read_text(encoding='utf-8'))

    # 生成历史日期按钮 HTML
    history_html = '\n'.join(
        f'<button class="hist-btn{" active" if d == latest_date else ""}" '
        f'data-date="{d}" onclick="loadDate(\'{d}\')">{d}</button>'
        for d in dates[:30]
    )

    # 嵌入所有数据为 JS 变量（静态，不需要后端）
    all_data_js = 'const ALL_DATA = ' + json.dumps(
        {d: json.loads((OUTPUT_DIR / f'{d}.json').read_text(encoding='utf-8')) for d in dates[:30]},
        ensure_ascii=False
    ) + ';'

    html = TEMPLATE \
        .replace('<!-- HISTORY_BUTTONS -->', history_html) \
        .replace('/* ALL_DATA_PLACEHOLDER */', all_data_js) \
        .replace('<!-- LATEST_DATE -->', latest_date)

    Path('index.html').write_text(html, encoding='utf-8')
    print(f'index.html rebuilt with {len(dates)} dates. Latest: {latest_date}')

if __name__ == '__main__':
    main()
