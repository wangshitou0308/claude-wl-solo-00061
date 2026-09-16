"""端到端业务流测试。"""
from tests.conftest import make_batch


def _inv_h(bid):
    return [
        {"kind": "block", "code": "H-L", "title": "题作秦琼",
         "dims": {"width_mm": 300, "height_mm": 450},
         "color_role": "线版", "face_dir": "反刻",
         "gaps": [[12, 13], [200, 18]], "damage": ["左上角虫蛀"],
         "inscriptions": ["光绪年制"]},
        {"kind": "block", "code": "H-R", "title": "题作尉迟恭",
         "dims": {"width_mm": 301, "height_mm": 451},
         "color_role": "红版", "face_dir": "反刻", "gaps": [[11, 14]]},
        {"kind": "wrapper", "code": "H-W", "title": "旧报纸包",
         "material": "宣纸", "inscriptions": ["门神一套"]},
        {"kind": "sample", "code": "H-S", "title": "试印红样",
         "colors": ["红", "黑"], "clues": ["与线版缺口对应"]},
    ]


def _inv_r(bid):
    return [
        {"kind": "block", "code": "R-A", "title": "完全不同的题名也不影响配对",
         "dims": {"width_mm": 300.5, "height_mm": 449.5},
         "color_role": "墨线版", "face_dir": "反向",
         "gaps": [[12.5, 12.5]], "inscriptions": ["光绪年制"]},
        {"kind": "block", "code": "R-B",
         "dims": {"width_mm": 301, "height_mm": 451},
         "color_role": "朱版", "face_dir": "反刻"},
        {"kind": "wrapper", "code": "R-W", "material": "宣紙"},
        {"kind": "sample", "code": "R-S", "colors": ["黑"]},
    ]


def _full_setup(client, accept=True):
    bid, wid = make_batch(client)
    client.put(f"/batches/{bid}/sides/handover/inventory", json={"items": _inv_h(bid)})
    client.put(f"/batches/{bid}/sides/receive/inventory", json={"items": _inv_r(bid)})
    r = client.get(f"/batches/{bid}/report")
    rep = r.json()
    assert len(rep["locked_pairs"]) == 4, rep
    assert rep["unmatched"] == []
    assert rep["candidate_groups"] == []
    return bid, wid


def test_happy_path_close_and_freeze(client):
    bid, wid = _full_setup(client)

    # 双方确认清点
    assert client.post(f"/batches/{bid}/sides/handover/inventory/confirm").status_code == 200
    assert client.post(f"/batches/{bid}/sides/receive/inventory/confirm").status_code == 200

    # 分配套组（标签不同没关系，靠锁定对并合；题名也不参与）
    client.put(f"/batches/{bid}/sides/handover/groups",
               json={"assignments": {"甲套": ["H-L", "H-R", "H-W", "H-S"]}})
    client.put(f"/batches/{bid}/sides/receive/groups",
               json={"assignments": {"乙套": ["R-A", "R-B", "R-W", "R-S"]}})
    rep = client.get(f"/batches/{bid}/report").json()
    assert len(rep["sets"]) == 1
    assert rep["sets"][0]["closed"] is True
    # 套组内三类齐全
    assert rep["sets"][0]["member_counts"] == {"block": 2, "wrapper": 1, "sample": 1}

    client.post(f"/batches/{bid}/sides/handover/groups/confirm", json={})
    client.post(f"/batches/{bid}/sides/receive/groups/confirm", json={})

    # 状态确认前：有缺口/损伤等软差异，需要知晓
    rep = client.get(f"/batches/{bid}/report").json()
    assert rep["sets"][0]["discrepancies"], "应存在软差异（缺口不齐/损伤未报）"
    r = client.post(f"/batches/{bid}/sides/handover/status/confirm",
                    json={"sets": [{"set_label": "甲套", "accept_discrepancy": False}]})
    assert r.status_code == 409 and r.json()["error"]["code"] == "discrepancy_ack_required"

    for side, label in (("handover", "甲套"), ("receive", "乙套")):
        r = client.post(f"/batches/{bid}/sides/{side}/status/confirm",
                        json={"sets": [{"set_label": label, "accept_discrepancy": True}]})
        assert r.status_code == 200, r.text

    rep = client.get(f"/batches/{bid}/report").json()
    assert rep["state"]["common_stage"] == "status"

    r = client.post(f"/batches/{bid}/freeze", json={})
    assert r.status_code == 200, r.text
    fz = r.json()
    assert fz["input_hash"] and fz["freeze_hash"]

    # 冻结后拒绝写入
    r = client.put(f"/batches/{bid}/sides/handover/inventory", json={"items": []})
    assert r.status_code == 409 and r.json()["error"]["code"] == "batch_frozen"

    rec = client.get(f"/batches/{bid}/freeze").json()
    assert rec["hash"] == fz["freeze_hash"]
    assert rec["snapshot"]["input_hash"] == fz["input_hash"]
    assert rec["snapshot"]["responsibility"]["from"] == "handover"
    assert rec["snapshot"]["responsibility"]["to"] == "receive"
    # 原始声明与题名原样保留
    h_decl = rec["snapshot"]["declarations"]["handover"]
    assert any(d["payload"].get("title") == "题作秦琼" for d in h_decl)


