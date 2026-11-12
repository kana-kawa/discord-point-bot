"""ポイント制度Botのデータ保存（SQLite）。

ポイントの残高は ledger（ポイントの出入りの記録）の合計で計算する。
残高を1つの数字で持たないので、履歴と残高が食い違うことがない。
"""

import datetime
import sqlite3

JST = datetime.timezone(datetime.timedelta(hours=9))

SCHEMA = """
CREATE TABLE IF NOT EXISTS ledger (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    amount     INTEGER NOT NULL,
    kind       TEXT    NOT NULL,  -- earn / adjust / exchange / refund
    reason     TEXT    NOT NULL,
    actor_id   INTEGER,
    created_at TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ledger_user ON ledger(user_id);

CREATE TABLE IF NOT EXISTS candidates (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER NOT NULL UNIQUE,  -- 同じ投稿が2回候補にならないように
    channel_id INTEGER NOT NULL,
    author_id  INTEGER NOT NULL,
    jump_url   TEXT    NOT NULL,
    points     INTEGER NOT NULL,
    source     TEXT    NOT NULL,  -- reaction / recommend
    status     TEXT    NOT NULL DEFAULT 'pending',
    created_at TEXT    NOT NULL,
    decided_by INTEGER,
    decided_at TEXT
);

CREATE TABLE IF NOT EXISTS gifts (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    name   TEXT    NOT NULL UNIQUE,
    cost   INTEGER NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS exchanges (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    gift_id    INTEGER NOT NULL REFERENCES gifts(id),
    gift_name  TEXT    NOT NULL,
    cost       INTEGER NOT NULL,
    status     TEXT    NOT NULL DEFAULT 'pending',
    created_at TEXT    NOT NULL,
    decided_by INTEGER,
    decided_at TEXT
);

CREATE TABLE IF NOT EXISTS audit_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_id   INTEGER,
    action     TEXT NOT NULL,
    detail     TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def now_jst() -> datetime.datetime:
    return datetime.datetime.now(JST)


class PointsError(Exception):
    """利用者に見せてよいエラー（ポイント不足など）。"""


class PointsDB:
    def __init__(self, path: str = "points.db"):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # ---- 内部で使う部品 ----
    def _ts(self) -> str:
        return now_jst().isoformat(timespec="seconds")

    def _ledger(self, user_id, amount, kind, reason, actor_id):
        self.conn.execute(
            "INSERT INTO ledger (user_id, amount, kind, reason, actor_id, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, amount, kind, reason, actor_id, self._ts()),
        )

    def _audit(self, actor_id, action, detail):
        self.conn.execute(
            "INSERT INTO audit_log (actor_id, action, detail, created_at) VALUES (?, ?, ?, ?)",
            (actor_id, action, detail, self._ts()),
        )

    # ---- 残高・履歴・ランキング ----
    def balance(self, user_id: int) -> int:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM ledger WHERE user_id = ?", (user_id,)
        ).fetchone()
        return row[0]

    def history(self, user_id: int, limit: int = 10):
        return self.conn.execute(
            "SELECT * FROM ledger WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()

    def monthly_ranking(self, limit: int = 10, when: datetime.datetime | None = None):
        """その月に獲得したポイント（付与と運営の修正）の多い順。交換で使った分は引かない。"""
        when = when or now_jst()
        month = when.strftime("%Y-%m")
        return self.conn.execute(
            "SELECT user_id, SUM(amount) AS total FROM ledger"
            " WHERE kind IN ('earn', 'adjust') AND substr(created_at, 1, 7) = ?"
            " GROUP BY user_id HAVING total > 0 ORDER BY total DESC, MIN(id) LIMIT ?",
            (month, limit),
        ).fetchall()

    # ---- ポイント付与の候補（リアクション検知・推薦） ----
    def add_candidate(self, message_id, channel_id, author_id, jump_url, points, source):
        """候補を登録する。同じ投稿がすでに候補なら None を返す。"""
        with self.conn:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO candidates"
                " (message_id, channel_id, author_id, jump_url, points, source, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (message_id, channel_id, author_id, jump_url, points, source, self._ts()),
            )
            if cur.rowcount == 0:
                return None
            self._audit(None, "candidate_add", f"候補#{cur.lastrowid} {source} 投稿者={author_id}")
            return self.get_candidate(cur.lastrowid)

    def get_candidate(self, candidate_id):
        return self.conn.execute(
            "SELECT * FROM candidates WHERE id = ?", (candidate_id,)
        ).fetchone()

    def decide_candidate(self, candidate_id, approve: bool, actor_id):
        """承認・却下する。すでに処理済みなら None（ボタンの二重押しで二重付与しない）。"""
        status = "approved" if approve else "rejected"
        with self.conn:
            cur = self.conn.execute(
                "UPDATE candidates SET status = ?, decided_by = ?, decided_at = ?"
                " WHERE id = ? AND status = 'pending'",
                (status, actor_id, self._ts(), candidate_id),
            )
            if cur.rowcount == 0:
                return None
            row = self.get_candidate(candidate_id)
            if approve:
                self._ledger(row["author_id"], row["points"], "earn",
                             f"投稿へのポイント（候補#{candidate_id}）", actor_id)
            self._audit(actor_id, f"candidate_{status}",
                        f"候補#{candidate_id} 投稿者={row['author_id']} {row['points']}pt")
            return row

    # ---- 運営による手動の付与・減算 ----
    def adjust(self, user_id, amount: int, reason: str, actor_id):
        if amount == 0:
            raise PointsError("0ポイントは指定できません。")
        with self.conn:
            if amount < 0 and self.balance(user_id) + amount < 0:
                raise PointsError("残高より多くは減らせません。")
            self._ledger(user_id, amount, "adjust", reason, actor_id)
            self._audit(actor_id, "adjust", f"対象={user_id} {amount:+}pt 理由={reason}")
        return self.balance(user_id)

    # ---- ギフト ----
    def upsert_gift(self, name: str, cost: int, actor_id):
        if cost <= 0:
            raise PointsError("必要ポイントは1以上にしてください。")
        with self.conn:
            self.conn.execute(
                "INSERT INTO gifts (name, cost, active) VALUES (?, ?, 1)"
                " ON CONFLICT(name) DO UPDATE SET cost = excluded.cost, active = 1",
                (name, cost),
            )
            self._audit(actor_id, "gift_upsert", f"{name} {cost}pt")

    def remove_gift(self, name: str, actor_id) -> bool:
        """交換履歴を残すため、削除ではなく「交換できない状態」にする。"""
        with self.conn:
            cur = self.conn.execute(
                "UPDATE gifts SET active = 0 WHERE name = ? AND active = 1", (name,)
            )
            if cur.rowcount:
                self._audit(actor_id, "gift_remove", name)
            return cur.rowcount > 0

    def list_gifts(self):
        return self.conn.execute(
            "SELECT * FROM gifts WHERE active = 1 ORDER BY cost, name"
        ).fetchall()

    # ---- ギフト交換 ----
    def request_exchange(self, user_id, gift_id):
        """交換を申請し、必要ポイントを仮確保する（残高から先に引いておく）。"""
        with self.conn:
            gift = self.conn.execute(
                "SELECT * FROM gifts WHERE id = ? AND active = 1", (gift_id,)
            ).fetchone()
            if gift is None:
                raise PointsError("そのギフトは現在交換できません。")
            if self.balance(user_id) < gift["cost"]:
                raise PointsError(
                    f"ポイントが足りません（必要 {gift['cost']}pt / 残高 {self.balance(user_id)}pt）。"
                )
            cur = self.conn.execute(
                "INSERT INTO exchanges (user_id, gift_id, gift_name, cost, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (user_id, gift["id"], gift["name"], gift["cost"], self._ts()),
            )
            eid = cur.lastrowid
            self._ledger(user_id, -gift["cost"], "exchange",
                         f"{gift['name']}の交換申請（申請#{eid}・仮確保）", user_id)
            self._audit(user_id, "exchange_request", f"申請#{eid} {gift['name']} {gift['cost']}pt")
            return self.get_exchange(eid)

    def get_exchange(self, exchange_id):
        return self.conn.execute(
            "SELECT * FROM exchanges WHERE id = ?", (exchange_id,)
        ).fetchone()

    def decide_exchange(self, exchange_id, approve: bool, actor_id):
        """承認・却下する。却下なら仮確保したポイントを返す。処理済みなら None。"""
        status = "approved" if approve else "rejected"
        with self.conn:
            cur = self.conn.execute(
                "UPDATE exchanges SET status = ?, decided_by = ?, decided_at = ?"
                " WHERE id = ? AND status = 'pending'",
                (status, actor_id, self._ts(), exchange_id),
            )
            if cur.rowcount == 0:
                return None
            row = self.get_exchange(exchange_id)
            if not approve:
                self._ledger(row["user_id"], row["cost"], "refund",
                             f"{row['gift_name']}の交換却下（申請#{exchange_id}・返却）", actor_id)
            self._audit(actor_id, f"exchange_{status}",
                        f"申請#{exchange_id} 申請者={row['user_id']} {row['gift_name']}")
            return row

    # ---- 操作ログ ----
    def recent_audit(self, limit: int = 15):
        return self.conn.execute(
            "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
