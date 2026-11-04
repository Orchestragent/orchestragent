#!/usr/bin/env python3
"""Collect the bilingual L calls into the native String Catalog deterministically."""
import json
import re
from pathlib import Path

root = Path(__file__).resolve().parents[1]
strings = {}
pattern = re.compile(r'L\("((?:[^"\\]|\\.)*)",\s*"((?:[^"\\]|\\.)*)"\)', re.S)
for source in sorted((root / 'Sources').glob('*.swift')):
    for english, chinese in pattern.findall(source.read_text()):
        if '\\(' in english or '\\(' in chinese:
            continue
        en, zh = json.loads('"' + english + '"'), json.loads('"' + chinese + '"')
        strings[en] = {'localizations': {language: {'stringUnit': {'state': 'translated', 'value': value}} for language, value in [('en', en), ('zh-Hans', zh)]}}
(root / 'Resources/Localizable.xcstrings').write_text(json.dumps({'sourceLanguage': 'en', 'strings': strings, 'version': '1.0'}, ensure_ascii=False, indent=2, sort_keys=True) + '\n')
print(f'String Catalog: {len(strings)} bilingual entries')
