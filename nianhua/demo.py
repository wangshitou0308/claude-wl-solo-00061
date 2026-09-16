#!/usr/bin/env python3
"""命令行示例：不依赖 HTTP，直接走服务层演示一次完整交接。

    .venv/bin/python demo.py /tmp/demo.db
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(__file__))

from app import service  # noqa: E402
from app.db import init_db  # noqa: E402


def pp(title, obj):
    print(f"\n=== {title} ===")
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def main():
    db = sys.argv[1] if len(sys.argv) > 1 else os.path.join(tempfile.gettempdir(), "demo.db")
    if os.path.exists(db):
        os.unlink(db)
    init_db(db)

    bid = service.create_batch({"title": "秦琼尉迟恭门神一套"}, db)["batch_id"]
    service.register_party(bid, {"side": "handover", "person": {"name": "祖父"}}, db)
    service.register_party(bid, {"side": "receive", "person": {"name": "长孙"}}, db)
    wid = service.register_witness(bid, {"person": {"name": "二叔"}, "relation": "家属"}, db)["witness_id"]

    H = [
        {"kind": "block", "code": "H-线版", "title": "秦琼（题名仅供登记）",
         "dims": {"width_mm": 300, "height_mm": 450},
         "color_role": "线版", "face_dir": "反刻",
         "gaps": [[12, 13], [200, 18]], "damage": ["左上角虫蛀"],
         "inscriptions": ["光绪年制"]},
        {"kind": "block", "code": "H-红版",
         "dims": {"width_mm": 301, "height_mm": 451},
         "color_role": "红版", "face_dir": "反刻"},
        {"kind": "wrapper", "code": "H-包纸", "material": "宣纸",
         "inscriptions": ["门神一套"]},
        {"kind": "sample", "code": "H-样张", "colors": ["红", "黑"],
         "clues": ["与线版缺口对应"]},
    ]
    R = [
        {"kind": "block", "code": "R-1", "title": "题名完全不同也不影响",
         "dims": {"width_mm": 300.5, "height_mm": 449.5},
         "color_role": "墨线版", "face_dir": "反向",
         "gaps": [[12.5, 12.5]], "inscriptions": ["光绪年制"]},
        {"kind": "block", "code": "R-2",
         "dims": {"width_mm": 301, "height_mm": 451},
         "color_role": "朱版", "face_dir": "反刻"},
        {"kind": "wrapper", "code": "R-包", "material": "宣紙"},
        {"kind": "sample", "code": "R-样", "colors": ["黑"]},
    ]
    service.submit_inventory(bid, "handover", {"items": H}, db)
    service.submit_inventory(bid, "receive", {"items": R}, db)

    rep = service.build_report(bid, db)
    print("锁定配对：")
    for p in rep["locked_pairs"]:
        print(f"  {p['kind']:8s} {p['handover']['code']}  ↔  {p['receive']['code']}  (证据分 {p['score']})")
    assert not rep["unmatched"] and not rep["candidate_groups"]

    print("\n（如需观察硬冲突，可把任一尺寸改到容差 2mm 之外）")

    # 正常三阶段
    service.confirm_inventory(bid, "handover", db)
    service.confirm_inventory(bid, "receive", db)
    service.assign_groups(bid, "handover", {"assignments": {"甲": [
        "H-线版", "H-红版", "H-包纸", "H-样张"]}}, db)
    service.assign_groups(bid, "receive", {"assignments": {"乙": [
        "R-1", "R-2", "R-包", "R-样"]}}, db)
    service.confirm_grouping(bid, "handover", {}, db)
    service.confirm_grouping(bid, "receive", {}, db)
    for side, label in (("handover", "甲"), ("receive", "乙")):
        service.confirm_status(bid, side, {"sets": [
            {"set_label": label, "accept_discrepancy": True}]}, db)

    fz = service.freeze(bid, {"note": "双方在场，责任移交"}, db)
    pp("冻结完成（保管责任 handover -> receive）", fz)
    rec = service.get_freeze_record(bid, db)
    print("快照中责任：", rec["snapshot"]["responsibility"])
    print("分歧处置留痕条数：", len(rec["snapshot"]["dispute_resolution"]))


if __name__ == "__main__":
    main()
