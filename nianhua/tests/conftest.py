"""测试夹具：每个用例独立临时库 + TestClient。"""
import os
import tempfile

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    monkeypatch.setenv("NIANHUA_DB", path)
    # main 在导入时读取 DB_PATH，需重新导入
    import importlib

    from app import main as main_mod
    importlib.reload(main_mod)
    main_mod.DB_PATH = path
    main_mod.init_db(path)
    with TestClient(main_mod.app) as c:
        yield c
    for ext in ("", "-wal", "-shm"):
        try:
            os.unlink(path + ext)
        except OSError:
            pass


def make_batch(client, title="门神一对"):
    r = client.post("/batches", json={"title": title})
    assert r.status_code == 201, r.text
    bid = r.json()["batch_id"]
    client.post(f"/batches/{bid}/parties",
                json={"side": "handover", "person": {"name": "祖父", "contact": "老宅"}})
    client.post(f"/batches/{bid}/parties",
                json={"side": "receive", "person": {"name": "长孙"}})
    rw = client.post(f"/batches/{bid}/witnesses",
                     json={"person": {"name": "二叔"}, "relation": "家属"})
    return bid, rw.json()["witness_id"]
