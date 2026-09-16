"""交接核对核心引擎（纯函数，无 IO）。

关键立场（对应需求）：
- 硬冲突：尺寸、色版角色、刻面方向、包纸材质、试印样呈色 等不可两立的物性矛盾；
  命中硬冲突的两方条目**不配对**，进入排除清单。
- 证据不足：多个候选都不被排除时全部保留为候选组，不强行配对。
- 题名 title 只登记、展示，绝不参与同一认定或套组并合。
"""
from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from typing import Any, Optional

# 尺寸容差（毫米）：手工木版量测误差
DIM_TOL_MM = 2.0
GAP_TOL_MM = 2.0

KINDS = ("block", "wrapper", "sample")

# ---------------------------------------------------------------- 归一化

_PUNCT = re.compile(r"[\s，。、；：“”‘’\"'（）()\[\]【】·．.,;:!?！？\-—_/]+")


def norm_text(s: Optional[str]) -> str:
    if not s:
        return ""
    return _PUNCT.sub("", str(s).strip()).lower()


_ROLE_MAP = [
    ("line", ("线版", "墨线版", "线稿", "主版", "线板", "墨版", "线")),
    ("red", ("红版", "朱版", "红板", "朱色版", "红色版")),
    ("yellow", ("黄版", "黄板", "黄色版")),
    ("blue", ("蓝版", "蓝板", "青色版", "蓝色版")),
    ("green", ("绿版", "绿板", "绿色版")),
    ("purple", ("紫版", "紫板", "紫色版")),
    ("black", ("黑版", "黑板", "黑色版")),
    ("gold", ("金版", "金板", "金色版", "套金版")),
    ("color", ("色版", "色板", "套色版")),  # 泛指，不与具体色冲突
]
_ROLE_EXACT = {
    "line": "line", "red": "red", "yellow": "yellow", "blue": "blue",
    "green": "green", "purple": "purple", "black": "black", "gold": "gold",
    "color": "color",
}


def norm_role(raw: Optional[str]) -> Optional[str]:
    if raw is None:
        return None
    t = str(raw).strip()
    low = t.lower()
    if low in _ROLE_EXACT:
        return _ROLE_EXACT[low]
    for canon, names in _ROLE_MAP:
        if t in names:
            return canon
    # 未识别：保留原文标记，不武断归并
    return f"?{norm_text(t)}"


_FACE_MAP = {
    "up": ("正刻", "正向", "正面", "正"),
    "reverse": ("反刻", "反向", "反面", "反"),
    "horizontal": ("横刻", "横向", "横"),
}


def norm_face(raw: Optional[str]) -> Optional[str]:
    if raw is None:
        return None
    t = str(raw).strip()
    low = t.lower()
    if low in _FACE_MAP:
        return low
    for canon, names in _FACE_MAP.items():
        if t in names:
            return canon
    return f"?{norm_text(t)}"


_MATERIAL_MAP = [
    ("xuan", ("宣纸", "宣紙", "宣")),
    ("maobian", ("毛边纸", "毛邊紙", "毛边")),
    ("newsprint", ("报纸", "報紙", "新闻纸")),
    ("cloth", ("布", "布包", "棉布", "绢", "綢", "绸")),
    ("box", ("函套", "木匣", "木盒", "盒", "匣")),
    ("paper", ("纸", "紙")),  # 泛指纸，不与具体纸种冲突
]


def norm_material(raw: Optional[str]) -> Optional[str]:
    if raw is None:
        return None
    t = str(raw).strip()
    for canon, names in _MATERIAL_MAP:
        if t in names:
            return canon
    return f"?{norm_text(t)}"


def norm_color_list(vals: Optional[list[str]]) -> list[str]:
    return sorted({norm_text(v) for v in (vals or []) if v and str(v).strip()})


# ---------------------------------------------------------------- 取数