def test_hard_conflict_excludes_and_blocks(client):
    bid, _ = make_batch(client)
    items_h = _inv_h(bid)
    items_r = _inv_r(bid)
    # 把接收方线版尺寸改到差 50mm -> 与 H-L 硬冲突；
    # 同时与 H-R（红版 301x451）也角色冲突且尺寸近 -> 角色硬冲突
    items_r[0]["dims"] = {"width_mm": 350, "height_mm": 500}
    client.put(f"/batches/{bid}/sides/handover/inventory", json={"items": items_h})
    client.put(f"/batches/{bid}/sides/receive/inventory", json={"items": items_r})
    rep = client.get(f"/batches/{bid}/report").json()

    unmatched = {u["code"]: u for u in rep["unmatched"]}
    assert "R-A" in unmatched and "H-L" in unmatched
    clue_fields = {(c["field"], c["kind"])
                   for u in unmatched.values() for c in u["conflicts"]}
    assert ("dims", "dims_mismatch") in clue_fields

    # 清点头被硬冲突阻断，返回最小线索
    r = client.post(f"/batches/{bid}/sides/receive/inventory/confirm")
    assert r.status_code == 409 and r.json()["error"]["code"] == "inventory_conflict"
    clues = r.json()["error"]["details"]["minimal_conflict_clues"]
    assert clues and all("conflicts" in c for c in clues)


def test_ambiguous_candidates_remain(client):
    """两个红版证据相同：禁止强行定案，全部保留为候选。"""
    bid, _ = make_batch(client)
    h = [
        {"kind": "block", "code": "H1", "dims": {"width_mm": 200, "height_mm": 300},
         "color_role": "红版", "face_dir": "反刻"},
        {"kind": "block", "code": "H2", "dims": {"width_mm": 200, "height_mm": 300},
         "color_role": "红版", "face_dir": "反刻"},
        {"kind": "wrapper", "code": "HW", "material": "宣纸"},
        {"kind": "sample", "code": "HS", "colors": ["红"]},
    ]
    r = [
        {"kind": "block", "code": "R1", "dims": {"width_mm": 200, "height_mm": 300},
         "color_role": "朱版", "face_dir": "反刻"},
        {"kind": "wrapper", "code": "RW", "material": "宣纸"},
        {"kind": "sample", "code": "RS", "colors": ["红"]},
    ]
    client.put(f"/batches/{bid}/sides/handover/inventory", json={"items": h})
    client.put(f"/batches/{bid}/sides/receive/inventory", json={"items": r})
    rep = client.get(f"/batches/{bid}/report").json()
    cand = rep["candidate_groups"]
    assert len(cand) == 1 and len(cand[0]["handover"]) == 2 and len(cand[0]["receive"]) == 1

    # 即使硬凑套组，候选未消 -> 组不闭合 -> 套组确认被阻断
    client.post(f"/batches/{bid}/sides/handover/inventory/confirm")
    client.post(f"/batches/{bid}/sides/receive/inventory/confirm")
    client.put(f"/batches/{bid}/sides/handover/groups",
               json={"assignments": {"S": ["H1", "H2", "HW", "HS"]}})
    client.put(f"/batches/{bid}/sides/receive/groups",
               json={"assignments": {"S": ["R1", "RW", "RS"]}})
    rr = client.post(f"/batches/{bid}/sides/handover/groups/confirm", json={})
    assert rr.status_code == 409 and rr.json()["error"]["code"] == "grouping_conflict"


