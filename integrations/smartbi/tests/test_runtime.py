"""Snapshot/worker tests cross the same interface used by MCP, with no network."""

import threading
import time
import unittest

from kk_smartbi import ParameterError, QueryNotFoundError, ResourceLimitError, ServiceBusyError
from kk_smartbi.query_runtime import QueryRuntime


def records():
    return {"report_id": "test", "table_format": "records", "columns": ["单号", "数值"], "column_count": 2,
            "rows": [{"单号": "0001", "数值": "1.00"}, {"单号": "0002", "数值": "2.00"}],
            "totals": [{"单号": "合计", "数值": "3.00"}], "data_row_count": 2, "truncated": False}


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.runtime = QueryRuntime()
        self.addCleanup(self.runtime.close)

    def test_snapshot_paging_is_immutable_and_never_reexecutes(self):
        source = records()
        calls = []
        query_id = self.runtime.run("test", lambda: (calls.append(True), source)[1])
        source["rows"][0]["数值"] = "changed"
        first = self.runtime.page(query_id, limit=1)
        second = self.runtime.page(query_id, offset=first["next_offset"], limit=1)
        self.assertEqual(first["rows"][0]["数值"], "1.00")
        self.assertEqual(second["rows"][0]["单号"], "0002")
        self.assertFalse(second["source_requeried"])
        first["totals"][0]["数值"] = "modified"
        self.assertEqual(self.runtime.page(query_id)["totals"][0]["数值"], "3.00")
        self.assertEqual(len(calls), 1)

    def test_async_submit_returns_immediately_and_identical_active_queries_deduplicate(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        calls = []

        def slow():
            calls.append(True)
            entered.set()
            release.wait(3)
            return records()

        before = time.monotonic()
        first = self.runtime.start("test", slow, fingerprint="same")
        self.assertLess(time.monotonic() - before, 0.5)
        self.assertTrue(entered.wait(1))
        second = self.runtime.start("test", slow, fingerprint="same")
        self.assertEqual(first["query_id"], second["query_id"])
        self.assertTrue(second["deduplicated"])
        self.assertEqual(self.runtime.status(first["query_id"])["status"], "running")
        with self.assertRaises(ServiceBusyError):
            self.runtime.run("another", records)
        release.set()
        done = self.runtime.status(first["query_id"], wait_seconds=1)
        self.assertEqual(done["status"], "succeeded")
        self.assertIn("snapshot_expires_at", done)
        self.assertEqual(len(calls), 1)

    def test_expired_snapshot_fails_instead_of_silently_requerying(self):
        clock = [0]
        runtime = QueryRuntime(ttl_seconds=5, clock=lambda: clock[0])
        self.addCleanup(runtime.close)
        query_id = runtime.run("test", records)
        clock[0] = 6
        with self.assertRaises(QueryNotFoundError):
            runtime.page(query_id)
        self.assertEqual(runtime.health()["snapshot_serialized_bytes"], 0)

    def test_capacity_evicts_completed_snapshots(self):
        runtime = QueryRuntime(max_entries=1)
        self.addCleanup(runtime.close)
        first = runtime.run("first", records)
        second = runtime.run("second", records)
        with self.assertRaises(QueryNotFoundError):
            runtime.status(first)
        self.assertEqual(runtime.status(second)["status"], "succeeded")

    def test_queue_is_bounded_and_running_job_is_not_evicted(self):
        runtime = QueryRuntime(max_entries=1)
        self.addCleanup(runtime.close)
        release = threading.Event()
        self.addCleanup(release.set)
        first = runtime.start("test", lambda: (release.wait(3), records())[1], fingerprint="one")
        with self.assertRaises(ServiceBusyError):
            runtime.start("test2", records, fingerprint="two")
        self.assertIn(runtime.status(first["query_id"])["status"], {"queued", "running"})
        release.set()

    def test_oversized_result_is_a_failed_job_without_partial_data(self):
        runtime = QueryRuntime(max_cache_bytes=10)
        self.addCleanup(runtime.close)
        query_id = runtime.run("test", records)
        status = runtime.status(query_id)
        self.assertEqual(status["status"], "failed")
        self.assertEqual(status["error"]["code"], "RESOURCE_LIMIT")
        self.assertNotIn("rows", runtime.page(query_id))

    def test_unexpected_exception_text_never_leaks_into_job_status(self):
        def fail():
            raise RuntimeError("SYNTHETIC_SECRET_DO_NOT_ECHO")
        query_id = self.runtime.run("test", fail)
        self.assertNotIn("SYNTHETIC_SECRET", str(self.runtime.status(query_id)))
        self.assertEqual(self.runtime.status(query_id)["error"]["code"], "INTERNAL_ERROR")

    def test_column_projection_and_output_budget_preserve_values(self):
        data = records()
        data["rows"] = [{"单号": f"{n:04}", "数值": "x" * 1500} for n in range(8)]
        query_id = self.runtime.run("test", lambda: data)
        page = self.runtime.page(query_id, limit=8, max_chars=4000)
        self.assertTrue(page["response_limited"])
        self.assertEqual(len(page["rows"][0]["数值"]), 1500)
        self.assertIsNotNone(page["next_offset"])
        selected = self.runtime.page(query_id, column_limit=1, limit=8, max_chars=4000)
        self.assertEqual(len(selected["rows"]), 8)
        self.assertEqual(selected["rows"][0], {"单号": "0000"})
        self.assertTrue(selected["columns_truncated"])

    def test_grid_slice_keeps_parent_merge_context_and_omits_only_plain_empty_cells(self):
        grid = {"table_format": "grid", "columns": [], "column_count": 2, "data_row_count": None, "table_row_count": 3,
                "rows": [{"row_index": 0, "cells": [{"column_index": 0, "text": "东区", "rowspan": 3, "colspan": 1}, {"column_index": 1, "text": "", "rowspan": 1, "colspan": 1}]},
                         {"row_index": 1, "cells": [{"column_index": 1, "text": "001", "rowspan": 1, "colspan": 1}]},
                         {"row_index": 2, "cells": [{"column_index": 1, "text": "002", "rowspan": 1, "colspan": 1}]}], "totals": []}
        query_id = self.runtime.run("test", lambda: grid)
        first = self.runtime.page(query_id, limit=1)
        self.assertEqual(len(first["rows"][0]["cells"]), 1)
        second = self.runtime.page(query_id, offset=1, limit=1)
        self.assertIsNone(second["data_row_count"])
        self.assertEqual(second["merged_context"][0]["cells"][0]["text"], "东区")
        self.assertEqual(second["rows"][0]["cells"][0]["column_index"], 1)
        self.assertEqual(second["totals_scope"], "unclassified")

    def test_invalid_page_arguments_are_explicit(self):
        query_id = self.runtime.run("test", records)
        for values in ({"offset": -1}, {"limit": 0}, {"offset": True}, {"column_limit": 0}, {"column_offset": 2}):
            with self.subTest(values=values), self.assertRaises(ParameterError):
                self.runtime.page(query_id, **values)

    def test_shutdown_does_not_discard_a_running_synchronous_query(self):
        entered, release, closed = threading.Event(), threading.Event(), threading.Event()
        errors = []

        def run():
            try:
                self.runtime.run("test", lambda: (entered.set(), release.wait(2), records())[-1])
            except Exception as exc:
                errors.append(type(exc).__name__)

        worker = threading.Thread(target=run)
        worker.start()
        self.assertTrue(entered.wait(1))
        closer = threading.Thread(target=lambda: (self.runtime.close(), closed.set()))
        closer.start()
        try:
            self.assertFalse(closed.wait(0.05))
        finally:
            release.set()
            worker.join(3)
            closer.join(3)
        self.assertTrue(closed.is_set())
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