_SCALAR_FIELDS = {
    "block": ("color_role", "face_dir"),
    "wrapper": ("material",),
    "sample": (),
}
_LIST_FIELDS = {
    "block": ("damage", "inscriptions", "gaps"),
    "wrapper": ("damage", "inscriptions"),
    "sample": ("colors", "clues", "inscriptions"),
}


def _dims_tuple(d: Optional[dict]) -> Optional[tuple[Optional[float], Optional[float]]]:
    if not d:
        return None
    w = d.get("width_mm")
    h = d.get("height_mm")
    if w is None and h is None:
        return None
    return (w, h)


def _merge_observations(
    kind: str, base: dict, obs_payloads: list[dict]
) -> tuple[dict, list[dict]]:
    """把双方采纳的见证观察并入本方声明。

    返回 (生效字段, 矛盾列表)；矛盾指见证与原声明在硬字段上不可两立。
    """
    eff: dict[str, Any] = {
        "title": base.get("title"),
        "note": base.get("note"),
        "dims": _dims_tuple(base.get("dims")),
        "color_role": norm_role(base.get("color_role")) if kind == "block" else None,
        "face_dir": norm_face(base.get("face_dir")) if kind == "block" else None,
        "material": norm_material(base.get("material")) if kind == "wrapper" else None,
        "damage": list(base.get("damage") or []),
        "inscriptions": list(base.get("inscriptions") or []),
        "gaps": [tuple(g) for g in (base.get("gaps") or [])],
        "colors": norm_color_list(base.get("colors")) if kind == "sample" else [],
        "clues": list(base.get("clues") or []) if kind == "sample" else [],
    }
    contradictions: list[dict] = []

    for ob in obs_payloads:
        ob_dims = _dims_tuple(ob.get("dims"))
        if ob_dims is not None:
            if eff["dims"] is None:
                eff["dims"] = ob_dims
            elif not _dims_compatible(eff["dims"], ob_dims):
                contradictions.append(
                    {"field": "dims", "declared": eff["dims"], "observed": ob_dims}
                )
        if kind == "block":
            for field, normalizer in (("color_role", norm_role), ("face_dir", norm_face)):
                val = normalizer(ob.get(field))
                if val is not None:
                    cur = eff[field]
                    if cur is None:
                        eff[field] = val
                    elif _specific(cur) and _specific(val) and cur != val:
                        contradictions.append(
                            {"field": field, "declared": cur, "observed": val}
                        )
        if kind == "wrapper":
            val = norm_material(ob.get("material"))
            if val is not None:
                cur = eff["material"]
                if cur is None:
                    eff["material"] = val
                elif _specific(cur) and _specific(val) and cur != val:
                    contradictions.append(
                        {"field": "material", "declared": cur, "observed": val}
                    )
        for lf in _LIST_FIELDS[kind]:
            if lf == "gaps":
                eff["gaps"] = _union_gaps(eff["gaps"], [tuple(g) for g in ob.get("gaps") or []])
            elif lf == "colors":
                eff["colors"] = sorted(set(eff["colors"]) | set(norm_color_list(ob.get("colors"))))
            else:
                for v in ob.get(lf) or []:
                    if v and v not in eff[lf]:
                        eff[lf].append(v)
    return eff, contradictions


def _specific(v: Optional[str]) -> bool:
    if not v or v.startswith("?"):
        return False
    return v not in ("color", "paper")


# ---------------------------------------------------------------- 比较

def _num_close(a: Optional[float], b: Optional[float], tol: float = DIM_TOL_MM) -> bool:
    if a is None or b is None:
        return True  # 缺测不构成冲突
    return abs(float(a) - float(b)) <= tol


def _dims_compatible(
    d1: Optional[tuple[Optional[float], Optional[float]]],
    d2: Optional[tuple[Optional[float], Optional[float]]],
) -> bool:
    if d1 is None or d2 is None:
        return True
    return _num_close(d1[0], d2[0]) and _num_close(d1[1], d2[1])