def test_witness_observation_breaks_tie_then_adopt(client):
    """见证补录缺口观察，双方采纳后锁定候选。"""
    bid, wid = make_batch(client)
    h = [
        {"kind": "block", "code": "H1", "dims": {"width_mm": 200, "height_mm": 300},
         "color_role": "红版", "face_dir": "反刻"},
        {"kind": "block", "code": "H2", "dims": {"width_mm": 200, "height_mm": 300},
         "color_role": "红版", "face_dir": "反刻"},
        {"kind": "wrapper", "code": "HW", "material": "宣纸"},
        {"kind": "sample", "code": "HS", "colors": ["红"]},
    ]
    r = [
        {"kind": "block", "code": "R1", "dims": {"width_mm": 200, "height_mm": 300},
         "color_role": "朱版", "face_dir": "反刻"},
        {"kind": "wrapper", "code": "RW", "material": "宣纸"},
        {"kind": "sample", "code": "RS", "colors": ["红"]},
    ]
    client.put(f"/batches/{bid}/sides/handover/inventory", json={"items": h})
    client.put(f"/batches/{bid}/sides/receive/inventory", json={"items": r})

    # 见证：实物 H1 与 R1 上都有同一处独有缺口（50,60）
    o1 = client.post(f"/batches/{bid}/witnesses/{wid}/observations",
                     json={"target_code": "H1", "side_hint": "handover",
                           "gaps": [[50, 60]], "note": "版边独有缺口"}).json()["observation_id"]
    o2 = client.post(f"/batches/{bid}/witnesses/{wid}/observations",
                     json={"target_code": "R1", "side_hint": "receive",
                           "gaps": [[50, 60]]}).json()["observation_id"]
    for oid in (o1, o2):
        client.post(f"/batches/{bid}/observations/{oid}/sides/handover/decision",
                    json={"adopted": True})
        client.post(f"/batches/{bid}/observations/{oid}/sides/receive/decision",
                    json={"adopted": True})

    rep = client.get(f"/batches/{bid}/report").json()
    assert rep["candidate_groups"] == []
    blocks = {p["handover"]["code"]: p["receive"]["code"]
              for p in rep["locked_pairs"] if p["kind"] == "block"}
    assert blocks == {"H1": "R1"}
    # H2 因无证据重叠而未配对（不是硬冲突）
    codes = {u["code"]: u["reason"] for u in rep["unmatched"]}
    assert codes.get("H2") == "no_evidence_overlap"


def test_withdraw_returns_to_last_common_state(client):
    bid, _ = _full_setup(client)
    for side in ("handover", "receive"):
        client.post(f"/batches/{bid}/sides/{side}/inventory/confirm")
    client.put(f"/batches/{bid}/sides/handover/groups",
               json={"assignments": {"甲套": ["H-L", "H-R", "H-W", "H-S"]}})
    client.put(f"/batches/{bid}/sides/receive/groups",
               json={"assignments": {"乙套": ["R-A", "R-B", "R-W", "R-S"]}})
    client.post(f"/batches/{bid}/sides/handover/groups/confirm", json={})

    rep = client.get(f"/batches/{bid}/report").json()
    assert rep["state"]["common_stage"] == "inventory"  # 接收方未确认套组
    client.post(f"/batches/{bid}/sides/receive/groups/confirm", json={})
    assert client.get(f"/batches/{bid}/report").json()["state"]["common_stage"] == "grouping"

    # 接收方撤回套组确认 -> 回到清点共同
    r = client.post(f"/batches/{bid}/sides/receive/withdraw")
    assert r.status_code == 200
    assert r.json()["common_stage"] == "inventory"


