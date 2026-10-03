import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from src.domain import ConflictError, PermissionDenied
from src.repository import Repository
from src.service import Service

ROLE = "emergency_manager"


class DispatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)
        self.item_a = self._make_item("A-1")
        self.item_b = self._make_item("A-2")
        self.crew = self.service.create_crew(
            {"name": "抢险一班", "capacity": 1}, "duty1", ROLE)
        self.sandbag = self.service.create_material(
            {"name": "沙袋", "unit": "只", "stock": 100}, "duty1", ROLE)
        self.board = self.service.create_material(
            {"name": "防水板", "unit": "块", "stock": 20}, "duty1", ROLE)

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _make_item(self, ref):
        return self.service.create_item(
            {"title": "缺陷" + ref, "description": "汛期缺陷",
             "severity": "emergency", "quantity": 9, "threshold": 3,
             "external_ref": ref}, "inspector", "inspector")

    def _submit(self, item_id, actor="duty1", qty_sand=50, qty_board=10,
                idem=None):
        payload = {"item_id": item_id, "crew_id": self.crew["id"],
                   "materials": [
                       {"material_id": self.sandbag["id"], "request_qty": qty_sand},
                       {"material_id": self.board["id"], "request_qty": qty_board}]}
        if idem is not None:
            payload["idempotency_key"] = idem
        return self.service.submit_dispatch(payload, actor, ROLE)

    def test_dispatch_success_reserves_capacity_and_material(self):
        d = self._submit(self.item_a["id"])
        self.assertEqual(d["status"], "dispatched")
        self.assertTrue(d["dispatch_no"].startswith("DP-"))
        self.assertEqual(d["gaps"], [])
        crew = self.service.list_crews(ROLE)[0]
        self.assertEqual(crew["available_slots"], 0)
        materials = {m["id"]: m for m in self.service.list_materials(ROLE)}
        self.assertEqual(materials[self.sandbag["id"]]["available_stock"], 50)
        self.assertEqual(materials[self.board["id"]]["available_stock"], 10)

    def test_concurrent_submit_same_defect_only_one_wins(self):
        results = []
        barrier = threading.Barrier(2)

        def submit(actor):
            barrier.wait()
            try:
                d = self._submit(self.item_a["id"], actor=actor, idem=None)
                results.append(("ok", d["dispatch_no"]))
            except ConflictError as exc:
                results.append(("conflict", str(exc)))

        t1 = threading.Thread(target=submit, args=("duty1",))
        t2 = threading.Thread(target=submit, args=("duty2",))
        t1.start(); t2.start(); t1.join(); t2.join()
        self.assertEqual(len(results), 2)
        oks = [r for r in results if r[0] == "ok"]
        conflicts = [r for r in results if r[0] == "conflict"]
        self.assertEqual(len(oks), 1)
        self.assertEqual(len(conflicts), 1)
        self.assertIn("已有进行中的派工", conflicts[0][1])

    def test_crew_capacity_shortage_queues_with_gap(self):
        self._submit(self.item_a["id"])
        d = self._submit(self.item_b["id"])
        self.assertEqual(d["status"], "queued")
        gap_types = {g["type"] for g in d["gaps"]}
        self.assertIn("crew", gap_types)
        crew_gap = next(g for g in d["gaps"] if g["type"] == "crew")
        self.assertEqual(crew_gap["shortage"], 1)

    def test_material_shortage_queues_with_itemized_gap(self):
        d = self._submit(self.item_a["id"], qty_sand=200, qty_board=30)
        self.assertEqual(d["status"], "queued")
        by_type = {g["type"]: g for g in d["gaps"]}
        self.assertIn("material", by_type)
        sand_gap = next(g for g in d["gaps"]
                        if g["material_id"] == self.sandbag["id"])
        self.assertEqual(sand_gap["need"], 200)
        self.assertEqual(sand_gap["available"], 100)
        self.assertEqual(sand_gap["shortage"], 100)
        board_gap = next(g for g in d["gaps"]
                         if g["material_id"] == self.board["id"])
        self.assertEqual(board_gap["shortage"], 10)
        # 排队单不占料
        materials = {m["id"]: m for m in self.service.list_materials(ROLE)}
        self.assertEqual(materials[self.sandbag["id"]]["available_stock"], 100)

    def test_full_receipt_deducts_stock_and_promotes_queue(self):
        first = self._submit(self.item_a["id"])
        waiting = self._submit(self.item_b["id"], qty_sand=50, qty_board=10)
        self.assertEqual(waiting["status"], "queued")
        result = self.service.post_receipt(first["id"], {
            "receipt_no": "RC-1",
            "lines": [
                {"material_id": self.sandbag["id"], "issued_qty": 50},
                {"material_id": self.board["id"], "issued_qty": 10}]}, "duty1", ROLE)
        self.assertEqual(result["outcome"], "full")
        done = self.service.get_dispatch(first["id"], ROLE)
        self.assertEqual(done["status"], "receipted")
        # 库存按实发扣减
        materials = {m["id"]: m for m in self.service.list_materials(ROLE)}
        self.assertEqual(materials[self.sandbag["id"]]["stock"], 50)
        self.assertEqual(materials[self.board["id"]]["stock"], 10)
        # 班组释放后排队单自动顶上
        self.assertIn(waiting["dispatch_no"], result["promoted"])
        self.assertEqual(self.service.get_dispatch(waiting["id"], ROLE)["status"],
                         "dispatched")

    def test_short_receipt_releases_and_retry_keeps_dispatch_no(self):
        d = self._submit(self.item_a["id"], qty_sand=50, qty_board=10)
        # 防水板只发了4块
        result = self.service.post_receipt(d["id"], {
            "receipt_no": "RC-2",
            "lines": [
                {"material_id": self.sandbag["id"], "issued_qty": 50},
                {"material_id": self.board["id"], "issued_qty": 4}]}, "duty1", ROLE)
        self.assertEqual(result["outcome"], "short")
        self.assertEqual(result["dispatch_no"], d["dispatch_no"])
        back = self.service.get_dispatch(d["id"], ROLE)
        self.assertEqual(back["status"], "queued")
        # 已发数量逐项入账，预占全部释放，库存按实发扣减
        line = {m["material_id"]: m for m in back["materials"]}
        self.assertEqual(line[self.sandbag["id"]]["issued_qty"], 50)
        self.assertEqual(line[self.board["id"]]["issued_qty"], 4)
        materials = {m["id"]: m for m in self.service.list_materials(ROLE)}
        self.assertEqual(materials[self.board["id"]]["available_stock"], 16)
        self.assertEqual(materials[self.sandbag["id"]]["stock"], 50)
        # 竞争单占走班组和剩余防水板，排队缺口只算未发的6块
        competitor = self.service.submit_dispatch(
            {"item_id": self.item_b["id"], "crew_id": self.crew["id"],
             "materials": [{"material_id": self.board["id"], "request_qty": 16}]},
            "duty2", ROLE)
        self.assertEqual(competitor["status"], "dispatched")
        again = self.service.retry_dispatch(d["id"], "duty2", ROLE)
        self.assertEqual(again["status"], "queued")
        board_gap = next(g for g in again["gaps"]
                         if g.get("material_id") == self.board["id"])
        self.assertEqual(board_gap["need"], 6)
        # 竞争单完成，班组释放；防水板库存此时为0，再补料10块
        self.service.post_receipt(competitor["id"], {
            "receipt_no": "RC-2B",
            "lines": [{"material_id": self.board["id"], "issued_qty": 16}]},
            "duty2", ROLE)
        self.repo.inbound_material(self.board["id"], 10)
        retried = self.service.retry_dispatch(d["id"], "duty2", ROLE)
        self.assertEqual(retried["status"], "dispatched")
        # 重试沿用原派工号，只补占未发的6块，不重复占沙袋
        self.assertEqual(retried["dispatch_no"], d["dispatch_no"])
        reservations = self.repo.conn.execute(
            "SELECT material_id, qty FROM material_reservations WHERE dispatch_id=?",
            (d["id"],)).fetchall()
        qtys = {r["material_id"]: r["qty"] for r in reservations}
        self.assertEqual(qtys[self.board["id"]], 6)
        self.assertNotIn(self.sandbag["id"], qtys)
        self.assertTrue(self.repo.verify_audit_chain())

    def test_receipt_line_by_line_reconciliation_guards(self):
        d = self._submit(self.item_a["id"])
        with self.assertRaises(ConflictError):
            self.service.post_receipt(d["id"], {
                "receipt_no": "RC-X",
                "lines": [
                    {"material_id": self.sandbag["id"], "issued_qty": 51},
                    {"material_id": self.board["id"], "issued_qty": 10}]}, "duty1", ROLE)
        with self.assertRaises(ConflictError):
            self.service.post_receipt(d["id"], {
                "receipt_no": "RC-X",
                "lines": [
                    {"material_id": self.sandbag["id"], "issued_qty": 50}]}, "duty1", ROLE)
        with self.assertRaises(ConflictError):
            self.service.post_receipt(d["id"], {
                "receipt_no": "RC-X",
                "lines": [
                    {"material_id": self.sandbag["id"], "issued_qty": 50},
                    {"material_id": self.board["id"], "issued_qty": 10},
                    {"material_id": self.board["id"], "issued_qty": 0}]}, "duty1", ROLE)
        # 校验失败不动库存、不改状态
        self.assertEqual(self.service.get_dispatch(d["id"], ROLE)["status"], "dispatched")
        materials = {m["id"]: m for m in self.service.list_materials(ROLE)}
        self.assertEqual(materials[self.sandbag["id"]]["stock"], 100)

    def test_receipt_replay_is_idempotent(self):
        d = self._submit(self.item_a["id"])
        lines = [
            {"material_id": self.sandbag["id"], "issued_qty": 50},
            {"material_id": self.board["id"], "issued_qty": 10}]
        first = self.service.post_receipt(d["id"], {"receipt_no": "RC-3", "lines": lines},
                                          "duty1", ROLE)
        second = self.service.post_receipt(d["id"], {"receipt_no": "RC-3", "lines": lines},
                                           "duty2", ROLE)
        self.assertEqual(first["outcome"], "full")
        self.assertEqual(second["outcome"], "replayed")
        materials = {m["id"]: m for m in self.service.list_materials(ROLE)}
        self.assertEqual(materials[self.sandbag["id"]]["stock"], 50)

    def test_multi_round_receipt_completes_dispatch(self):
        # 首轮防水板短发 -> 回队；重试只补占3块；次轮回执含已发齐项(0)，最终发齐
        d = self._submit(self.item_a["id"], qty_sand=50, qty_board=10)
        self.service.post_receipt(d["id"], {
            "receipt_no": "RC-M1",
            "lines": [
                {"material_id": self.sandbag["id"], "issued_qty": 50},
                {"material_id": self.board["id"], "issued_qty": 7}]}, "duty1", ROLE)
        retried = self.service.retry_dispatch(d["id"], "duty2", ROLE)
        self.assertEqual(retried["status"], "dispatched")
        result = self.service.post_receipt(d["id"], {
            "receipt_no": "RC-M2",
            "lines": [
                {"material_id": self.sandbag["id"], "issued_qty": 0},
                {"material_id": self.board["id"], "issued_qty": 3}]}, "duty2", ROLE)
        self.assertEqual(result["outcome"], "full")
        done = self.service.get_dispatch(d["id"], ROLE)
        self.assertEqual(done["status"], "receipted")
        materials = {m["id"]: m for m in self.service.list_materials(ROLE)}
        self.assertEqual(materials[self.sandbag["id"]]["stock"], 50)
        self.assertEqual(materials[self.board["id"]]["stock"], 10)

    def test_write_failure_releases_reservation_and_retries(self):
        d = self._submit(self.item_a["id"])

        def boom(*args, **kwargs):
            raise sqlite3.OperationalError("disk I/O error")

        self.repo.accept_full_receipt = boom
        result = self.service.post_receipt(d["id"], {
            "receipt_no": "RC-4",
            "lines": [
                {"material_id": self.sandbag["id"], "issued_qty": 50},
                {"material_id": self.board["id"], "issued_qty": 10}]}, "duty1", ROLE)
        del self.repo.accept_full_receipt
        self.assertEqual(result["outcome"], "write_failed")
        self.assertEqual(result["dispatch_no"], d["dispatch_no"])
        back = self.service.get_dispatch(d["id"], ROLE)
        self.assertEqual(back["status"], "queued")
        # 未扣库、预占已释放
        materials = {m["id"]: m for m in self.service.list_materials(ROLE)}
        self.assertEqual(materials[self.sandbag["id"]]["stock"], 100)
        self.assertEqual(materials[self.sandbag["id"]]["available_stock"], 100)
        self.assertIsNone(self.repo.get_receipt_by_no("RC-4"))
        # 重试成功，使用原派工号
        again = self.service.retry_dispatch(d["id"], "duty2", ROLE)
        self.assertEqual(again["status"], "dispatched")
        self.assertEqual(again["dispatch_no"], d["dispatch_no"])

    def test_idempotency_key_replay_returns_same_dispatch(self):
        first = self._submit(self.item_a["id"], idem="submit-1")
        second = self._submit(self.item_a["id"], idem="submit-1")
        self.assertEqual(first["id"], second["id"])

    def test_permission_denied_for_viewer(self):
        with self.assertRaises(PermissionDenied):
            self.service.submit_dispatch(
                {"item_id": self.item_a["id"], "crew_id": self.crew["id"],
                 "materials": [{"material_id": self.sandbag["id"],
                                "request_qty": 1}]}, "viewer", "viewer")


if __name__ == "__main__":
    unittest.main()
