"""データ処理のテスト（Discordにつながなくても動く）。  python test_db.py で実行。"""

import unittest

from db import PointsDB, PointsError

ALICE, BOB, ADMIN = 1, 2, 99


class PointsDBTest(unittest.TestCase):
    def setUp(self):
        self.db = PointsDB(":memory:")

    def _approved_candidate(self, message_id=100, user=ALICE, points=10):
        row = self.db.add_candidate(message_id, 1, user, "https://example.com", points, "reaction")
        return self.db.decide_candidate(row["id"], True, ADMIN)

    def test_same_message_is_candidate_only_once(self):
        self.assertIsNotNone(self.db.add_candidate(100, 1, ALICE, "u", 10, "reaction"))
        self.assertIsNone(self.db.add_candidate(100, 1, ALICE, "u", 20, "recommend"))

    def test_double_approve_gives_points_once(self):
        row = self.db.add_candidate(100, 1, ALICE, "u", 10, "reaction")
        self.assertIsNotNone(self.db.decide_candidate(row["id"], True, ADMIN))
        self.assertIsNone(self.db.decide_candidate(row["id"], True, ADMIN))   # 二重押し
        self.assertIsNone(self.db.decide_candidate(row["id"], False, ADMIN))  # 承認後の却下
        self.assertEqual(self.db.balance(ALICE), 10)

    def test_reject_gives_nothing(self):
        row = self.db.add_candidate(100, 1, ALICE, "u", 10, "reaction")
        self.db.decide_candidate(row["id"], False, ADMIN)
        self.assertEqual(self.db.balance(ALICE), 0)

    def test_adjust_cannot_go_negative(self):
        self.db.adjust(ALICE, 30, "テスト", ADMIN)
        self.assertEqual(self.db.adjust(ALICE, -10, "取消", ADMIN), 20)
        with self.assertRaises(PointsError):
            self.db.adjust(ALICE, -21, "取消", ADMIN)
        self.assertEqual(self.db.balance(ALICE), 20)

    def test_exchange_reserves_and_refunds(self):
        self.db.adjust(ALICE, 100, "テスト", ADMIN)
        self.db.upsert_gift("Amazonギフト500円", 80, ADMIN)
        gift = self.db.list_gifts()[0]

        ex = self.db.request_exchange(ALICE, gift["id"])
        self.assertEqual(self.db.balance(ALICE), 20)  # 申請した時点で仮確保
        with self.assertRaises(PointsError):          # 残りでは足りない
            self.db.request_exchange(ALICE, gift["id"])

        self.db.decide_exchange(ex["id"], False, ADMIN)
        self.assertEqual(self.db.balance(ALICE), 100)  # 却下で返却
        self.assertIsNone(self.db.decide_exchange(ex["id"], False, ADMIN))  # 二重返却しない
        self.assertEqual(self.db.balance(ALICE), 100)

    def test_exchange_approve_keeps_points_spent(self):
        self.db.adjust(ALICE, 100, "テスト", ADMIN)
        self.db.upsert_gift("デジタルギフト", 50, ADMIN)
        ex = self.db.request_exchange(ALICE, self.db.list_gifts()[0]["id"])
        self.db.decide_exchange(ex["id"], True, ADMIN)
        self.assertEqual(self.db.balance(ALICE), 50)

    def test_removed_gift_cannot_be_exchanged(self):
        self.db.adjust(ALICE, 100, "テスト", ADMIN)
        self.db.upsert_gift("終了したギフト", 10, ADMIN)
        gift_id = self.db.list_gifts()[0]["id"]
        self.assertTrue(self.db.remove_gift("終了したギフト", ADMIN))
        with self.assertRaises(PointsError):
            self.db.request_exchange(ALICE, gift_id)

    def test_ranking_ignores_spent_points(self):
        self._approved_candidate(100, ALICE, 30)
        self._approved_candidate(101, BOB, 20)
        self.db.upsert_gift("ギフト", 30, ADMIN)
        self.db.request_exchange(ALICE, self.db.list_gifts()[0]["id"])  # 使っても順位は下がらない
        ranking = [(r["user_id"], r["total"]) for r in self.db.monthly_ranking()]
        self.assertEqual(ranking, [(ALICE, 30), (BOB, 20)])

    def test_audit_log_records_actions(self):
        self._approved_candidate()
        actions = [r["action"] for r in self.db.recent_audit()]
        self.assertIn("candidate_approved", actions)
        self.assertIn("candidate_add", actions)


if __name__ == "__main__":
    unittest.main(verbosity=2)