def _dims_score(
    d1: Optional[tuple[Optional[float], Optional[float]]],
    d2: Optional[tuple[Optional[float], Optional[float]]],
) -> float:
    if d1 is None or d2 is None:
        return 0.0
    axes = 0
    ok = 0
    for a, b in zip(d1, d2):
        if a is not None and b is not None:
            axes += 1
            if _num_close(a, b):
                ok += 1
    if axes == 0:
        return 0.0
    return 3.0 * ok / axes


def _gap_dist(g1: tuple[float, float], g2: tuple[float, float]) -> float:
    return math.dist(g1, g2)


def _union_gaps(a: list[tuple], b: list[tuple]) -> list[tuple[float, float]]:
    out = [tuple(g) for g in a]
    for g in b:
        if all(_gap_dist(tuple(g), tuple(h)) > GAP_TOL_MM for h in out):
            out.append(tuple(g))
    return out


def _gaps_compare(a: list[tuple], b: list[tuple]) -> tuple[float, list[dict]]:
    """返回 (证据分, 差异)。缺口不齐只算软差异。"""
    if not a or not b:
        return 0.0, []
    matched_b: set[int] = set()
    matched_a: set[int] = set()
    for i, ga in enumerate(a):
        best, bd = None, GAP_TOL_MM
        for j, gb in enumerate(b):
            d = _gap_dist(tuple(ga), tuple(gb))
            if d <= bd:
                best, bd = j, d
        if best is not None:
            matched_a.add(i)
            matched_b.add(best)
    score = 2.0 if (len(matched_a) == len(a) and len(matched_b) == len(b)) else (
        1.0 if matched_a else 0.0
    )
    discr: list[dict] = []
    missing_b = [list(a[i]) for i in range(len(a)) if i not in matched_a]
    missing_a = [list(b[j]) for j in range(len(b)) if j not in matched_b]
    if missing_b:
        discr.append({"field": "gaps", "kind": "gap_only_handover", "points": missing_b})
    if missing_a:
        discr.append({"field": "gaps", "kind": "gap_only_receive", "points": missing_a})
    return score, discr


def _inscriptions_compare(a: list[str], b: list[str]) -> tuple[float, list[dict]]:
    if not a or not b:
        return 0.0, []
    na = {norm_text(x): x for x in a if x}
    nb = {norm_text(x): x for x in b if x}
    common = set(na) & set(nb)
    discr = []
    only_h = [na[k] for k in sorted(set(na) - set(nb))]
    only_r = [nb[k] for k in sorted(set(nb) - set(na))]
    if only_h or only_r:
        discr.append(
            {"field": "inscriptions", "kind": "inscription_diff",
             "only_handover": only_h, "only_receive": only_r}
        )
    return (1.0 if common else 0.0), discr


def _damage_compare(a: list[str], b: list[str]) -> list[dict]:
    na = {norm_text(x) for x in a if x}
    nb = {norm_text(x) for x in b if x}
    discr = []
    if na and not nb:
        discr.append({"field": "damage", "kind": "damage_unreported_receive"})
    elif nb and not na:
        discr.append({"field": "damage", "kind": "damage_unreported_handover"})
    elif na and nb and not (na & nb):
        discr.append({"field": "damage", "kind": "damage_statement_diff"})
    return discr