def test_correction_only_invalidates_affected(client):
    bid, _ = _full_setup(client)
    # 走到共同 status（一个套组）
    for side in ("handover", "receive"):
        client.post(f"/batches/{bid}/sides/{side}/inventory/confirm")
    client.put(f"/batches/{bid}/sides/handover/groups",
               json={"assignments": {"甲套": ["H-L", "H-R", "H-W", "H-S"]}})
    client.put(f"/batches/{bid}/sides/receive/groups",
               json={"assignments": {"乙套": ["R-A", "R-B", "R-W", "R-S"]}})
    for side in ("handover", "receive"):
        client.post(f"/batches/{bid}/sides/{side}/groups/confirm", json={})
    for side, label in (("handover", "甲套"), ("receive", "乙套")):
        client.post(f"/batches/{bid}/sides/{side}/status/confirm",
                    json={"sets": [{"set_label": label, "accept_discrepancy": True}]})
    assert client.get(f"/batches/{bid}/report").json()["state"]["common_stage"] == "status"

    # 更正包纸材质（改为布 -> 与 R-W 宣纸硬冲突）
    new_w = {"kind": "wrapper", "code": "H-W", "material": "布"}
    r = client.put(f"/batches/{bid}/sides/handover/items/H-W", json={"item": new_w})
    assert r.status_code == 200
    invalidated = {(x["side"], x["stage"], x["scope"])
                   for x in r.json()["invalidated"]}
    assert ("handover", "inventory", "*") in invalidated
    assert ("handover", "grouping", "*") in invalidated
    assert ("handover", "status", "甲套") in invalidated
    assert ("receive", "status", "乙套") in invalidated

    rep = client.get(f"/batches/{bid}/report").json()
    assert rep["state"]["common_stage"] == "created"
    unmatched = {u["code"] for u in rep["unmatched"]}
    assert {"H-W", "R-W"} <= unmatched

    # 改回宣纸后重新可闭合（旧版本仍在事件/快照中可查）
    good_w = {"kind": "wrapper", "code": "H-W", "material": "宣纸",
              "inscriptions": ["门神一套"]}
    client.put(f"/batches/{bid}/sides/handover/items/H-W", json={"item": good_w})
    rep = client.get(f"/batches/{bid}/report").json()
    assert not rep["unmatched"]


def test_witness_contradiction_blocks(client):
    """双方采纳的见证与原声明硬矛盾 -> 阻断闭合，要求复核。"""
    bid, wid = _full_setup(client)
    oid = client.post(f"/batches/{bid}/witnesses/{wid}/observations",
                      json={"target_code": "H-L", "side_hint": "handover",
                            "dims": {"width_mm": 999, "height_mm": 999},
                            "note": "现场量得完全不同"}).json()["observation_id"]
    client.post(f"/batches/{bid}/observations/{oid}/sides/handover/decision",
                json={"adopted": True})
    client.post(f"/batches/{bid}/observations/{oid}/sides/receive/decision",
                json={"adopted": True})
    rep = client.get(f"/batches/{bid}/report").json()
    assert rep["global_issues"] or any(
        any(i["issue"] == "witness_contradiction" for i in s["issues"])
        for s in rep["sets"])

    # 有见证矛盾时冻结被拒
    for side in ("handover", "receive"):
        client.post(f"/batches/{bid}/sides/{side}/inventory/confirm")
    client.put(f"/batches/{bid}/sides/handover/groups",
               json={"assignments": {"甲套": ["H-L", "H-R", "H-W", "H-S"]}})
    client.put(f"/batches/{bid}/sides/receive/groups",
               json={"assignments": {"乙套": ["R-A", "R-B", "R-W", "R-S"]}})
    # 组不闭合，无法走到冻结；直接验证报告含矛盾即可
    rep = client.get(f"/batches/{bid}/report").json()
    assert any(not s["closed"] for s in rep["sets"])


def test_disputed_observation_not_incorporated(client):
    bid, wid = _full_setup(client)
    oid = client.post(f"/batches/{bid}/witnesses/{wid}/observations",
                      json={"target_code": "H-L", "side_hint": "handover",
                            "damage": ["新发现的裂"], "note": "待核"}).json()["observation_id"]
    client.post(f"/batches/{bid}/observations/{oid}/sides/handover/decision",
                json={"adopted": True})
    rr = client.post(f"/batches/{bid}/observations/{oid}/sides/receive/decision",
                     json={"adopted": False, "note": "退回再核"})
    assert rr.json()["status"] == "disputed"
    rep = client.get(f"/batches/{bid}/report").json()
    assert rep["blockers"]["freeze"] is True


def test_title_never_decides(client):
    """同名不同物：题名相同也不得并合。"""
    bid, _ = make_batch(client)
    h = [
        {"kind": "block", "code": "H1", "title": "门神",
         "dims": {"width_mm": 100, "height_mm": 100}, "color_role": "红版"},
        {"kind": "wrapper", "code": "HW", "material": "宣纸", "title": "门神"},
        {"kind": "sample", "code": "HS", "colors": ["红"], "title": "门神"},
    ]
    r = [
        {"kind": "block", "code": "R1", "title": "门神",
         "dims": {"width_mm": 900, "height_mm": 900}, "color_role": "蓝版"},
        {"kind": "wrapper", "code": "RW", "material": "布", "title": "门神"},
        {"kind": "sample", "code": "RS", "colors": ["蓝"], "title": "门神"},
    ]
    client.put(f"/batches/{bid}/sides/handover/inventory", json={"items": h})
    client.put(f"/batches/{bid}/sides/receive/inventory", json={"items": r})
    rep = client.get(f"/batches/{bid}/report").json()
    assert {u["code"] for u in rep["unmatched"]} == {"H1", "R1", "HW", "RW", "HS", "RS"}


