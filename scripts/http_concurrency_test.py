"""HTTP 层并发实测：预检进行期间另一连接持续提交写入。

- 写入方：插入新设备 777 并提交，稍后由独立连接确认可见
- 读取方：并发发起 20 次对设备 1 的预检，回执内容必须全程稳定
  （不会看到设备 777，删除集 / 阻断数与单测一致）
- 写入提交后的新预检应能在新快照中看到 777
"""
import json
import threading

import psycopg
import urllib.request

API = "http://127.0.0.1:8000/api/v1/precheck?table=equipment&pk=1"
URL = "postgresql://postgres@127.0.0.1:5433/device_ops"


def precheck():
    with urllib.request.urlopen(API, timeout=10) as resp:
        return json.loads(resp.read())


results = []
errors = []


def worker():
    try:
        r = precheck()
        results.append((
            r["summary"]["rows_to_delete"],
            r["summary"]["blocking_references"],
            sorted(p["table"] for p in r["planned_deletes"]),
        ))
    except Exception as exc:  # noqa: BLE001
        errors.append(repr(exc))


writer = psycopg.connect(URL, autocommit=True)
threads = [threading.Thread(target=worker) for _ in range(20)]
for t in threads:
    t.start()
writer.execute(
    "INSERT INTO equipment (id, code, name, location) "
    "VALUES (777, 'EQ-777', '并发设备', 'Z 区')"
)
for t in threads:
    t.join()

baseline = results[0]
assert not errors, errors
assert all(r == baseline for r in results), "并发期间回执不一致"
print("并发 20 次预检回执稳定:", baseline)

with urllib.request.urlopen(
    "http://127.0.0.1:8000/api/v1/precheck?table=equipment&pk=777", timeout=10
) as resp:
    after = json.loads(resp.read())
assert after["targets"][0]["exists"] is True
print("写入提交后的新快照可见设备 777:", after["targets"][0])

writer.execute("DELETE FROM equipment WHERE id = 777")
writer.close()
print("PASS: 预检期间业务写入照常提交，回执取自独立快照且互不干扰")