def compare_items(eff_a: dict, eff_b: dict) -> dict:
    """比较两件（已并入见证的）生效条目。

    返回 {compatible: bool, score: float, hard: [...], discrepancies: [...]}
    """
    hard: list[dict] = []
    discr: list[dict] = []
    score = _dims_score(eff_a["dims"], eff_b["dims"])
    if not _dims_compatible(eff_a["dims"], eff_b["dims"]):
        hard.append({
            "field": "dims", "kind": "dims_mismatch",
            "handover": list(eff_a["dims"]) if eff_a["dims"] else None,
            "receive": list(eff_b["dims"]) if eff_b["dims"] else None,
            "tolerance_mm": DIM_TOL_MM,
        })

    kind = eff_a["kind"]
    if kind == "block":
        ra, rb = eff_a["color_role"], eff_b["color_role"]
        if _specific(ra) and _specific(rb) and ra != rb:
            hard.append({"field": "color_role", "kind": "color_role_conflict",
                         "handover": ra, "receive": rb})
        elif ra == rb and _specific(ra):
            score += 2.0
        fa, fb = eff_a["face_dir"], eff_b["face_dir"]
        if _specific(fa) and _specific(fb) and fa != fb:
            hard.append({"field": "face_dir", "kind": "face_dir_conflict",
                         "handover": fa, "receive": fb})
        elif fa == fb and _specific(fa):
            score += 1.0
        gs, gd = _gaps_compare(eff_a["gaps"], eff_b["gaps"])
        score += gs
        discr += gd
    elif kind == "wrapper":
        ma, mb = eff_a["material"], eff_b["material"]
        if _specific(ma) and _specific(mb) and ma != mb:
            hard.append({"field": "material", "kind": "material_conflict",
                         "handover": ma, "receive": mb})
        elif ma == mb and _specific(ma):
            score += 2.0

    elif kind == "sample":
        ca, cb = set(eff_a["colors"]), set(eff_b["colors"])
        if ca and cb and not (ca & cb):
            hard.append({"field": "colors", "kind": "colors_disjoint",
                         "handover": sorted(ca), "receive": sorted(cb)})
        elif ca & cb:
            score += 2.0

    ins, idd = _inscriptions_compare(eff_a["inscriptions"], eff_b["inscriptions"])
    score += ins
    discr += idd
    discr += _damage_compare(eff_a["damage"], eff_b["damage"])
    return {"compatible": not hard, "score": round(score, 3),
            "hard": hard, "discrepancies": discr}


# ---------------------------------------------------------------- 配对消解

class _UF:
    def __init__(self) -> None:
        self.p: dict[Any, Any] = {}

    def find(self, x: Any) -> Any:
        self.p.setdefault(x, x)
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a: Any, b: Any) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[rb] = ra


def _force_pairs(nodes_l: list[str], nodes_r: list[str],
                 edges: dict[tuple[str, str], dict]) -> tuple[set, dict, set]:
    """互单消解：互为唯一最高候选的对强制配对，迭代至稳定。

    返回 (已锁定对, 剩余边, 已消耗节点)。零分边不算"证据支持"，永不强制。
    """
    locked: set[tuple[str, str]] = set()
    used: set[str] = set()
    remaining = dict(edges)
    while True:
        progress = False
        best_l: dict[str, list[tuple[str, float]]] = defaultdict(list)
        best_r: dict[str, list[tuple[str, float]]] = defaultdict(list)
        for (l, r), e in remaining.items():
            if e["score"] > 0:
                best_l[l].append((r, e["score"]))
                best_r[r].append((l, e["score"]))

        def unique_top(cands: list[tuple[str, float]]) -> Optional[tuple[str, float]]:
            if not cands:
                return None
            mx = max(s for _, s in cands)
            top = [c for c in cands if c[1] == mx]
            return top[0] if len(top) == 1 else None

        for l in nodes_l:
            if l in used:
                continue
            tr = unique_top(best_l.get(l, []))
            if tr is None:
                continue
            r, s_l = tr
            if r in used:
                continue
            tl = unique_top(best_r.get(r, []))
            if tl is not None and tl[0] == l:
                locked.add((l, r))
                used.add(l)
                used.add(r)
                progress = True
        if not progress:
            break
        remaining = {(l, r): e for (l, r), e in remaining.items()
                     if l not in used and r not in used}
    return locked, remaining, used


