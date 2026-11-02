"""Required runtime capability, checked before any provider dispatch."""

import threading
import unittest


class ThreadCapability(unittest.TestCase):
    def test_threads_can_synchronize(self):
        barrier = threading.Barrier(2)
        results = []

        def worker():
            results.append(barrier.wait(2))

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        results.append(barrier.wait(2))
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(set(results), {0, 1})
