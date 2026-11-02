# textstats：文本统计小库（orch 演示）

用 Python 标准库实现一个文本统计包，供 `orch` 端到端演示。不需要命令行入口。

## 需求

1. `tokenize(text: str) -> list[str]`：按非字母数字字符切分并转小写，丢弃空串；
   每个汉字单独算一个词（例如 "你好 world" -> ["你", "好", "world"]）。
2. `word_count(text: str) -> int`：返回词数，空文本为 0。
3. `top_words(text: str, k: int) -> list[tuple[str, int]]`：出现次数最多的 k 个词，
   次数相同时按词的字典序；k <= 0 时抛 ValueError；不足 k 个时返回全部。
4. `reading_time(text: str, wpm: int = 200) -> int`：阅读分钟数，向上取整，
   空文本为 0；wpm <= 0 时抛 ValueError。

## 结构

- `textstats/tokenizer.py`：`tokenize`
- `textstats/stats.py`：其余三个函数，依赖 `tokenize`
- 测试放在 `tests/` 目录。