def build_report(
    items_h: list[dict],
    items_r: list[dict],
    sets_h: dict[str, list[str]],
    sets_r: dict[str, list[str]],
    observations: list[dict],
) -> dict:
    """生成整批复核报告。

    items_*: [{item_id, code, payload, payload_hash}]
    observations: [{id, witness_name, target_kind, targets:[(side,key)],
                     payload, payload_hash, status, decisions:{side:(adopted,note)}}]
    """
    by_id_h = {it["item_id"]: it for it in items_h}
    by_id_r = {it["item_id"]: it for it in items_r}

    # 见证观察按目标归集
    attached: dict[tuple[str, str], list[dict]] = defaultdict(list)
    obs_blockers: list[dict] = []
    for ob in observations:
        if ob["status"] == "incorporated" and ob["target_kind"] == "item":
            for side, key in ob["targets"]:
                attached[(side, key)].append(ob["payload"])
        if ob["status"] in ("pending", "disputed"):
            obs_blockers.append({
                "observation_id": ob["id"],
                "witness": ob["witness_name"],
                "status": ob["status"],
                "target_kind": ob["target_kind"],
                "targets": [{"side": s, "key": k} for s, k in ob["targets"]],
                "note": ob["payload"].get("note"),
            })

    # 生效条目 + 与见证的矛盾
    def effective(side: str, it: dict) -> tuple[dict, list[dict]]:
        eff, contra = _merge_observations(
            it["payload"]["kind"], it["payload"], attached.get((side, it["item_id"]), [])
        )
        eff["kind"] = it["payload"]["kind"]
        return eff, contra

    eff_h = {it["item_id"]: effective("handover", it) for it in items_h}
    eff_r = {it["item_id"]: effective("receive", it) for it in items_r}

    witness_contradictions = []
    for iid, (_, contra) in eff_h.items():
        for c in contra:
            witness_contradictions.append({"side": "handover", "item_id": iid,
                                           "code": by_id_h[iid]["code"], **c})
    for iid, (_, contra) in eff_r.items():
        for c in contra:
            witness_contradictions.append({"side": "receive", "item_id": iid,
                                           "code": by_id_r[iid]["code"], **c})

    # 逐类构图
    locked_pairs: list[dict] = []
    candidate_groups: list[dict] = []
    unmatched: list[dict] = []

    for kind in KINDS:
        l_nodes = [it["item_id"] for it in items_h if it["payload"]["kind"] == kind]
        r_nodes = [it["item_id"] for it in items_r if it["payload"]["kind"] == kind]
        compat_edges: dict[tuple[str, str], dict] = {}
        incompat: dict[str, list[dict]] = defaultdict(list)
        for l in l_nodes:
            for r in r_nodes:
                cmp_ = compare_items(eff_h[l][0], eff_r[r][0])
                if cmp_["compatible"]:
                    compat_edges[(l, r)] = cmp_
                else:
                    for h in cmp_["hard"]:
                        incompat[l].append({"other_code": by_id_r[r]["code"], **h})
                        incompat[r].append({"other_code": by_id_h[l]["code"], **h})

        locked, remaining, used = _force_pairs(l_nodes, r_nodes, compat_edges)
        for l, r in sorted(locked):
            cmp_ = compat_edges[(l, r)]
            locked_pairs.append({
                "kind": kind,
                "handover": _ref(by_id_h[l]),
                "receive": _ref(by_id_r[r]),
                "score": cmp_["score"],
                "discrepancies": cmp_["discrepancies"],
            })

        # 剩余连通分量 = 候选组（证据不足，全部保留）
        uf = _UF()
        for (l, r) in remaining:
            uf.find(l)
            uf.find(r)
            uf.union(l, r)
        comps: dict[Any, list[str]] = defaultdict(list)
        for n in list(uf.p):
            comps[uf.find(n)].append(n)
        for root, members in comps.items():
            ls = [m for m in members if m in by_id_h]
            rs = [m for m in members if m in by_id_r]
            edge_list = [
                {"handover": _ref(by_id_h[l])["code"],
                 "receive": _ref(by_id_r[r])["code"],
                 "score": compat_edges[(l, r)]["score"]}
                for (l, r) in remaining if l in set(ls) and r in set(rs)
            ]
            candidate_groups.append({
                "kind": kind,
                "handover": [_ref(by_id_h[m]) for m in sorted(ls)],
                "receive": [_ref(by_id_r[m]) for m in sorted(rs)],
                "edges": sorted(edge_list, key=lambda e: (-e["score"], e["handover"], e["receive"])),
                "reason": "ambiguous_candidates",
            })

        # 孤立节点 = 未能配对，给出最小冲突线索
        paired = set()
        for l, r in locked:
            paired.add(l)
            paired.add(r)
        for n in comps:
            paired.update(comps[n])
        for n in sorted(set(l_nodes) | set(r_nodes)):
            if n in paired:
                continue
            side = "handover" if n in by_id_h else "receive"
            info = by_id_h[n] if side == "handover" else by_id_r[n]
            reasons = _minimal_reasons(incompat.get(n, []))
            unmatched.append({
                "kind": kind, "side": side, **_ref(info),
                "reason": "no_compatible_partner" if reasons else "no_evidence_overlap",
                "conflicts": reasons,
            })

    # ---------------- 套组：以锁定对为边连接两方套组节点
    item_set_h = {iid: lbl for lbl, ids in sets_h.items() for iid in ids}
    item_set_r = {iid: lbl for lbl, ids in sets_r.items() for iid in ids}
    uf2 = _UF()
    node_meta: dict[tuple[str, str], dict] = {}
    for lbl, ids in sets_h.items():
        node_meta[("handover", lbl)] = {"side": "handover", "label": lbl,
                                        "members": sorted(ids)}
    for lbl, ids in sets_r.items():
        node_meta[("receive", lbl)] = {"side": "receive", "label": lbl,
                                       "members": sorted(ids)}

    pair_join_issues: list[dict] = []
    for p in locked_pairs:
        lh = p["handover"]["item_id"]
        rr = p["receive"]["item_id"]
        sh, sr = item_set_h.get(lh), item_set_r.get(rr)
        if sh is None or sr is None:
            pair_join_issues.append({
                "kind": p["kind"],
                "handover_code": p["handover"]["code"],
                "receive_code": p["receive"]["code"],
                "issue": "pair_member_unassigned",
                "unassigned_side": "handover" if sh is None else "receive",
            })
            if sh is not None:
                uf2.find(("handover", sh))
            if sr is not None:
                uf2.find(("receive", sr))
        else:
            uf2.union(("handover", sh), ("receive", sr))
    for node in node_meta:
        uf2.find(node)

    # 候选组触碰了哪些套组
    ambiguity_by_set: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for g in candidate_groups:
        touched: set[tuple[str, str]] = set()
        for ref in g["handover"]:
            lbl = item_set_h.get(ref["item_id"])
            touched.add(("handover", lbl if lbl is not None else "∅未入组"))
        for ref in g["receive"]:
            lbl = item_set_r.get(ref["item_id"])
            touched.add(("receive", lbl if lbl is not None else "∅未入组"))
        clue = {"kind": g["kind"],
                "handover_codes": [r["code"] for r in g["handover"]],
                "receive_codes": [r["code"] for r in g["receive"]]}
        for t in touched:
            ambiguity_by_set[t].append(clue)

    # 见证阻断按条目归属到套组
    obs_by_set: dict[tuple[str, str], list[dict]] = defaultdict(list)
    obs_global: list[dict] = []
    for b in obs_blockers:
        placed = False
        for t in b["targets"]:
            side, key = t["side"], t["key"]
            if b["target_kind"] == "item":
                lbl = item_set_h.get(key) if side == "handover" else item_set_r.get(key)
                if lbl is not None:
                    obs_by_set[(side, lbl)].append(b)
                    placed = True
            else:
                obs_by_set[(side, key)].append(b)
                placed = True
        if not placed:
            obs_global.append(b)

    contra_by_set: dict[tuple[str, str], list[dict]] = defaultdict(list)
    contra_global: list[dict] = []
    for c in witness_contradictions:
        iid = c["item_id"]
        lbl = item_set_h.get(iid) if c["side"] == "handover" else item_set_r.get(iid)
        if lbl is not None:
            contra_by_set[(c["side"], lbl)].append(c)
        else:
            contra_global.append(c)

    groups_nodes: dict[Any, list[tuple[str, str]]] = defaultdict(list)
    for node in uf2.p:
        groups_nodes[uf2.find(node)].append(node)

    set_reports: list[dict] = []
    for root in sorted(groups_nodes, key=lambda x: sorted(str(t) for t in groups_nodes[x])):
        nodes = sorted(groups_nodes[root])
        sides_present = {s for s, _ in nodes}
        labels = [node_meta[n] for n in nodes]
        issues: list[dict] = []

        # 空套组
        for n in nodes:
            if not node_meta[n]["members"]:
                issues.append({"issue": "set_empty", "side": n[0], "label": n[1]})

        # 一方独有
        if sides_present != {"handover", "receive"}:
            only = next(iter(sides_present))
            issues.append({"issue": "set_one_sided", "side": only})

        # 分裂：一方两个套组并入同一组分
        c = Counter(s for s, _ in nodes)
        for s, n in c.items():
            if n > 1:
                issues.append({"issue": "set_split", "side": s, "count": n})

        # 成员统计
        kind_count: Counter = Counter()
        pair_codes: list[dict] = []
        discrepancy_flat: list[dict] = []
        node_pair_ids: set[str] = set()
        for m in [i for n in nodes for i in node_meta[n]["members"]]:
            node_pair_ids.add(m)
        for p in locked_pairs:
            if p["handover"]["item_id"] in node_pair_ids or p["receive"]["item_id"] in node_pair_ids:
                kind_count[p["kind"]] += 1
                pair_codes.append({"kind": p["kind"],
                                   "handover": p["handover"]["code"],
                                   "receive": p["receive"]["code"]})
                for d in p["discrepancies"]:
                    discrepancy_flat.append({
                        "kind": p["kind"],
                        "handover": p["handover"]["code"],
                        "receive": p["receive"]["code"], **d})

        missing = [k for k in KINDS if kind_count[k] == 0]
        if missing:
            issues.append({"issue": "set_incomplete_kinds", "missing": missing})

        # 未入组的配对成员
        unassigned_pairs = [
            x for x in pair_join_issues
            if (x["unassigned_side"] == "handover"
                and item_set_r.get(_pair_iid(locked_pairs, x, "receive")) in
                [n[1] for n in nodes if n[0] == "receive"])
            or (x["unassigned_side"] == "receive"
                and item_set_h.get(_pair_iid(locked_pairs, x, "handover")) in
                [n[1] for n in nodes if n[0] == "handover"])
        ]
        # 上式可能漏：直接按对侧标签归属
        unassigned_pairs = []
        for x in pair_join_issues:
            side_ok = (
                (x["unassigned_side"] == "receive"
                 and item_set_h.get(_pair_iid(locked_pairs, x, "handover"))
                 in [n[1] for n in nodes if n[0] == "handover"])
                or (x["unassigned_side"] == "handover"
                    and item_set_r.get(_pair_iid(locked_pairs, x, "receive"))
                    in [n[1] for n in nodes if n[0] == "receive"])
            )
            if side_ok:
                unassigned_pairs.append(x)
                issues.append({"issue": "pair_member_unassigned", **x})

        # 候选组（证据不足）
        amb: list[dict] = []
        for n in nodes:
            amb.extend(ambiguity_by_set.get(n, []))
        # 去重
        amb = _dedup(amb)
        if amb:
            issues.append({"issue": "ambiguous_membership", "groups": amb})

        # 见证相关阻断
        set_obs: list[dict] = []
        for n in nodes:
            set_obs.extend(obs_by_set.get(n, []))
        set_obs = _dedup(set_obs)
        if set_obs:
            issues.append({"issue": "observation_unresolved", "items": set_obs})

        set_contra: list[dict] = []
        for n in nodes:
            set_contra.extend(contra_by_set.get(n, []))
        set_contra = _dedup(set_contra)
        if set_contra:
            issues.append({"issue": "witness_contradiction", "items": set_contra})

        blocking_issue_kinds = {
            "set_empty", "set_one_sided", "set_split", "set_incomplete_kinds",
            "pair_member_unassigned", "ambiguous_membership",
            "observation_unresolved", "witness_contradiction",
        }
        closed = not any(i["issue"] in blocking_issue_kinds for i in issues)

        set_reports.append({
            "set_id": "|".join(f"{s}:{l}" for s, l in nodes),
            "labels": [{"side": s, "label": l} for s, l in nodes],
            "closed": closed,
            "member_counts": {k: kind_count[k] for k in KINDS},
            "pairs": pair_codes,
            "issues": issues,
            "discrepancies": discrepancy_flat,
        })

    # 未入组条目构成的全局问题
    unassigned_items = []
    for it in items_h:
        if it["item_id"] not in item_set_h:
            unassigned_items.append({"side": "handover", **_ref(it)})
    for it in items_r:
        if it["item_id"] not in item_set_r:
            unassigned_items.append({"side": "receive", **_ref(it)})

    global_issues = []
    if unassigned_items:
        global_issues.append({"issue": "items_not_in_set", "items": unassigned_items})
    if obs_global:
        global_issues.append({"issue": "observation_unresolved", "items": obs_global})
    if contra_global:
        global_issues.append({"issue": "witness_contradiction", "items": contra_global})

    handover_blocked = any(
        u["side"] == "handover" and u["reason"] == "no_compatible_partner"
        for u in unmatched
    )
    receive_blocked = any(
        u["side"] == "receive" and u["reason"] == "no_compatible_partner"
        for u in unmatched
    )

    return {
        "locked_pairs": sorted(locked_pairs,
                               key=lambda p: (p["kind"], p["handover"]["code"],
                                              p["receive"]["code"])),
        "candidate_groups": sorted(candidate_groups,
                                   key=lambda g: (g["kind"],
                                                  [x["code"] for x in g["handover"]],
                                                  [x["code"] for x in g["receive"]])),
        "unmatched": unmatched,
        "sets": sorted(set_reports, key=lambda s: s["set_id"]),
        "global_issues": global_issues,
        "blockers": {
            "inventory_handover": handover_blocked,
            "inventory_receive": receive_blocked,
            "freeze": bool(
                unmatched or candidate_groups or unassigned_items
                or obs_blockers or witness_contradictions
                or any(not s["closed"] for s in set_reports)
            ),
        },
    }