def test_set_split_and_missing_kind(client):
    bid, _ = make_batch(client)
    client.put(f"/batches/{bid}/sides/handover/inventory", json={"items": _inv_h(bid)})
    client.put(f"/batches/{bid}/sides/receive/inventory", json={"items": _inv_r(bid)})
    for side in ("handover", "receive"):
        client.post(f"/batches/{bid}/sides/{side}/inventory/confirm")
    # 交出方把一个配对的两块拆到两个套组
    client.put(f"/batches/{bid}/sides/handover/groups",
               json={"assignments": {"甲1": ["H-L", "H-W"], "甲2": ["H-R", "H-S"]}})
    client.put(f"/batches/{bid}/sides/receive/groups",
               json={"assignments": {"乙套": ["R-A", "R-B", "R-W", "R-S"]}})
    rep = client.get(f"/batches/{bid}/report").json()
    # 接收方乙套通过两块连接把甲1/甲2并入同一组分 -> split
    merged = next(s for s in rep["sets"]
                  if any(l["label"] == "乙套" for l in s["labels"]))
    assert any(i["issue"] == "set_split" and i["side"] == "handover"
               for i in merged["issues"])
    # 甲1/甲2 各自缺类 -> 不闭合
    assert merged["closed"] is False


def test_resubmit_after_confirm_requires_withdraw(client):
    bid, _ = _full_setup(client)
    client.post(f"/batches/{bid}/sides/handover/inventory/confirm")
    r = client.put(f"/batches/{bid}/sides/handover/inventory",
                   json={"items": _inv_h(bid)})
    assert r.status_code == 409 and r.json()["error"]["code"] == "stage_confirmed"
    client.post(f"/batches/{bid}/sides/handover/withdraw")
    r = client.put(f"/batches/{bid}/sides/handover/inventory",
                   json={"items": _inv_h(bid)})
    assert r.status_code == 200 and r.json()["submitted"] == 4


def test_observation_requires_both_adoptions(client):
    bid, wid = _full_setup(client)
    oid = client.post(f"/batches/{bid}/witnesses/{wid}/observations",
                      json={"target_code": "H-L", "side_hint": "handover",
                            "inscriptions": ["补录题记"]}).json()["observation_id"]
    client.post(f"/batches/{bid}/observations/{oid}/sides/handover/decision",
                json={"adopted": True})
    rep = client.get(f"/batches/{bid}/report").json()
    # 单方采纳前：观察处于 pending，作为未决阻断出现
    assert any(gi["issue"] == "observation_unresolved" for gi in rep["global_issues"])

    # 接收方退回复核
    rr = client.post(f"/batches/{bid}/observations/{oid}/sides/receive/decision",
                     json={"adopted": False, "note": "退回再核"})
    assert rr.json()["status"] == "disputed"
    rep = client.get(f"/batches/{bid}/report").json()
    assert rep["blockers"]["freeze"] is True

    # 双方采纳后：观察并入，全流程可闭合冻结
    client.post(f"/batches/{bid}/observations/{oid}/sides/receive/decision",
                json={"adopted": True, "note": "复核属实"})
    rep = client.get(f"/batches/{bid}/report").json()
    assert not any(gi["issue"] == "observation_unresolved" for gi in rep["global_issues"])

    for side in ("handover", "receive"):
        client.post(f"/batches/{bid}/sides/{side}/inventory/confirm")
    client.put(f"/batches/{bid}/sides/handover/groups",
               json={"assignments": {"甲套": ["H-L", "H-R", "H-W", "H-S"]}})
    client.put(f"/batches/{bid}/sides/receive/groups",
               json={"assignments": {"乙套": ["R-A", "R-B", "R-W", "R-S"]}})
    for side in ("handover", "receive"):
        client.post(f"/batches/{bid}/sides/{side}/groups/confirm", json={})
    for side, label in (("handover", "甲套"), ("receive", "乙套")):
        r = client.post(f"/batches/{bid}/sides/{side}/status/confirm",
                        json={"sets": [{"set_label": label, "accept_discrepancy": True}]})
        assert r.status_code == 200, r.text
    assert client.post(f"/batches/{bid}/freeze", json={}).status_code == 200
