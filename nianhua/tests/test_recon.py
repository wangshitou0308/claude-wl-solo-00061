"""recon 纯函数单元测试：归一化、硬冲突、证据不足候选、互单消解。"""
from app import recon


def _it(kind, code, **kw):
    base = {"kind": kind, "code": code}
    base.update(kw)
    return {"item_id": code, "code": code, "payload": base, "payload_hash": "h-" + code}


def test_role_and_face_synonym_normalization():
    assert recon.norm_role("线版") == recon.norm_role("墨线版") == recon.norm_role("line")
    assert recon.norm_role("红版") == recon.norm_role("朱色版") == "red"
    assert recon.norm_face("反刻") == recon.norm_face("反向") == "reverse"
    # 未知词保留原文，不武断归并
    assert recon.norm_role("矾红偏橘的一种").startswith("?")
    # 泛指色版/纸不与具体种类冲突
    assert recon._specific("color") is False
    assert recon._specific("paper") is False
    assert recon._specific("red") is True
    assert recon._specific("reverse") is True


def test_dims_hard_conflict_within_tolerance_is_soft():
    a = {"kind": "block", "dims": (300.0, 450.0), "color_role": "line",
         "face_dir": "reverse", "damage": [], "inscriptions": [], "gaps": [],
         "colors": [], "clues": [], "title": "x"}
    b = dict(a, dims=(301.5, 449.0))
    assert recon.compare_items(a, b)["compatible"] is True
    c = dict(a, dims=(400.0, 450.0))
    r = recon.compare_items(a, c)
    assert r["compatible"] is False
    assert r["hard"][0]["field"] == "dims"


def test_missing_dims_never_conflict():
    a = {"kind": "block", "dims": None, "color_role": "line", "face_dir": None,
         "damage": [], "inscriptions": [], "gaps": [], "colors": [], "clues": []}
    b = dict(a, dims=(400.0, 400.0))
    r = recon.compare_items(a, b)
    # 尺寸缺测不冲突；角色相同（具体值）给证据分
    assert not r["hard"] and r["score"] == 2.0


def test_ambiguous_two_to_one_all_kept():
    h1 = _it("block", "H1", dims={"width_mm": 200, "height_mm": 300},
             color_role="红版", face_dir="反刻", damage=[], inscriptions=[], gaps=[])
    h2 = _it("block", "H2", dims={"width_mm": 200, "height_mm": 300},
             color_role="红版", face_dir="反刻", damage=[], inscriptions=[], gaps=[])
    r1 = _it("block", "R1", dims={"width_mm": 200, "height_mm": 300},
             color_role="朱版", face_dir="反刻", damage=[], inscriptions=[], gaps=[])
    rep = recon.build_report([h1, h2], [r1], {}, {}, [])
    assert len(rep["candidate_groups"]) == 1
    g = rep["candidate_groups"][0]
    assert {x["code"] for x in g["handover"]} == {"H1", "H2"}
    assert [x["code"] for x in g["receive"]] == ["R1"]
    assert rep["locked_pairs"] == []


def test_mutual_unique_forces_pair():
    common = dict(dims={"width_mm": 200, "height_mm": 300}, face_dir="反刻",
                  damage=[], inscriptions=[], gaps=[])
    h1 = _it("block", "H1", color_role="红版", **common)
    h2 = _it("block", "H2", color_role="蓝版", **common)
    r1 = _it("block", "R1", color_role="朱版", **common)
    r2 = _it("block", "R2", color_role="青色版", **common)
    rep = recon.build_report([h1, h2], [r1, r2], {}, {}, [])
    pairs = {(p["handover"]["code"], p["receive"]["code"]) for p in rep["locked_pairs"]}
    assert pairs == {("H1", "R1"), ("H2", "R2")}
    assert rep["candidate_groups"] == []


def test_title_is_not_evidence():
    h = _it("block", "H", title="门神", dims={"width_mm": 100, "height_mm": 100},
            color_role="红版", face_dir="反刻", damage=[], inscriptions=[], gaps=[])
    r = _it("block", "R", title="门神", dims={"width_mm": 800, "height_mm": 800},
            color_role="蓝版", face_dir="正刻", damage=[], inscriptions=[], gaps=[])
    rep = recon.build_report([h], [r], {}, {}, [])
    assert {u["code"] for u in rep["unmatched"]} == {"H", "R"}
    fields = {c["field"] for u in rep["unmatched"] for c in u["conflicts"]}
    assert {"dims", "color_role", "face_dir"} <= fields


def test_sample_colors_disjoint_is_hard():
    h = _it("sample", "HS", colors=["红", "黄"], clues=[], inscriptions=[])
    r = _it("sample", "RS", colors=["蓝"], clues=[], inscriptions=[])
    rep = recon.build_report([h], [r], {}, {}, [])
    assert rep["unmatched"] and rep["unmatched"][0]["reason"] == "no_compatible_partner"


def test_gap_union_matching_and_discrepancy():
    h = {"kind": "block", "dims": None, "color_role": "line", "face_dir": "reverse",
         "damage": [], "inscriptions": [], "gaps": [(10, 10), (100, 50)],
         "colors": [], "clues": []}
    r = dict(h, gaps=[(10.5, 10.2)])
    out = recon.compare_items(h, r)
    assert out["compatible"]
    assert any(d["kind"] == "gap_only_handover" for d in out["discrepancies"])


def test_set_closure_requires_all_three_kinds():
    hb = _it("block", "HB", dims={"width_mm": 200, "height_mm": 300},
             color_role="线版", face_dir="反刻", damage=[], inscriptions=[], gaps=[])
    hw = _it("wrapper", "HW", material="宣纸", damage=[], inscriptions=[])
    hs = _it("sample", "HS", colors=["黑"], clues=[], inscriptions=[])
    rb = _it("block", "RB", dims={"width_mm": 200, "height_mm": 300},
             color_role="墨线版", face_dir="反刻", damage=[], inscriptions=[], gaps=[])
    rw = _it("wrapper", "RW", material="宣紙", damage=[], inscriptions=[])
    rs = _it("sample", "RS", colors=["黑"], clues=[], inscriptions=[])
    sets_h = {"S": ["HB", "HW", "HS"]}
    sets_r = {"T": ["RB", "RW", "RS"]}
    full = recon.build_report([hb, hw, hs], [rb, rw, rs], sets_h, sets_r, [])
    (s,) = full["sets"]
    assert s["closed"] and s["member_counts"] == {"block": 1, "wrapper": 1, "sample": 1}

    # 缺样张 -> 不闭合
    no_sample = recon.build_report([hb, hw], [rb, rw],
                                   {"S": ["HB", "HW"]}, {"T": ["RB", "RW"]}, [])
    (s2,) = no_sample["sets"]
    assert not s2["closed"]
    assert any(i["issue"] == "set_incomplete_kinds" and i["missing"] == ["sample"]
               for i in s2["issues"])
