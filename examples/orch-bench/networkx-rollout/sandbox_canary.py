"""Required local capabilities, checked before constructing any provider."""

import fcntl
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


class RequiredCapabilities(unittest.TestCase):
    def test_threads_and_cross_descriptor_file_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "journal.lock"
            with path.open("w") as first, path.open("a") as second:
                fcntl.flock(first, fcntl.LOCK_EX)
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(second, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(first, fcntl.LOCK_UN)
                fcntl.flock(second, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with ThreadPoolExecutor(max_workers=4) as pool:
                self.assertEqual(list(pool.map(str, range(4))), ["0", "1", "2", "3"])
