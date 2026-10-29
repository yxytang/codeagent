"""Small deterministic tasks; solution and grader_source are evaluator-only."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from textwrap import dedent


@dataclass(frozen=True)
class TaskCase:
    id: str
    category: str
    objective: str
    files: dict[str, str]
    solution: dict[str, str]
    grader_source: str


def _src(value: str) -> str:
    return dedent(value).lstrip("\n")


def _grader(checks: str) -> str:
    return (
        "import json, os, sys, unittest\n"
        "from pathlib import Path\n"
        "from unittest.mock import patch\n"
        "sys.path.insert(0, str(Path.cwd()))\n\n"
        + _src(checks)
        + "\nif __name__ == '__main__':\n"
        "    result = unittest.TextTestRunner(verbosity=2).run(\n"
        "        unittest.defaultTestLoader.loadTestsFromTestCase(Acceptance))\n"
        "    raise SystemExit(0 if result.wasSuccessful() else 1)\n"
    )


def _public(module: str, checks: str) -> str:
    return (
        f"import unittest\nimport {module}\n\n"
        "class PublicExample(unittest.TestCase):\n"
        "    def test_example(self):\n"
        + "\n".join("        " + line for line in checks.splitlines())
        + "\n\nif __name__ == '__main__':\n    unittest.main()\n"
    )


def _understanding_public(task_id: str, sources: dict[str, str]) -> str:
    """Expose the answer contract, while keeping semantic answers private."""
    schemas = {
        "understand-01": _src('''
            self.assertIs(type(answer), dict)
            self.assertEqual(set(answer), {'results', 'compute_calls', 'keys'})
            self.assertIs(type(answer['compute_calls']), int)
            for field in ['results', 'keys']:
                self.assertIs(type(answer[field]), list)
                for value in answer[field]:
                    self.assertIs(type(value), int)
            self.assertEqual(len(answer['results']), 6)
        '''),
        "understand-02": _src('''
            self.assertIs(type(answer), dict)
            self.assertEqual(set(answer), {'balances', 'accepted'})
            self.assertIs(type(answer['balances']), dict)
            for account, cents in answer['balances'].items():
                self.assertIs(type(account), str)
                self.assertIs(type(cents), int)
            self.assertIs(type(answer['accepted']), list)
            for event_id in answer['accepted']:
                self.assertIs(type(event_id), str)
        '''),
        "understand-03": _src('''
            self.assertIs(type(answer), dict)
            self.assertEqual(set(answer), {'ready', 'waiting', 'blocked'})
            for field in ['ready', 'waiting', 'blocked']:
                self.assertIs(type(answer[field]), list)
                for name in answer[field]:
                    self.assertIs(type(name), str)
        '''),
        "understand-04": _src('''
            self.assertIs(type(answer), list)
            self.assertEqual(len(answer), 4)
            for item in answer:
                self.assertIs(type(item), dict)
                self.assertEqual(set(item), {'result', 'trace'})
                self.assertIs(type(item['result']), str)
                self.assertIs(type(item['trace']), list)
                for event in item['trace']:
                    self.assertIs(type(event), str)
        '''),
    }
    # Normalize newlines in both the fixture and the runtime check, matching the
    # private preservation check across Windows and POSIX text-file writes.
    preserved = {name: hashlib.sha256(text.encode('utf-8')).hexdigest()
                 for name, text in sources.items()}
    return (
        '"""Public checks validate structure and unchanged inputs, not answer values."""\n'
        "import hashlib\nimport json\nfrom pathlib import Path\nimport unittest\n\n"
        f"PRESERVED_SHA256 = {preserved!r}\n\n"
        "class PublicContract(unittest.TestCase):\n"
        "    def test_answer_schema(self):\n"
        "        answer = json.loads(Path('answers.json').read_text(encoding='utf-8'))\n"
        + "".join("        " + line + "\n" for line in schemas[task_id].splitlines())
        + "\n    def test_sources_preserved(self):\n"
        "        for name, expected in PRESERVED_SHA256.items():\n"
        "            with self.subTest(path=name):\n"
        "                content = Path(name).read_text(encoding='utf-8').encode('utf-8')\n"
        "                self.assertEqual(hashlib.sha256(content).hexdigest(), expected)\n"
        "\nif __name__ == '__main__':\n    unittest.main()\n"
    )


def _understanding_cases() -> list[TaskCase]:
    cache = _src('''
        class Memo:
            def __init__(self, capacity=2):
                self.capacity = capacity
                self.data = {}
                self.calls = 0

            def get(self, key):
                if key not in self.data:
                    self.calls += 1
                    self.data[key] = key * key
                    if len(self.data) > self.capacity:
                        del self.data[next(iter(self.data))]
                return self.data[key]
    ''')
    events = _src('''
        def summarize(events):
            balances = {}
            accepted = []
            seen = set()
            for event in events:
                if event["id"] in seen:
                    continue
                seen.add(event["id"])
                if event["status"] != "posted":
                    continue
                account = event["account"]
                amount = event["cents"]
                if event["kind"] == "expense":
                    amount = -amount
                balances[account] = balances.get(account, 0) + amount
                accepted.append(event["id"])
            return {"balances": balances, "accepted": accepted}
    ''')
    scheduler = _src('''
        def classify(tasks, done, failed):
            ready, waiting, blocked = [], [], []
            for name, dependencies in tasks.items():
                if name in done or name in failed:
                    continue
                if any(dep in failed for dep in dependencies):
                    blocked.append(name)
                elif all(dep in done for dep in dependencies):
                    ready.append(name)
                else:
                    waiting.append(name)
            return {"ready": ready, "waiting": waiting, "blocked": blocked}
    ''')
    retry = _src('''
        def execute(outcomes, limit):
            trace = []
            for attempt, value in enumerate(outcomes, 1):
                trace.append("start:" + str(attempt))
                try:
                    if value == "transient":
                        raise TimeoutError("retry")
                    if value == "fatal":
                        raise ValueError("stop")
                    return {"result": value, "trace": trace}
                except TimeoutError:
                    trace.append("retry:" + str(attempt))
                    if attempt >= limit:
                        return {"result": "exhausted", "trace": trace}
                except ValueError:
                    return {"result": "fatal", "trace": trace}
                finally:
                    trace.append("finish:" + str(attempt))
            return {"result": "empty", "trace": trace}
    ''')
    specifications = [
        (
            "understand-01", "cache.py", cache,
            "Read cache.py. For m = Memo(2), call m.get(k) for keys "
            "[2, 3, 2, 4, 3, 2] in that order. Write answers.json with exactly "
            "the keys results (returned values), compute_calls (m.calls), and "
            "keys (list(m.data), in insertion order). Do not change cache.py.",
            {"results": [4, 9, 4, 16, 9, 4], "compute_calls": 4, "keys": [4, 2]},
        ),
        (
            "understand-02", "events.py", events,
            "Read events.py and input.json. Write the result of "
            "summarize(input_events) into answers.json. Compute the balances "
            "and accepted IDs, preserving list order. Do not modify events.py or input.json.",
            {"balances": {"cash": 650, "card": -300}, "accepted": ["a", "c", "e"]},
        ),
        (
            "understand-03", "scheduler.py", scheduler,
            "Read scheduler.py and input.json. Write the result of "
            "classify(tasks, done, failed) to answers.json, using the three "
            "fields in input.json as arguments. Preserve each result list's "
            "order. Do not change the provided input or Python source.",
            {"ready": ["lint"], "waiting": ["ship", "docs"], "blocked": ["test"]},
        ),
        (
            "understand-04", "retry.py", retry,
            "Read retry.py. Write answers.json as a JSON list with the exact "
            "return values of these calls, in order: "
            "execute(['transient', 'ok'], 3), "
            "execute(['transient', 'transient', 'ok'], 2), "
            "execute(['fatal', 'ok'], 3), execute([], 2). Include the complete "
            "trace lists observable by the caller, including finally effects. Do not change retry.py.",
            [
                {"result": "ok", "trace": ["start:1", "retry:1", "finish:1", "start:2", "finish:2"]},
                {"result": "exhausted", "trace": ["start:1", "retry:1", "finish:1", "start:2", "retry:2", "finish:2"]},
                {"result": "fatal", "trace": ["start:1", "finish:1"]},
                {"result": "empty", "trace": []},
            ],
        ),
    ]
    inputs = {
        "understand-02": [
            {"id": "a", "account": "cash", "cents": 1000, "kind": "income", "status": "posted"},
            {"id": "b", "account": "cash", "cents": 200, "kind": "expense", "status": "pending"},
            {"id": "b", "account": "cash", "cents": 200, "kind": "expense", "status": "posted"},
            {"id": "c", "account": "cash", "cents": 350, "kind": "expense", "status": "posted"},
            {"id": "a", "account": "card", "cents": 900, "kind": "income", "status": "posted"},
            {"id": "e", "account": "card", "cents": 300, "kind": "expense", "status": "posted"},
        ],
        "understand-03": {
            "tasks": {"load": [], "parse": ["load"], "lint": ["load"], "test": ["parse"], "ship": ["test", "lint"], "docs": ["lint"]},
            "done": ["load"], "failed": ["parse"],
        },
    }
    cases = []
    for task_id, name, source, objective, expected in specifications:
        files = {name: source}
        if task_id in inputs:
            files["input.json"] = json.dumps(inputs[task_id], ensure_ascii=False, indent=2) + "\n"
        files["test_public.py"] = _understanding_public(task_id, files)
        objective += (
            " Keep test_public.py unchanged and run it with unittest. Its checks "
            "validate JSON structure and unchanged inputs; exact answer values "
            "are checked separately during acceptance."
        )
        checks = (
            "class Acceptance(unittest.TestCase):\n"
            "    def test_answers(self):\n"
            f"        expected = json.loads({json.dumps(json.dumps(expected))})\n"
            "        actual = json.loads(Path('answers.json').read_text(encoding='utf-8'))\n"
            "        self.assertEqual(actual, expected)\n"
            "    def test_sources_preserved(self):\n"
        )
        for path, original in files.items():
            checks += f"        self.assertEqual(Path({path!r}).read_text(encoding='utf-8'), {original!r})\n"
        cases.append(TaskCase(task_id, "code_understanding", objective, files,
                              {"answers.json": json.dumps(expected, indent=2) + "\n"}, _grader(checks)))
    return cases


def _bug_cases() -> list[TaskCase]:
    return [
        TaskCase(
            "bugfix-01", "bug_fix",
            "Fix intervals.merge_intervals: accept unsorted (start, end) pairs, "
            "merge overlapping OR touching closed intervals, return sorted tuples, "
            "never modify input, return [] for empty input, and reject start > end "
            "with ValueError. Keep the public API. Run the public test and add checks as needed.",
            {
                "intervals.py": _src('''
                    def merge_intervals(intervals):
                        result = []
                        for start, end in sorted(intervals):
                            if start > end:
                                raise ValueError("invalid interval")
                            if result and start <= result[-1][1]:
                                result[-1] = (result[-1][0], end)
                            else:
                                result.append((start, end))
                        return result
                '''),
                "test_public.py": _public("intervals", "self.assertEqual(intervals.merge_intervals([(1, 3), (2, 4)]), [(1, 4)])"),
            },
            {"intervals.py": _src('''
                def merge_intervals(intervals):
                    result = []
                    for start, end in sorted(intervals):
                        if start > end:
                            raise ValueError("invalid interval")
                        if result and start <= result[-1][1]:
                            result[-1] = (result[-1][0], max(result[-1][1], end))
                        else:
                            result.append((start, end))
                    return result
            ''')},
            _grader('''
                import intervals
                class Acceptance(unittest.TestCase):
                    def test_nested_touching_and_unsorted(self):
                        data = [(8, 9), (1, 10), (2, 3), (10, 12), (20, 20)]
                        original = list(data)
                        self.assertEqual(intervals.merge_intervals(data), [(1, 12), (20, 20)])
                        self.assertEqual(data, original)
                    def test_empty_separate_and_negative(self):
                        self.assertEqual(intervals.merge_intervals([]), [])
                        self.assertEqual(intervals.merge_intervals([(4, 5), (-3, -1)]), [(-3, -1), (4, 5)])
                    def test_invalid(self):
                        with self.assertRaises(ValueError):
                            intervals.merge_intervals([(3, 2)])
            '''),
        ),
        TaskCase(
            "bugfix-02", "bug_fix",
            "Fix money.parse_cents(text). Parse a decimal string exactly, rounding "
            "to the nearest integer cent with decimal ROUND_HALF_UP (ties away "
            "from zero). Accept surrounding whitespace and signs. Reject invalid "
            "or non-finite values with ValueError. Avoid binary floating point.",
            {
                "money.py": _src('''
                    def parse_cents(text):
                        try:
                            return round(float(text) * 100)
                        except (ValueError, OverflowError) as exc:
                            raise ValueError("invalid amount") from exc
                '''),
                "test_public.py": _public("money", "self.assertEqual(money.parse_cents('12.34'), 1234)"),
            },
            {"money.py": _src('''
                from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

                def parse_cents(text):
                    try:
                        value = Decimal(text.strip())
                        if not value.is_finite():
                            raise ValueError("invalid amount")
                        return int((value * 100).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
                    except (InvalidOperation, TypeError, AttributeError, OverflowError) as exc:
                        raise ValueError("invalid amount") from exc
            ''')},
            _grader('''
                import money
                class Acceptance(unittest.TestCase):
                    def test_exact_half_up(self):
                        for text, cents in [('1.005', 101), ('2.675', 268), ('-1.005', -101), ('  +0.005 ', 1), ('0', 0), ('12.344', 1234), ('9999999.995', 1000000000)]:
                            with self.subTest(text=text):
                                self.assertEqual(money.parse_cents(text), cents)
                    def test_reject_invalid(self):
                        for value in ['NaN', 'Infinity', '-Infinity', 'x', '']:
                            with self.subTest(value=value), self.assertRaises(ValueError):
                                money.parse_cents(value)
            '''),
        ),
        TaskCase(
            "bugfix-03", "bug_fix",
            "Fix pagination.pages(items, size): return a list of consecutive "
            "non-empty list pages of at most size elements, preserving order. "
            "Accept any finite iterable including generators without mutating "
            "it, return [] for empty input, and raise ValueError for size <= 0.",
            {
                "pagination.py": _src('''
                    def pages(items, size):
                        if size <= 0:
                            raise ValueError("size must be positive")
                        return [items[i:i + size] for i in range(0, len(items) + 1, size)]
                '''),
                "test_public.py": _public("pagination", "self.assertEqual(pagination.pages([1, 2, 3], 2), [[1, 2], [3]])"),
            },
            {"pagination.py": _src('''
                def pages(items, size):
                    if size <= 0:
                        raise ValueError("size must be positive")
                    result, page = [], []
                    for item in items:
                        page.append(item)
                        if len(page) == size:
                            result.append(page)
                            page = []
                    if page:
                        result.append(page)
                    return result
            ''')},
            _grader('''
                import pagination
                class Acceptance(unittest.TestCase):
                    def test_boundaries(self):
                        for values, size, expected in [([], 2, []), ([1, 2], 2, [[1, 2]]), ([1, 2, 3], 1, [[1], [2], [3]]), ([1], 5, [[1]])]:
                            original = list(values)
                            self.assertEqual(pagination.pages(values, size), expected)
                            self.assertEqual(values, original)
                    def test_generator(self):
                        self.assertEqual(pagination.pages((i for i in range(5)), 2), [[0, 1], [2, 3], [4]])
                    def test_invalid_size(self):
                        for size in [0, -1]:
                            with self.assertRaises(ValueError):
                                pagination.pages([], size)
            '''),
        ),
        TaskCase(
            "bugfix-04", "bug_fix",
            "Fix ttl_cache.TTLCache. put(key, value, ttl) stores an entry expiring "
            "at clock() + ttl; get returns its value strictly BEFORE expiry, "
            "otherwise removes it and returns default. Preserve falsey values. "
            "Reject negative ttl with ValueError. Use the injected clock, without sleeping.",
            {
                "ttl_cache.py": _src('''
                    class TTLCache:
                        def __init__(self, clock):
                            self.clock = clock
                            self.data = {}

                        def put(self, key, value, ttl):
                            self.data[key] = (value, self.clock() + ttl)

                        def get(self, key, default=None):
                            if key not in self.data:
                                return default
                            value, expires = self.data[key]
                            if self.clock() > expires:
                                del self.data[key]
                                return default
                            return value or default
                '''),
                "test_public.py": _public("ttl_cache", "cache = ttl_cache.TTLCache(lambda: 0)\ncache.put('x', 'ok', 5)\nself.assertEqual(cache.get('x'), 'ok')"),
            },
            {"ttl_cache.py": _src('''
                class TTLCache:
                    def __init__(self, clock):
                        self.clock = clock
                        self.data = {}

                    def put(self, key, value, ttl):
                        if ttl < 0:
                            raise ValueError("ttl must not be negative")
                        self.data[key] = (value, self.clock() + ttl)

                    def get(self, key, default=None):
                        if key not in self.data:
                            return default
                        value, expires = self.data[key]
                        if self.clock() >= expires:
                            del self.data[key]
                            return default
                        return value
            ''')},
            _grader('''
                import ttl_cache
                class Acceptance(unittest.TestCase):
                    def test_time_boundary_and_removal(self):
                        now = [10]
                        cache = ttl_cache.TTLCache(lambda: now[0])
                        cache.put('a', 2, 5)
                        now[0] = 14
                        self.assertEqual(cache.get('a'), 2)
                        now[0] = 15
                        self.assertEqual(cache.get('a', 'missing'), 'missing')
                        self.assertNotIn('a', cache.data)
                    def test_falsey_overwrite_and_zero(self):
                        cache = ttl_cache.TTLCache(lambda: 0)
                        for value in [0, False, '', None]:
                            cache.put('x', value, 1)
                            self.assertIs(cache.get('x', 'missing'), value)
                        cache.put('x', 5, 0)
                        self.assertEqual(cache.get('x', 'missing'), 'missing')
                    def test_negative(self):
                        with self.assertRaises(ValueError):
                            ttl_cache.TTLCache(lambda: 0).put('x', 1, -1)
            '''),
        ),
    ]


def _feature_cases() -> list[TaskCase]:
    return [
        TaskCase(
            "feature-01", "feature",
            "Implement slug.slugify(text): normalize Unicode using NFKD, "
            "discard non-ASCII code points, lowercase, replace each consecutive "
            "run of characters outside a-z and 0-9 with a single hyphen, and "
            "strip leading/trailing hyphens. Return an empty string if no ASCII "
            "letters/digits remain. Input is always a string. Use only stdlib.",
            {"slug.py": "def slugify(text):\n    raise NotImplementedError\n", "test_public.py": _public("slug", "self.assertEqual(slug.slugify('Hello World'), 'hello-world')")},
            {"slug.py": _src(r'''
                import re
                import unicodedata

                def slugify(text):
                    ascii_text = unicodedata.normalize('NFKD', text).encode('ascii', 'ignore').decode()
                    return re.sub(r'[^a-z0-9]+', '-', ascii_text.lower()).strip('-')
            ''')},
            _grader(r'''
                import slug
                class Acceptance(unittest.TestCase):
                    def test_examples(self):
                        for source, expected in [('  Café & Déjà Vu! ', 'cafe-deja-vu'), ('a___B---C', 'a-b-c'), ('123 version 2', '123-version-2'), ('中文', ''), ('', ''), ('---', ''), ('Ångström', 'angstrom')]:
                            with self.subTest(source=source):
                                self.assertEqual(slug.slugify(source), expected)
            '''),
        ),
        TaskCase(
            "feature-02", "feature",
            "Implement config.parse_config(text). Parse newline-separated KEY=VALUE "
            "records: ignore blank lines and lines whose stripped text starts '#'; "
            "split at the first '='; strip key/value whitespace; preserve embedded "
            "'=' and '#' in values. Last duplicate wins. Reject missing '=' or "
            "empty keys with ValueError. Return a dict of strings. No interpolation.",
            {"config.py": "def parse_config(text):\n    raise NotImplementedError\n", "test_public.py": _public("config", "self.assertEqual(config.parse_config('A=1'), {'A': '1'})")},
            {"config.py": _src(r'''
                def parse_config(text):
                    result = {}
                    for line in text.splitlines():
                        line = line.strip()
                        if not line or line.startswith('#'):
                            continue
                        key, separator, value = line.partition('=')
                        key = key.strip()
                        if not separator or not key:
                            raise ValueError('invalid record')
                        result[key] = value.strip()
                    return result
            ''')},
            _grader(r'''
                import config
                class Acceptance(unittest.TestCase):
                    def test_whitespace_duplicates_and_literals(self):
                        text = ' # comment\n A = one \nB=a=b#c\nA=two\nEMPTY=\n\n'
                        self.assertEqual(config.parse_config(text), {'A': 'two', 'B': 'a=b#c', 'EMPTY': ''})
                        self.assertEqual(config.parse_config(''), {})
                        self.assertEqual(config.parse_config('A=$HOME\r\nB=中文'), {'A': '$HOME', 'B': '中文'})
                    def test_malformed(self):
                        for text in ['broken', '=x', '  =x', 'A=1\nwrong']:
                            with self.subTest(text=text), self.assertRaises(ValueError):
                                config.parse_config(text)
            '''),
        ),
        TaskCase(
            "feature-03", "feature",
            "Implement retry.retry_call(fn, attempts=3, retry_on=(TimeoutError,)). "
            "Call zero-argument fn until it returns (including falsey results), "
            "or attempts total calls are exhausted. Retry only exceptions "
            "matching retry_on, and re-raise the last original exception after "
            "exhaustion. Other exceptions propagate immediately. attempts < 1 "
            "raises ValueError before calling fn. No sleeping or extra dependencies.",
            {"retry.py": "def retry_call(fn, attempts=3, retry_on=(TimeoutError,)):\n    raise NotImplementedError\n", "test_public.py": _public("retry", "self.assertEqual(retry.retry_call(lambda: 7), 7)")},
            {"retry.py": _src(r'''
                def retry_call(fn, attempts=3, retry_on=(TimeoutError,)):
                    if attempts < 1:
                        raise ValueError('attempts must be positive')
                    for attempt in range(attempts):
                        try:
                            return fn()
                        except retry_on:
                            if attempt + 1 == attempts:
                                raise
            ''')},
            _grader(r'''
                import retry
                class Acceptance(unittest.TestCase):
                    def test_success_and_retry(self):
                        calls = []
                        def fn():
                            calls.append(1)
                            if len(calls) < 3:
                                raise TimeoutError('later')
                            return 0
                        self.assertEqual(retry.retry_call(fn, 3), 0)
                        self.assertEqual(len(calls), 3)
                    def test_exhaustion_and_unmatched(self):
                        for exc, types, expected_calls in [(TimeoutError('last'), (TimeoutError,), 2), (ValueError('stop'), (TimeoutError,), 1), (ValueError('retry'), (ValueError,), 2)]:
                            calls = []
                            def fn():
                                calls.append(1)
                                raise exc
                            with self.assertRaises(type(exc)) as caught:
                                retry.retry_call(fn, 2, types)
                            self.assertIs(caught.exception, exc)
                            self.assertEqual(len(calls), expected_calls)
                    def test_invalid_attempts(self):
                        for attempts in [0, -1]:
                            with self.assertRaises(ValueError):
                                retry.retry_call(lambda: self.fail('must not run'), attempts)
            '''),
        ),
        TaskCase(
            "feature-04", "feature",
            "Implement report.summarize_csv(text). Input CSV has category,amount "
            "headers. Sum decimal amounts exactly per category, stripping category "
            "whitespace; reject blank categories, missing fields, non-finite or "
            "invalid amounts with ValueError. Round each category TOTAL to cents "
            "with ROUND_HALF_UP and return dict[str, int]. Ignore fully empty rows, "
            "support quoted categories containing commas, and return {} for a "
            "header-only CSV. Do not use float or third-party packages.",
            {"report.py": "def summarize_csv(text):\n    raise NotImplementedError\n", "test_public.py": _public("report", "self.assertEqual(report.summarize_csv('category,amount\\nfood,1.20\\n'), {'food': 120})")},
            {"report.py": _src(r'''
                import csv
                import io
                from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

                def summarize_csv(text):
                    reader = csv.DictReader(io.StringIO(text))
                    if not reader.fieldnames or not {'category', 'amount'} <= set(reader.fieldnames):
                        raise ValueError('missing headers')
                    totals = {}
                    for row in reader:
                        try:
                            category = row['category'].strip()
                            value = Decimal(row['amount'].strip())
                            if not category or not value.is_finite():
                                raise ValueError('invalid row')
                        except (InvalidOperation, TypeError, AttributeError) as exc:
                            raise ValueError('invalid row') from exc
                        totals[category] = totals.get(category, Decimal(0)) + value
                    return {key: int((value * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP)) for key, value in totals.items()}
            ''')},
            _grader(r'''
                import report
                class Acceptance(unittest.TestCase):
                    def test_exact_grouped_totals(self):
                        text = 'category,amount\n food ,0.004\nfood,0.004\n"travel,work",12.34\n"travel,work",-2.335\n\n'
                        self.assertEqual(report.summarize_csv(text), {'food': 1, 'travel,work': 1001})
                        self.assertEqual(report.summarize_csv('category,amount\n'), {})
                    def test_invalid(self):
                        for text in ['x,y\na,1', 'category,amount\n,1', 'category,amount\na,NaN', 'category,amount\na,no', 'category,amount\na', 'category,amount\na,Infinity']:
                            with self.subTest(text=text), self.assertRaises(ValueError):
                                report.summarize_csv(text)
            '''),
        ),
    ]


def _refactor_cases() -> list[TaskCase]:
    return [
        TaskCase(
            "refactor-01", "refactor",
            "Refactor labels.py to remove duplicated normalization. Introduce "
            "clean_text(value), which strips whitespace, collapses internal "
            "whitespace to single spaces, and casefolds. Keep normalize_label "
            "and normalize_owner public APIs and results unchanged, with both "
            "delegating to clean_text. Input strings only. Do not duplicate logic.",
            {"labels.py": _src(r'''
                def normalize_label(value):
                    return ' '.join(value.strip().split()).casefold()

                def normalize_owner(value):
                    return ' '.join(value.strip().split()).casefold()
            '''), "test_public.py": _public("labels", "self.assertEqual(labels.normalize_label('  A   B '), 'a b')")},
            {"labels.py": _src(r'''
                def clean_text(value):
                    return ' '.join(value.strip().split()).casefold()

                def normalize_label(value):
                    return clean_text(value)

                def normalize_owner(value):
                    return clean_text(value)
            ''')},
            _grader(r'''
                import labels
                class Acceptance(unittest.TestCase):
                    def test_compatibility(self):
                        for value, expected in [('  A\t B\n', 'a b'), ('Straße', 'strasse'), ('', ''), (' 用户 ', '用户')]:
                            for fn in [labels.clean_text, labels.normalize_label, labels.normalize_owner]:
                                self.assertEqual(fn(value), expected)
                    def test_both_wrappers_delegate(self):
                        with patch.object(labels, 'clean_text', return_value='sentinel') as helper:
                            self.assertEqual(labels.normalize_label('a'), 'sentinel')
                            self.assertEqual(labels.normalize_owner('b'), 'sentinel')
                            self.assertEqual(helper.call_args_list[0].args, ('a',))
                            self.assertEqual(helper.call_args_list[1].args, ('b',))
            '''),
        ),
        TaskCase(
            "refactor-02", "refactor",
            "Refactor storage.py by extracting TodoRepository(path) with load() "
            "and save(items). Preserve legacy load_items(path)/save_items(path, "
            "items) wrappers, delegating to the repository methods. JSON must "
            "use UTF-8, preserve Unicode and item structure; load returns [] for "
            "a missing file, while malformed JSON propagates JSONDecodeError. "
            "save need not create parent directories. Keep the public test passing.",
            {"storage.py": _src(r'''
                import json
                from pathlib import Path

                def load_items(path):
                    path = Path(path)
                    if not path.exists():
                        return []
                    return json.loads(path.read_text(encoding='utf-8'))

                def save_items(path, items):
                    Path(path).write_text(json.dumps(items, ensure_ascii=False), encoding='utf-8')
            '''), "test_public.py": _public("storage", "import tempfile\nfrom pathlib import Path\nwith tempfile.TemporaryDirectory() as root:\n    path = Path(root) / 'todo.json'\n    storage.save_items(path, [{'title': 'work'}])\n    self.assertEqual(storage.load_items(path), [{'title': 'work'}])")},
            {"storage.py": _src(r'''
                import json
                from pathlib import Path

                class TodoRepository:
                    def __init__(self, path):
                        self.path = Path(path)

                    def load(self):
                        if not self.path.exists():
                            return []
                        return json.loads(self.path.read_text(encoding='utf-8'))

                    def save(self, items):
                        self.path.write_text(json.dumps(items, ensure_ascii=False), encoding='utf-8')

                def load_items(path):
                    return TodoRepository(path).load()

                def save_items(path, items):
                    return TodoRepository(path).save(items)
            ''')},
            _grader(r'''
                import tempfile
                import storage
                class Acceptance(unittest.TestCase):
                    def test_repository_and_legacy(self):
                        with tempfile.TemporaryDirectory() as root:
                            path = Path(root) / 'todo.json'
                            repo = storage.TodoRepository(path)
                            self.assertEqual(repo.load(), [])
                            rows = [{'title': '中文', 'done': False}, {'title': 'a', 'tags': [1, 2]}]
                            repo.save(rows)
                            self.assertEqual(storage.load_items(path), rows)
                            storage.save_items(path, [])
                            self.assertEqual(repo.load(), [])
                            path.write_text('{broken', encoding='utf-8')
                            with self.assertRaises(json.JSONDecodeError):
                                repo.load()
                    def test_wrappers_delegate(self):
                        with patch.object(storage.TodoRepository, 'load', return_value=['sentinel']) as load:
                            self.assertEqual(storage.load_items('unused'), ['sentinel'])
                            load.assert_called_once_with()
                        with patch.object(storage.TodoRepository, 'save', return_value=None) as save:
                            self.assertIsNone(storage.save_items('unused', [1]))
                            save.assert_called_once_with([1])
            '''),
        ),
        TaskCase(
            "refactor-03", "refactor",
            "Refactor accounts.register_user(user, save). Extract pure "
            "validate_user(user), returning a NEW normalized {'name', 'email'} "
            "dict. Keep behavior: strip name/email, lowercase email, require "
            "nonempty name and '@' in email, otherwise ValueError; do not mutate "
            "input. register_user must call validate_user once, then pass its "
            "result to save once and return save's result. Invalid input must "
            "never call save. Preserve the legacy API.",
            {"accounts.py": _src(r'''
                def register_user(user, save):
                    name = user.get('name', '').strip()
                    email = user.get('email', '').strip().lower()
                    if not name or '@' not in email:
                        raise ValueError('invalid user')
                    return save({'name': name, 'email': email})
            '''), "test_public.py": _public("accounts", "self.assertEqual(accounts.register_user({'name': ' A ', 'email': 'A@B.COM'}, lambda x: x), {'name': 'A', 'email': 'a@b.com'})")},
            {"accounts.py": _src(r'''
                def validate_user(user):
                    name = user.get('name', '').strip()
                    email = user.get('email', '').strip().lower()
                    if not name or '@' not in email:
                        raise ValueError('invalid user')
                    return {'name': name, 'email': email}

                def register_user(user, save):
                    return save(validate_user(user))
            ''')},
            _grader(r'''
                import accounts
                class Acceptance(unittest.TestCase):
                    def test_pure_validation(self):
                        user = {'name': ' A ', 'email': ' A@B.COM ', 'extra': 5}
                        original = dict(user)
                        self.assertEqual(accounts.validate_user(user), {'name': 'A', 'email': 'a@b.com'})
                        self.assertEqual(user, original)
                    def test_invalid_has_no_side_effect(self):
                        for user in [{}, {'name': ' ', 'email': 'a@b'}, {'name': 'A', 'email': 'invalid'}]:
                            with self.assertRaises(ValueError):
                                accounts.register_user(user, lambda _: self.fail('save called'))
                    def test_delegation_and_result(self):
                        value = {'name': 'normalized', 'email': 'a@b'}
                        saved = []
                        def save(item):
                            saved.append(item)
                            return 42
                        with patch.object(accounts, 'validate_user', return_value=value) as validate:
                            self.assertEqual(accounts.register_user({'raw': 1}, save), 42)
                            validate.assert_called_once_with({'raw': 1})
                        self.assertEqual(saved, [value])
            '''),
        ),
        TaskCase(
            "refactor-04", "refactor",
            "Refactor formatter.format_report(data, kind) from a conditional "
            "into an extensible FORMATTERS dict mapping names to callable "
            "formatters. Preserve built-in 'json' output (ensure_ascii=False, "
            "sort_keys=True) and 'text' output (sorted 'key=value' lines), and "
            "ValueError for unknown names. Add register_formatter(name, fn), "
            "which rejects non-callables with TypeError and permits replacement. "
            "format_report must resolve the current registry on every call.",
            {"formatter.py": _src(r'''
                import json

                def format_report(data, kind):
                    if kind == 'json':
                        return json.dumps(data, ensure_ascii=False, sort_keys=True)
                    if kind == 'text':
                        return '\n'.join(f'{key}={data[key]}' for key in sorted(data))
                    raise ValueError('unknown format')
            '''), "test_public.py": _public("formatter", "self.assertEqual(formatter.format_report({'b': 2, 'a': 1}, 'text'), 'a=1\\nb=2')")},
            {"formatter.py": _src(r'''
                import json

                def _json(data):
                    return json.dumps(data, ensure_ascii=False, sort_keys=True)

                def _text(data):
                    return '\n'.join(f'{key}={data[key]}' for key in sorted(data))

                FORMATTERS = {'json': _json, 'text': _text}

                def register_formatter(name, fn):
                    if not callable(fn):
                        raise TypeError('formatter must be callable')
                    FORMATTERS[name] = fn

                def format_report(data, kind):
                    try:
                        formatter = FORMATTERS[kind]
                    except KeyError as exc:
                        raise ValueError('unknown format') from exc
                    return formatter(data)
            ''')},
            _grader(r'''
                import formatter
                class Acceptance(unittest.TestCase):
                    def test_builtin_compatibility(self):
                        self.assertEqual(formatter.format_report({'z': '中文', 'a': 1}, 'json'), '{"a": 1, "z": "中文"}')
                        self.assertEqual(formatter.format_report({'z': 2, 'a': 1}, 'text'), 'a=1\nz=2')
                        self.assertEqual(formatter.format_report({}, 'text'), '')
                        with self.assertRaises(ValueError):
                            formatter.format_report({}, 'missing')
                    def test_extension_and_live_registry(self):
                        self.assertIsInstance(formatter.FORMATTERS, dict)
                        formatter.register_formatter('custom', lambda d: str(len(d)))
                        self.assertEqual(formatter.format_report({'a': 1}, 'custom'), '1')
                        with patch.dict(formatter.FORMATTERS, {'text': lambda d: 'replaced'}):
                            self.assertEqual(formatter.format_report({}, 'text'), 'replaced')
                        with self.assertRaises(TypeError):
                            formatter.register_formatter('invalid', 3)
            '''),
        ),
    ]


def _mutation_grader(correct: str, mutants: list[str]) -> str:
    """Run tests in fresh subprocesses against a correct subject and mutants.

    Only test_candidate.py is copied.  A candidate cannot pass acceptance by
    altering its local subject.py.  Private variants never enter Agent inputs.
    """
    harness = _src(r'''
        import json
        from pathlib import Path
        import sys
        import unittest
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import test_candidate
        suite = unittest.defaultTestLoader.loadTestsFromModule(test_candidate)
        count = suite.countTestCases()
        result = unittest.TextTestRunner(verbosity=1).run(suite)
        print('BENCHMARK_TEST_RESULT=' + json.dumps({'tests': count, 'passed': result.wasSuccessful()}))
        raise SystemExit(0 if count > 0 and result.wasSuccessful() else 1)
    ''')
    return _grader(
        "import subprocess, tempfile\n"
        f"CORRECT = {correct!r}\nMUTANTS = {mutants!r}\nHARNESS = {harness!r}\n"
        + _src(r'''
            class Acceptance(unittest.TestCase):
                def run_candidate(self, subject):
                    candidate = Path('test_candidate.py').read_text(encoding='utf-8')
                    with tempfile.TemporaryDirectory(prefix='xcode-mutation-') as directory:
                        root = Path(directory)
                        (root / 'subject.py').write_text(subject, encoding='utf-8')
                        (root / 'test_candidate.py').write_text(candidate, encoding='utf-8')
                        script = root / '_run_tests.py'
                        script.write_text(HARNESS, encoding='utf-8')
                        result = subprocess.run([sys.executable, '-I', '-B', str(script)], cwd=root, capture_output=True, text=True, timeout=8)
                        marker = 'BENCHMARK_TEST_RESULT='
                        lines = [line[len(marker):] for line in result.stdout.splitlines() if line.startswith(marker)]
                        self.assertTrue(lines, 'candidate tests did not complete: ' + result.stderr[-1200:])
                        info = json.loads(lines[-1])
                        self.assertGreater(info['tests'], 0, 'no unittest tests discovered')
                        return result.returncode == 0 and info['passed']

                def test_correct_implementation_passes(self):
                    self.assertTrue(self.run_candidate(CORRECT), 'tests rejected the correct implementation')

                def test_all_mutants_are_detected(self):
                    for index, mutant in enumerate(MUTANTS, 1):
                        with self.subTest(mutant=index):
                            self.assertFalse(self.run_candidate(mutant), 'incorrect variant survived candidate tests')
        ''')
    )


def _test_writing_cases() -> list[TaskCase]:
    palindrome = _src(r'''
        def is_palindrome(text):
            cleaned = ''.join(c.casefold() for c in text if c.isalnum())
            return cleaned == cleaned[::-1]
    ''')
    shipping = _src(r'''
        def shipping_fee(subtotal, member=False):
            if subtotal < 0:
                raise ValueError('negative subtotal')
            return 0 if member or subtotal >= 5000 else 500
    ''')
    lru = _src(r'''
        from collections import OrderedDict

        class LRUCache:
            def __init__(self, capacity):
                if capacity < 1:
                    raise ValueError('capacity must be positive')
                self.capacity = capacity
                self.data = OrderedDict()

            def get(self, key, default=None):
                if key not in self.data:
                    return default
                self.data.move_to_end(key)
                return self.data[key]

            def put(self, key, value):
                self.data[key] = value
                self.data.move_to_end(key)
                if len(self.data) > self.capacity:
                    self.data.popitem(last=False)
    ''')
    intervals = _src(r'''
        def merge_intervals(values):
            result = []
            for start, end in sorted(values):
                if result and start <= result[-1][1]:
                    result[-1] = (result[-1][0], max(result[-1][1], end))
                else:
                    result.append((start, end))
            return result
    ''')
    specs = [
        (
            "tests-01", palindrome,
            "subject.is_palindrome ignores non-alphanumeric characters, uses "
            "Unicode casefold, and considers an empty normalized string a palindrome.",
            [
                palindrome.replace("c.casefold()", "c"),
                palindrome.replace(" if c.isalnum()", ""),
                palindrome.replace("return cleaned ==", "return bool(cleaned) and cleaned =="),
            ],
            _src(r'''
                import unittest
                from subject import is_palindrome

                class PalindromeTests(unittest.TestCase):
                    def test_case_and_punctuation(self):
                        self.assertTrue(is_palindrome('A man, a plan, a canal: Panama!'))
                        self.assertFalse(is_palindrome('ab'))

                    def test_empty(self):
                        self.assertTrue(is_palindrome(''))
                        self.assertTrue(is_palindrome(' ! '))
            '''),
        ),
        (
            "tests-02", shipping,
            "subject.shipping_fee takes an integer subtotal in cents; shipping "
            "is free for members or subtotal >= 5000, otherwise 500 cents. "
            "Negative subtotals raise ValueError even for members.",
            [
                shipping.replace("subtotal >= 5000", "subtotal > 5000"),
                shipping.replace("member or subtotal", "subtotal"),
                shipping.replace("if subtotal < 0:", "if False:"),
            ],
            _src(r'''
                import unittest
                from subject import shipping_fee

                class ShippingTests(unittest.TestCase):
                    def test_threshold_and_member(self):
                        self.assertEqual(shipping_fee(4999), 500)
                        self.assertEqual(shipping_fee(5000), 0)
                        self.assertEqual(shipping_fee(0, True), 0)

                    def test_negative(self):
                        for member in [False, True]:
                            with self.assertRaises(ValueError):
                                shipping_fee(-1, member)
            '''),
        ),
        (
            "tests-03", lru,
            "subject.LRUCache requires capacity >= 1. get returns stored values "
            "including falsey ones, or default on misses, and marks hits most "
            "recent. put overwrites existing keys without evicting another key; "
            "overflow evicts the least recently accessed/updated key.",
            [
                lru.replace("        self.data.move_to_end(key)\n        return self.data[key]", "        return self.data[key]"),
                lru.replace("        self.data[key] = value", "        if key in self.data and len(self.data) >= self.capacity:\n            self.data.popitem(last=False)\n        self.data[key] = value"),
                lru.replace("return self.data[key]", "return self.data[key] or default"),
            ],
            _src(r'''
                import unittest
                from subject import LRUCache

                class CacheTests(unittest.TestCase):
                    def test_hit_updates_recency(self):
                        cache = LRUCache(2)
                        cache.put('a', 1)
                        cache.put('b', 2)
                        self.assertEqual(cache.get('a'), 1)
                        cache.put('c', 3)
                        self.assertIsNone(cache.get('b'))
                        self.assertEqual(cache.get('a'), 1)

                    def test_update_and_falsey(self):
                        cache = LRUCache(2)
                        cache.put('a', 1)
                        cache.put('b', 2)
                        cache.put('b', 3)
                        self.assertEqual(cache.get('a'), 1)
                        cache.put('zero', 0)
                        self.assertEqual(cache.get('zero', 'missing'), 0)

                    def test_invalid_capacity(self):
                        with self.assertRaises(ValueError):
                            LRUCache(0)
            '''),
        ),
        (
            "tests-04", intervals,
            "subject.merge_intervals accepts unsorted valid (start, end) closed "
            "intervals, returns sorted merged tuples for overlapping or touching "
            "intervals, handles nested ranges and empty input, and does not "
            "mutate the input list. Invalid intervals are outside this task's scope.",
            [
                intervals.replace("start <= result[-1][1]", "start < result[-1][1]"),
                intervals.replace("max(result[-1][1], end)", "end"),
                intervals.replace("in sorted(values)", "in values"),
            ],
            _src(r'''
                import unittest
                from subject import merge_intervals

                class IntervalTests(unittest.TestCase):
                    def test_touching_and_nested(self):
                        self.assertEqual(merge_intervals([(1, 3), (3, 5)]), [(1, 5)])
                        self.assertEqual(merge_intervals([(1, 10), (2, 3)]), [(1, 10)])

                    def test_unsorted_and_unchanged(self):
                        data = [(8, 9), (1, 3), (2, 4)]
                        before = list(data)
                        self.assertEqual(merge_intervals(data), [(1, 4), (8, 9)])
                        self.assertEqual(data, before)
                        self.assertEqual(merge_intervals([]), [])
            '''),
        ),
    ]
    cases = []
    for task_id, correct, contract, mutants, solution in specs:
        objective = (
            "Write self-contained standard-library unittest tests in "
            "test_candidate.py for the provided correct subject.py; do not "
            "change subject.py. " + contract + " Tests must pass for the correct "
            "implementation and detect faulty variants of its documented "
            "behavior. At least one test must be discovered by unittest. "
            "Use no external packages or helper files."
        )
        cases.append(TaskCase(
            task_id, "test_writing", objective,
            {"subject.py": correct, "test_candidate.py": "import unittest\n\n# Add unittest.TestCase tests here.\n"},
            {"test_candidate.py": solution}, _mutation_grader(correct, mutants),
        ))
    return cases


def get_cases() -> list[TaskCase]:
    """Return a fresh task suite: five categories, four independent tasks each."""
    return _understanding_cases() + _bug_cases() + _feature_cases() + _refactor_cases() + _test_writing_cases()
