import tempfile
import threading
import unittest
from pathlib import Path

from src.domain import ConflictError, NotFoundError
from src.repository import Repository
from src.service import Service


class DispatchFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)
        self.item = self.service.create_item(
            {"title": "渗漏缺陷", "description": "汛期渗漏", "severity": "emergency",
             "quantity": 12, "threshold": 6, "external_ref": "DF-1"},
            "creator", "inspector")
        self.team = self.service.create_team({"name": "应急一班", "capacity": 2},
                                             "boss", "emergency_manager")
        self.mat = self.service.create_material(
            {"name": "堵漏袋", "sku": "MAT-001", "stock": 10, "unit": "个"},
            "boss", "emergency_manager")

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _dispatch(self, dispatch_no, qty=5, team_id=None):
        return self.service.dispatch({
            "dispatch_no": dispatch_no,
            "item_id": self.item["id"],
            "team_id": team_id if team_id is not None else self.team["id"],
            "materials": [{"material_id": self.mat["id"], "qty": qty}],
        }, "值班员", "emergency_manager")

    def test_dispatch_reserves_material_and_team(self):
        order = self._dispatch("D-1", qty=5)
        self.assertEqual(order["status"], "dispatched")
        self.assertEqual(self.repo.held_quantity(self.mat["id"]), 5)
        lines = self.repo.list_dispatch_lines("D-1")
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["status"], "held")
        self.assertEqual(self.repo.count_active_assignments(self.team["id"]), 1)

    def test_queue_when_material_shortage_with_gap(self):
        order = self._dispatch("D-1", qty=20)
        self.assertEqual(order["status"], "queued")
        self.assertIsNone(order["gap"]["team"])
        self.assertEqual(len(order["gap"]["materials"]), 1)
        gap = order["gap"]["materials"][0]
        self.assertEqual(gap["material_id"], self.mat["id"])
        self.assertEqual(gap["required"], 20)
        self.assertEqual(gap["available"], 10)
        self.assertEqual(gap["short"], 10)
        # 排队不预占
        self.assertEqual(self.repo.held_quantity(self.mat["id"]), 0)
        self.assertEqual(self.repo.count_active_assignments(self.team["id"]), 0)

    def test_queue_when_team_shortage_with_gap(self):
        # 班组容量2，先占满
        self._dispatch("D-1", qty=1)
        self._dispatch("D-2", qty=1)
        self.assertEqual(self.repo.count_active_assignments(self.team["id"]), 2)
        # 第三笔班组容量不足 -> 排队并写明班组缺口
        order = self._dispatch("D-3", qty=1)
        self.assertEqual(order["status"], "queued")
        self.assertIsNotNone(order["gap"]["team"])
        self.assertEqual(order["gap"]["team"]["short"], 1)
        self.assertEqual(order["gap"]["team"]["available"], 0)

    def test_duplicate_dispatch_no_conflicts(self):
        self._dispatch("D-1", qty=5)
        with self.assertRaises(ConflictError):
            self._dispatch("D-1", qty=5)

    def test_concurrent_same_dispatch_no_only_one_succeeds(self):
        barrier = threading.Barrier(2)
        results = []

        def worker():
            barrier.wait()
            try:
                results.append(("ok", self._dispatch("D-RACE", qty=5)))
            except ConflictError as exc:
                results.append(("conflict", str(exc)))

        t1 = threading.Thread(target=worker)
        t2 = threading.Thread(target=worker)
        t1.start(); t2.start()
        t1.join(); t2.join()
        oks = [r for r in results if r[0] == "ok"]
        conflicts = [r for r in results if r[0] == "conflict"]
        self.assertEqual(len(oks), 1, "只有一笔派工成功")
        self.assertEqual(len(conflicts), 1, "另一笔因派工号重复失败")
        self.assertEqual(oks[0][1]["dispatch_no"], "D-RACE")

    def test_concurrent_dispatches_no_oversell(self):
        # 班组容量1，两笔不同派工号并发抢同一班组
        team1 = self.service.create_team({"name": "应急二班", "capacity": 1},
                                         "boss", "emergency_manager")
        barrier = threading.Barrier(2)
        results = []

        def worker(dispatch_no):
            barrier.wait()
            try:
                results.append(("ok", self.service.dispatch({
                    "dispatch_no": dispatch_no,
                    "item_id": self.item["id"],
                    "team_id": team1["id"],
                    "materials": [{"material_id": self.mat["id"], "qty": 1}],
                }, "值班员", "emergency_manager")))
            except ConflictError as exc:
                results.append(("conflict", str(exc)))

        t1 = threading.Thread(target=worker, args=("D-A",))
        t2 = threading.Thread(target=worker, args=("D-B",))
        t1.start(); t2.start()
        t1.join(); t2.join()
        statuses = [r[1]["status"] for r in results if r[0] == "ok"]
        self.assertEqual(sorted(statuses), ["dispatched", "queued"],
                         "并发下一笔派工、一笔排队，不得超卖")
        self.assertEqual(self.repo.count_active_assignments(team1["id"]), 1)

    def test_retry_queued_dispatch_reserves(self):
        # 库存不足排队
        order = self._dispatch("D-1", qty=20)
        self.assertEqual(order["status"], "queued")
        self.assertEqual(self.repo.held_quantity(self.mat["id"]), 0)
        # 给 MAT-001 补货后重试
        with self.repo._lock, self.repo.conn:
            self.repo.conn.execute(
                "UPDATE materials SET stock=stock+20 WHERE id=?",
                (self.mat["id"],))
        retried = self.service.retry_dispatch("D-1", "值班员", "emergency_manager")
        self.assertEqual(retried["status"], "dispatched")
        self.assertEqual(self.repo.held_quantity(self.mat["id"]), 20)

    def test_outbound_short_releases_and_retries_no_double_occupy(self):
        self._dispatch("D-1", qty=5)
        self.assertEqual(self.repo.held_quantity(self.mat["id"]), 5)
        # 出库实发不足（要5发3）
        result = self.service.create_outbound_receipt(
            "D-1",
            {"lines": [{"material_id": self.mat["id"], "shipped_qty": 3}]},
            "值班员", "emergency_manager")
        self.assertEqual(result["receipt"]["status"], "short")
        self.assertTrue(result["retried"])
        # 释放后按原派工号重试：重新预占5，而不是叠加成10
        self.assertEqual(result["retried_dispatch"]["dispatch_no"], "D-1")
        self.assertEqual(result["retried_dispatch"]["status"], "dispatched")
        self.assertEqual(self.repo.held_quantity(self.mat["id"]), 5,
                         "重试不得重复占料")
        lines = self.repo.list_dispatch_lines("D-1")
        self.assertEqual(len(lines), 1, "明细行不重复")
        self.assertEqual(lines[0]["status"], "held")

    def test_outbound_write_fail_releases_and_retries(self):
        self._dispatch("D-1", qty=5)
        result = self.service.create_outbound_receipt(
            "D-1",
            {"lines": [{"material_id": self.mat["id"], "shipped_qty": 5}],
             "write_fail": True},
            "值班员", "emergency_manager")
        self.assertEqual(result["receipt"]["status"], "write_failed")
        self.assertTrue(result["retried"])
        self.assertEqual(result["retried_dispatch"]["status"], "dispatched")
        self.assertEqual(self.repo.held_quantity(self.mat["id"]), 5,
                         "写入失败释放后重试不得重复占料")

    def test_outbound_reconciled_completes_and_deducts_stock(self):
        self._dispatch("D-1", qty=5)
        result = self.service.create_outbound_receipt(
            "D-1",
            {"lines": [{"material_id": self.mat["id"], "shipped_qty": 5}]},
            "值班员", "emergency_manager")
        self.assertEqual(result["receipt"]["status"], "reconciled")
        self.assertFalse(result["retried"])
        self.assertEqual(result["dispatch"]["status"], "completed")
        # 库存扣减、预占释放、班组释放
        mat = self.repo.get_material(self.mat["id"])
        self.assertEqual(mat["stock"], 5)
        self.assertEqual(self.repo.held_quantity(self.mat["id"]), 0)
        self.assertEqual(self.repo.count_active_assignments(self.team["id"]), 0)

    def test_retry_is_idempotent_on_held_lines(self):
        self._dispatch("D-1", qty=5)
        # 释放后重试两次，预占始终为5
        self.repo.release_dispatch("D-1")
        self.service.retry_dispatch("D-1", "值班员", "emergency_manager")
        self.assertEqual(self.repo.held_quantity(self.mat["id"]), 5)
        # 已派工状态不可再重试（幂等保护）
        with self.assertRaises(ConflictError):
            self.service.retry_dispatch("D-1", "值班员", "emergency_manager")
        self.assertEqual(self.repo.held_quantity(self.mat["id"]), 5)

    def test_outbound_requires_dispatched_order(self):
        # 不存在的派工单 -> NotFound
        with self.assertRaises(NotFoundError):
            self.service.create_outbound_receipt(
                "D-NOPE",
                {"lines": [{"material_id": self.mat["id"], "shipped_qty": 5}]},
                "值班员", "emergency_manager")
        # 已排队（未预占）的派工单 -> Conflict
        self._dispatch("D-1", qty=20)
        with self.assertRaises(ConflictError):
            self.service.create_outbound_receipt(
                "D-1",
                {"lines": [{"material_id": self.mat["id"], "shipped_qty": 5}]},
                "值班员", "emergency_manager")

    def test_full_flow_audit_chain(self):
        self._dispatch("D-1", qty=5)
        self.service.create_outbound_receipt(
            "D-1",
            {"lines": [{"material_id": self.mat["id"], "shipped_qty": 3}]},
            "值班员", "emergency_manager")
        events = self.service.audit("viewer")
        actions = [e["action"] for e in events]
        self.assertIn("dispatch", actions)
        self.assertIn("receipt", actions)
        self.assertIn("release", actions)
        self.assertIn("retry", actions)
        self.assertTrue(self.repo.verify_audit_chain())


if __name__ == "__main__":
    unittest.main()
