"""快照隔离实测：在预检事务执行期间，由另一会话并发写入并提交。

流程：
1. 开 REPEATABLE READ READ ONLY 事务（模拟服务内事务）
2. 加载目录 -> 记录 equipment 行数（快照值）
3. 另一连接 INSERT 新设备并 COMMIT（业务写入照常提交）
4. 同事务再次查询 equipment 行数，应与步骤 2 一致
5. 新事务中应能看到新设备
"""
import os

import psycopg

URL = os.environ.get("DATABASE_URL", "postgresql://postgres@127.0.0.1:5433/device_ops")

writer = psycopg.connect(URL, autocommit=True)
reader = psycopg.connect(URL)

with reader.transaction():
    reader.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
    with reader.cursor() as cur:
        cur.execute("SELECT count(*) FROM equipment")
        before = cur.fetchone()[0]

        writer.execute(
            "INSERT INTO equipment (id, code, name, location) "
            "VALUES (999, 'EQ-999', '并发写入设备', 'Z 区')"
        )
        wcur = writer.cursor()
        wcur.execute("SELECT count(*) FROM equipment")
        writer_view = wcur.fetchone()[0]

        cur.execute("SELECT count(*) FROM equipment")
        snapshot_view = cur.fetchone()[0]

        cur.execute("SELECT max(id) FROM equipment")
        max_seen = cur.fetchone()[0]

with reader.cursor() as cur:
    cur.execute("SELECT count(*) FROM equipment")
    after_commit_view = cur.fetchone()[0]

writer.execute("DELETE FROM equipment WHERE id = 999")
writer.close()
reader.close()

print(f"快照内写入前行数          : {before}")
print(f"写入方提交后看到的行数    : {writer_view}")
print(f"同快照事务再次看到的行数  : {snapshot_view} (应={before})")
print(f"快照事务看到的最大id      : {max_seen} (应<999)")
print(f"新事务看到的行数          : {after_commit_view} (应={before}+1)")
assert snapshot_view == before, "快照被并发写污染！"
assert max_seen < 999
assert after_commit_view == before + 1
print("PASS: 整份回执取自同一快照，业务写入照常提交且互不影响")
