"""Held-out behavioral acceptance; never copied into planner/builder context."""

import io
import json
import tempfile
import unittest
from pathlib import Path

from boltons import fileutils, jsonutils, queueutils
from boltons.taskqueueutils import CheckpointQueue


class QueueBehavior(unittest.TestCase):
    def test_backends_preserve_order_tombstones_and_custom_priority(self):
        for cls in (queueutils.HeapPriorityQueue, queueutils.SortedPriorityQueue):
            with self.subTest(backend=cls.__name__):
                calls = []

                def key(value, recorded=calls):
                    recorded.append(value)
                    return value

                queue = cls(priority_key=key)
                for task, priority in [("a", 8), ("b", 2), ("c", 2), ("gone", 0)]:
                    queue.add(task, priority)
                queue.remove("gone")
                queue.add("a", 1)
                count = len(calls)
                self.assertEqual(queue.peek_many(), ["a", "b", "c"])
                self.assertEqual(queue.peek_many(2), ["a", "b"])
                self.assertEqual(queue.peek_many(0), [])
                backend = type(queue._pq)
                self.assertEqual(queue.compact(), 2)
                self.assertEqual(queue.compact(), 0)
                self.assertIs(type(queue._pq), backend)
                self.assertEqual(len(calls), count)
                self.assertEqual([queue.pop(), queue.pop(), queue.pop()], ["a", "b", "c"])
                self.assertEqual(queue.peek_many(), [])
                for invalid in (-1, True, 1.5, "2"):
                    with self.assertRaises(ValueError):
                        queue.peek_many(invalid)


class CheckpointReader(unittest.TestCase):
    def test_bytes_blank_lines_and_partial_tail(self):
        prefix = '"你好"\r\n\n'.encode()
        source = io.BytesIO(prefix + b'{"n":')
        reader = jsonutils.JSONLCheckpointIterator(source)
        self.assertEqual(list(reader), ["你好"])
        self.assertEqual(reader.byte_offset, len(prefix))
        source.seek(0, 2)
        source.write(b'7}\n')
        resumed = jsonutils.JSONLCheckpointIterator(source, reader.byte_offset)
        self.assertEqual(list(resumed), [{"n": 7}])
        self.assertEqual(resumed.byte_offset, len(source.getvalue()))
        self.assertFalse(source.closed)

    def test_invalid_lines_and_offsets(self):
        for bad in (b'not-json\n', b'"\xff"\n'):
            source = io.BytesIO(b'1\n' + bad + b'2\n')
            reader = jsonutils.JSONLCheckpointIterator(source)
            self.assertEqual(next(reader), 1)
            with self.assertRaises(ValueError):
                next(reader)
            self.assertEqual(reader.byte_offset, 2)
            ignored = jsonutils.JSONLCheckpointIterator(source, 2, ignore_errors=True)
            self.assertEqual(list(ignored), [2])
            self.assertEqual(ignored.byte_offset, len(source.getvalue()))
        for offset in (-1, True, 0.5, 99):
            with self.assertRaises(ValueError):
                jsonutils.JSONLCheckpointIterator(io.BytesIO(b'1\n'), offset)


class DurableQueue(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "state.json"

    def test_atomic_json_preserves_original_on_encoding_failure(self):
        fileutils.atomic_save_json(self.path, {"z": "你好", "a": 3})
        expected = '{"a":3,"z":"你好"}\n'.encode()
        self.assertEqual(self.path.read_bytes(), expected)
        for invalid in (float("nan"), {"object": object()}, {"x": float("inf")}):
            with self.assertRaises((ValueError, TypeError)):
                fileutils.atomic_save_json(self.path, invalid)
            self.assertEqual(self.path.read_bytes(), expected)
            self.assertEqual([p.name for p in self.path.parent.iterdir()], ["state.json"])

    def test_inflight_recovery_order_and_completed_dedup(self):
        queue = CheckpointQueue(self.path)
        for key, priority in [("old", 2), ("new", 2), ("urgent", 9)]:
            self.assertTrue(queue.enqueue(key, {"value": key}, priority))
        self.assertEqual(queue.pop(), {"id": "urgent", "payload": {"value": "urgent"},
                                       "priority": 9})
        queue.ack("urgent")
        self.assertEqual(queue.pop()["id"], "old")
        queue.save()
        resumed = CheckpointQueue(self.path)
        self.assertEqual(resumed.peek(), ["old", "new"])
        self.assertFalse(resumed.enqueue("urgent", {}, 100))
        self.assertFalse(resumed.enqueue("old", {}, 100))
        self.assertEqual(resumed.pop()["id"], "old")
        resumed.ack("old")
        self.assertEqual(resumed.pop()["id"], "new")
        resumed.ack("new")
        with self.assertRaises(IndexError):
            resumed.pop()
        with self.assertRaises(KeyError):
            resumed.ack("new")

    def test_ingest_save_resume_and_partial_tail(self):
        first = b'{"id":"a","payload":1,"priority":2}\n'
        second = b'{"id":"b","payload":2,"priority":3}'
        source = io.BytesIO(first + second)
        queue = CheckpointQueue(self.path)
        self.assertEqual(queue.ingest(source, "feed"), 1)
        self.assertEqual(queue.pop()["id"], "a")
        queue.ack("a")
        queue.save()
        source.seek(0, 2)
        source.write(b'\n' + first)
        resumed = CheckpointQueue(self.path)
        self.assertEqual(resumed.ingest(source, "feed"), 1)
        self.assertEqual(resumed.peek(), ["b"])
        self.assertEqual(resumed.ingest(source, "other-feed"), 0)
        resumed.save()
        self.assertEqual(CheckpointQueue(self.path).ingest(source, "feed"), 0)

    def test_bad_record_does_not_checkpoint_past_failure(self):
        good = b'{"id":"a","payload":1,"priority":0}\n'
        bad = b'{"id":"bad","payload":1,"priority":true}\n'
        source = io.BytesIO(good + bad)
        queue = CheckpointQueue(self.path)
        with self.assertRaises(ValueError):
            queue.ingest(source, "feed")
        self.assertEqual(queue.peek(), ["a"])
        queue.save()
        with self.assertRaises(ValueError):
            CheckpointQueue(self.path).ingest(source, "feed")

    def test_invalid_input_is_nonmutating_and_state_version_is_checked(self):
        queue = CheckpointQueue(self.path)
        queue.enqueue("x", 1, 0)
        for task, payload, priority in [("", 1, 1), (None, 1, 0), ("y", object(), 1),
                                        ("x", 1, True), ("y", 1, float("nan"))]:
            with self.assertRaises(ValueError):
                queue.enqueue(task, payload, priority)
            self.assertEqual(queue.peek(), ["x"])
        self.path.write_text(json.dumps({"schema_version": 999}))
        before = self.path.read_bytes()
        with self.assertRaises(ValueError):
            CheckpointQueue(self.path)
        self.assertEqual(self.path.read_bytes(), before)