def _ref(it: dict) -> dict:
    return {"item_id": it["item_id"], "code": it["code"],
            "kind": it["payload"]["kind"], "title": it["payload"].get("title")}


def _pair_iid(locked_pairs: list[dict], x: dict, side: str) -> Optional[str]:
    for p in locked_pairs:
        if p["handover"]["code"] == x["handover_code"] and \
                p["receive"]["code"] == x["receive_code"]:
            return p[side]["item_id"]
    return None


def _minimal_reasons(reasons: list[dict]) -> list[dict]:
    """最小冲突线索：同 (field, kind) 合并对侧条目码，仅留字段取值摘要。"""
    merged: dict[tuple, dict] = {}
    for src in reasons:
        k = (src["field"], src["kind"])
        if k not in merged:
            base = {x: src[x] for x in src
                    if x not in ("other_code",)}
            base["other_codes"] = []
            merged[k] = base
        merged[k]["other_codes"].append(src["other_code"])
    out = list(merged.values())
    for v in out:
        v["other_codes"] = sorted(set(v["other_codes"]))
    return sorted(out, key=lambda x: (x["field"], x["kind"]))


def _dedup(items: list[dict]) -> list:
    seen = set()
    out = []
    for x in items:
        k = repr(x)
        if k not in seen:
            seen.add(k)
            out.append(x)
    return out


