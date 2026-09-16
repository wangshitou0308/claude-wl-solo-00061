"""领域服务：编排 SQLite 与 recon 引擎，承载交接状态机。

共同状态（停在上一共同状态）由双方各自的确认行实时推导：
  清点共同  = 双方都有 inventory 确认
  套组共同  = 清点共同 且 双方都有 grouping 确认
  状态共同  = 套组共同 且 每个套组双方都有 status 确认
任何一方撤回或资料更正使其依据失效，共同状态自动回退，旧结论不再被引用。
"""
from __future__ import annotations

import json
from collections import defaultdict
from typing import Any, Optional

from . import recon
from .db import session
from .hashing import canon, new_id, sha_of, utcnow

STAGES = ("inventory", "grouping", "status")


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, details: Any = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details


# ---------------------------------------------------------------- 基础

def _event(conn, batch_id: str, actor: str, kind: str, detail: dict) -> None:
    conn.execute(
        "INSERT INTO event(batch_id, at, actor, kind, detail) VALUES (?,?,?,?,?)",
        (batch_id, utcnow(), actor, kind, canon(detail)),
    )


def _get_batch(conn, batch_id: str) -> dict:
    row = conn.execute("SELECT * FROM batch WHERE id=?", (batch_id,)).fetchone()
    if not row:
        raise ApiError(404, "batch_not_found", f"批次 {batch_id} 不存在")
    return dict(row)


def _require_open(conn, batch_id: str) -> dict:
    b = _get_batch(conn, batch_id)
    if b["frozen_at"]:
        raise ApiError(409, "batch_frozen", "批次已冻结，不可再变更",
                       {"frozen_at": b["frozen_at"]})
    return b


def _party(conn, batch_id: str, side: str) -> Optional[dict]:
    r = conn.execute("SELECT * FROM party WHERE batch_id=? AND side=?",
                     (batch_id, side)).fetchone()
    return dict(r) if r else None


def _require_party(conn, batch_id: str, side: str) -> dict:
    p = _party(conn, batch_id, side)
    if not p:
        raise ApiError(409, "party_missing", f"{'交出方' if side == 'handover' else '接收方'}尚未登记")
    return p


def _confirmed(conn, batch_id: str, side: str, stage: str, scope: str = "*") -> Optional[dict]:
    r = conn.execute(
        "SELECT * FROM confirmation WHERE batch_id=? AND side=? AND stage=? AND scope=?",
        (batch_id, side, stage, scope),
    ).fetchone()
    return dict(r) if r else None


def _other(side: str) -> str:
    return "receive" if side == "handover" else "handover"


def _counterpart_labels(report: dict, side: str, labels: list[str]) -> list[str]:
    """经报告中套组组分（锁定对并合），求本方标签对应的对方套组标签。"""
    out: set[str] = set()
    wanted = set(labels)
    for s in report.get("sets", []):
        own = [l["label"] for l in s["labels"] if l["side"] == side]
        opp = [l["label"] for l in s["labels"] if l["side"] == _other(side)]
        if any(l in wanted for l in own):
            out.update(opp)
    return sorted(out)


# ---------------------------------------------------------------- 批次/人员

def create_batch(data: dict, db_path: Optional[str] = None) -> dict:
    bid = new_id()
    with session(db_path) as conn:
        conn.execute(
            "INSERT INTO batch(id,title,note,created_at) VALUES (?,?,?,?)",
            (bid, data.get("title"), data.get("note"), utcnow()),
        )
        _event(conn, bid, "system", "batch_created",
               {"title": data.get("title"), "note": data.get("note")})
    return {"batch_id": bid}


def register_party(batch_id: str, data: dict, db_path: Optional[str] = None) -> dict:
    side = data["side"]
    p = data["person"]
    with session(db_path) as conn:
        _require_open(conn, batch_id)
        existing = _party(conn, batch_id, side)
        if existing:
            raise ApiError(409, "party_exists",
                           f"{'交出方' if side == 'handover' else '接收方'}已登记",
                           {"existing": existing["name"]})
        conn.execute(
            "INSERT INTO party(batch_id,side,name,contact,created_at) VALUES (?,?,?,?,?)",
            (batch_id, side, p["name"], p.get("contact"), utcnow()),
        )
        _event(conn, batch_id, f"party:{side}", "party_registered",
               {"side": side, "name": p["name"]})
    return {"batch_id": batch_id, "side": side, "name": p["name"]}


def register_witness(batch_id: str, data: dict, db_path: Optional[str] = None) -> dict:
    p = data["person"]
    wid = new_id()
    with session(db_path) as conn:
        _require_open(conn, batch_id)
        conn.execute(
            "INSERT INTO witness(id,batch_id,name,contact,relation,created_at) "
            "VALUES (?,?,?,?,?,?)",
            (wid, batch_id, p["name"], p.get("contact"), data.get("relation"), utcnow()),
        )
        _event(conn, batch_id, f"witness:{wid}", "witness_registered",
               {"witness_id": wid, "name": p["name"], "relation": data.get("relation")})
    return {"witness_id": wid, "name": p["name"]}


# ---------------------------------------------------------------- 条目读模型

def _active_items(conn, batch_id: str, side: str) -> list[dict]:
    rows = conn.execute(
        """SELECT i.id AS item_id, i.code, i.payload, i.payload_hash, i.version
             FROM inventory inv JOIN item i ON i.id = inv.item_id
            WHERE inv.batch_id=? AND inv.side=?
            ORDER BY inv.seq""",
        (batch_id, side),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["payload"] = json.loads(d["payload"])
        out.append(d)
    return out


def _set_membership(conn, batch_id: str, side: str) -> dict[str, list[str]]:
    rows = conn.execute(
        "SELECT set_label, item_id FROM set_member WHERE batch_id=? AND side=?",
        (batch_id, side),
    ).fetchall()
    m: dict[str, list[str]] = defaultdict(list)
    for r in rows:
        m[r["set_label"]].append(r["item_id"])
    return dict(m)


def _active_item_by_code(conn, batch_id: str, side: str, code: str) -> Optional[dict]:
    for it in _active_items(conn, batch_id, side):
        if it["code"] == code:
            return it
    return None


def _drop_confirmations(conn, batch_id: str, side: str, stages_scopes: list[tuple[str, str]],
                        reason: str, actor: str) -> list[tuple[str, str]]:
    dropped = []
    for stage, scope in stages_scopes:
        cur = _confirmed(conn, batch_id, side, stage, scope)
        if cur:
            conn.execute(
                "DELETE FROM confirmation WHERE batch_id=? AND side=? AND stage=? AND scope=?",
                (batch_id, side, stage, scope),
            )
            dropped.append((stage, scope))
            _event(conn, batch_id, actor, "confirmation_invalidated",
                   {"side": side, "stage": stage, "scope": scope, "reason": reason})
    return dropped


# ---------------------------------------------------------------- 清点

def _replace_inventory(conn, batch_id: str, side: str, items: list[dict], actor: str) -> dict:
    """整单替换：写新版本 item 行、重建 inventory 头；保留旧版本留痕。"""
    old = _active_items(conn, batch_id, side)
    old_by_code = {it["code"]: it for it in old}

    # 同方条目码不可重复
    codes = [it["code"] for it in items]
    dup = {c for c in codes if codes.count(c) > 1}
    if dup:
        raise ApiError(422, "duplicate_code", "本方清单内条目码重复", {"codes": sorted(dup)})

    new_ids_by_code: dict[str, str] = {}
    for it in items:
        payload = it["payload"] if "payload" in it else it
        h = sha_of(payload)
        prev = old_by_code.get(it["code"])
        if prev and prev["payload_hash"] == h:
            new_ids_by_code[it["code"]] = prev["item_id"]
            continue
        version = (prev["version"] + 1) if prev else 1
        iid = new_id()
        conn.execute(
            "INSERT INTO item(id,batch_id,side,code,version,payload,payload_hash,created_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (iid, batch_id, side, it["code"], version, canon(payload), h, utcnow()),
        )
        if prev:
            conn.execute("UPDATE item SET superseded_by=? WHERE id=?", (iid, prev["item_id"]))
            _event(conn, batch_id, actor, "item_superseded",
                   {"side": side, "code": it["code"], "old_version": prev["version"],
                    "new_version": version})
        else:
            _event(conn, batch_id, actor, "item_added",
                   {"side": side, "code": it["code"], "version": version})
        new_ids_by_code[it["code"]] = iid

    for code, prev in old_by_code.items():
        if code not in new_ids_by_code:
            _event(conn, batch_id, actor, "item_removed",
                   {"side": side, "code": code, "version": prev["version"]})

    conn.execute("DELETE FROM inventory WHERE batch_id=? AND side=?", (batch_id, side))
    for seq, it in enumerate(items):
        conn.execute(
            "INSERT INTO inventory(batch_id,side,item_id,seq) VALUES (?,?,?,?)",
            (batch_id, side, new_ids_by_code[it["code"]], seq),
        )
    return {"new_ids": new_ids_by_code, "old_ids": {c: v["item_id"] for c, v in old_by_code.items()}}


def submit_inventory(batch_id: str, side: str, data: dict, db_path: Optional[str] = None) -> dict:
    with session(db_path) as conn:
        _require_open(conn, batch_id)
        _require_party(conn, batch_id, side)
        if _confirmed(conn, batch_id, side, "inventory"):
            raise ApiError(409, "stage_confirmed",
                           "清点已确认；请先撤回确认或对个别条目走更正",
                           {"stage": "inventory"})
        result = _replace_inventory(conn, batch_id, side, data["items"], f"party:{side}")

        # 套组成员引用按 code 迁移；找不到的成员清掉并登记
        sets = _set_membership(conn, batch_id, side)
        old_rows = conn.execute(
            "SELECT id, code FROM item WHERE batch_id=? AND side=?",
            (batch_id, side),
        ).fetchall()
        old_id_code = {r["id"]: r["code"] for r in old_rows}
        dropped_members: list[dict] = []
        new_sets: dict[str, list[str]] = {}
        conn.execute("DELETE FROM set_member WHERE batch_id=? AND side=?", (batch_id, side))
        for lbl, ids in sets.items():
            kept = []
            for iid in ids:
                code = old_id_code.get(iid)
                if code in result["new_ids"]:
                    kept.append(result["new_ids"][code])
                else:
                    dropped_members.append({"set": lbl, "code": code})
            new_sets[lbl] = kept
        for lbl, ids in new_sets.items():
            for iid in ids:
                conn.execute(
                    "INSERT OR IGNORE INTO set_member(batch_id,side,set_label,item_id)"
                    " VALUES (?,?,?,?)",
                    (batch_id, side, lbl, iid),
                )
        if dropped_members:
            _event(conn, batch_id, f"party:{side}", "set_member_removed_on_replace",
                   {"items": dropped_members})

        return {"submitted": len(data["items"]),
                "dropped_set_members": dropped_members}


def correct_item(batch_id: str, side: str, code: str, data: dict,
                 db_path: Optional[str] = None) -> dict:
    """资料更正：仅替换一条，只让受影响结论失效。"""
    new_item = data["item"]
    if new_item["code"] != code:
        raise ApiError(422, "code_immutable", "更正不可改变条目码（请删旧增新）")
    with session(db_path) as conn:
        _require_open(conn, batch_id)
        _require_party(conn, batch_id, side)
        current = _active_item_by_code(conn, batch_id, side, code)
        if not current:
            raise ApiError(404, "item_not_found", f"条目 {code} 不在当前清单中")
        # kind 不可变（换类等于换物，应新增）
        if current["payload"]["kind"] != new_item["kind"]:
            raise ApiError(422, "kind_immutable", "更正不可改变实物类别")

        # 直接写新版本（仅一条）
        payload = new_item
        h = sha_of(payload)
        if h == current["payload_hash"]:
            return {"code": code, "changed": False}

        # 在写入前推导"对方受影响套组"：与本方含该条目的套组经锁定对并合的对方标签
        pre_report = _assemble_report(conn, batch_id)
        own_sets = _set_membership(conn, batch_id, side)
        affected_set_labels = sorted(
            lbl for lbl, ids in own_sets.items() if current["item_id"] in ids)
        other_labels = _counterpart_labels(
            pre_report, side, affected_set_labels)

        iid = new_id()
        version = current["version"] + 1
        conn.execute(
            "INSERT INTO item(id,batch_id,side,code,version,payload,payload_hash,created_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (iid, batch_id, side, code, version, canon(payload), h, utcnow()),
        )
        conn.execute("UPDATE item SET superseded_by=? WHERE id=?", (iid, current["item_id"]))
        conn.execute(
            "UPDATE inventory SET item_id=? WHERE batch_id=? AND side=? AND item_id=?",
            (iid, batch_id, side, current["item_id"]),
        )
        _event(conn, batch_id, f"party:{side}", "item_corrected",
               {"side": side, "code": code, "old_version": current["version"],
                "new_version": version, "old_hash": current["payload_hash"], "new_hash": h})

        # 资料更正只让受影响结论失效：
        # - 本方：清点点头、套组头、含该条目的套组状态确认
        # - 对方：与受影响套组在同一组分（锁定对并合）中的套组状态确认
        affected = [("inventory", "*")]
        if affected_set_labels:
            affected.append(("grouping", "*"))
            affected += [("status", lbl) for lbl in affected_set_labels]
        dropped = _drop_confirmations(conn, batch_id, side, affected,
                                      f"item_corrected:{code}", f"party:{side}")
        other_side = _other(side)
        other_dropped = _drop_confirmations(
            conn, batch_id, other_side,
            [("status", l) for l in other_labels],
            f"counterpart_item_corrected:{side}:{code}", "system")
        return {"code": code, "changed": True, "version": version,
                "invalidated": [{"side": side, "stage": s, "scope": sc}
                                for s, sc in dropped]
                + [{"side": other_side, "stage": s, "scope": sc}
                   for s, sc in other_dropped]}


# ---------------------------------------------------------------- 套组分配

def assign_groups(batch_id: str, side: str, data: dict, db_path: Optional[str] = None) -> dict:
    with session(db_path) as conn:
        _require_open(conn, batch_id)
        _require_party(conn, batch_id, side)
        if _confirmed(conn, batch_id, side, "grouping"):
            raise ApiError(409, "stage_confirmed",
                           "套组已确认；请先撤回确认再重新分配", {"stage": "grouping"})
        active = {it["item_id"]: it["code"] for it in _active_items(conn, batch_id, side)}
        missing = []
        for lbl, codes in data["assignments"].items():
            for c in codes:
                if c not in {v for v in active.values()}:
                    missing.append({"set": lbl, "code": c})
        if missing:
            raise ApiError(422, "code_not_in_inventory", "存在未在本方清点清单中的条目码",
                           {"items": missing})
        code_to_id = {v: k for k, v in active.items()}
        conn.execute("DELETE FROM set_member WHERE batch_id=? AND side=?", (batch_id, side))
        for lbl, codes in sorted(data["assignments"].items()):
            for c in codes:
                conn.execute(
                    "INSERT OR IGNORE INTO set_member(batch_id,side,set_label,item_id)"
                    " VALUES (?,?,?,?)",
                    (batch_id, side, lbl, code_to_id[c]),
                )
        _event(conn, batch_id, f"party:{side}", "groups_assigned",
               {"assignments": {l: sorted(c) for l, c in data["assignments"].items()}})
        return {"sets": len(data["assignments"])}


# ---------------------------------------------------------------- 报告与依据哈希

def _observations_for_report(conn, batch_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM observation WHERE batch_id=? ORDER BY created_at, id",
        (batch_id,),
    ).fetchall()
    out = []
    for r in rows:
        ob = dict(r)
        payload = json.loads(ob["payload"])
        dec_rows = conn.execute(
            "SELECT * FROM observation_decision WHERE observation_id=? ORDER BY side",
            (ob["id"],),
        ).fetchall()
        decisions = {d["side"]: {"adopted": bool(d["adopted"]), "note": d["note"]}
                     for d in dec_rows}
        if len(decisions) == 2 and all(v["adopted"] for v in decisions.values()):
            status = "incorporated"
        elif len(decisions) == 2:
            status = "disputed"
        else:
            status = "pending"

        targets: list[tuple[str, str]] = []
        if ob["target_kind"] == "item":
            if ob["side_hint"]:
                it = _active_item_by_code(conn, batch_id, ob["side_hint"], ob["target_code"])
                if it:
                    targets.append((ob["side_hint"], it["item_id"]))
            else:
                for sd in ("handover", "receive"):
                    it = _active_item_by_code(conn, batch_id, sd, ob["target_code"])
                    if it:
                        targets.append((sd, it["item_id"]))
        elif ob["target_kind"] == "set":
            for sd in ("handover", "receive"):
                if ob["target_code"] in _set_membership(conn, batch_id, sd):
                    targets.append((sd, ob["target_code"]))
        witness = conn.execute("SELECT name FROM witness WHERE id=?", (ob["witness_id"],)).fetchone()
        out.append({"id": ob["id"], "witness_name": witness["name"] if witness else "?",
                    "target_kind": ob["target_kind"], "targets": targets,
                    "payload": payload, "payload_hash": ob["payload_hash"],
                    "status": status, "decisions": decisions})
    return out


def build_report(batch_id: str, db_path: Optional[str] = None) -> dict:
    with session(db_path) as conn:
        _get_batch(conn, batch_id)
        return _assemble_report(conn, batch_id)


def _assemble_report(conn, batch_id: str) -> dict:
    items_h = _active_items(conn, batch_id, "handover")
    items_r = _active_items(conn, batch_id, "receive")
    sets_h = _set_membership(conn, batch_id, "handover")
    sets_r = _set_membership(conn, batch_id, "receive")
    obs = _observations_for_report(conn, batch_id)
    report = recon.build_report(items_h, items_r, sets_h, sets_r, obs)
    report["batch_id"] = batch_id
    report["state"] = _state(conn, batch_id, report)
    return report


def _inv_basis(conn, batch_id: str, side: str) -> str:
    hashes = [it["payload_hash"] for it in _active_items(conn, batch_id, side)]
    return sha_of({"inventory": sorted(hashes)})


def _grp_basis(conn, batch_id: str, side: str, obs) -> str:
    sets = _set_membership(conn, batch_id, side)
    items = {it["item_id"]: it for it in _active_items(conn, batch_id, side)}
    members = {lbl: sorted(items[i]["payload_hash"] for i in ids if i in items)
               for lbl, ids in sorted(sets.items())}
    obs_hash = sorted(o["payload_hash"] for o in obs
                      if o["status"] == "incorporated"
                      and any(s == side for s, _ in o["targets"]))
    return sha_of({"grouping": members, "observations": obs_hash})


def _ev_basis(conn, batch_id: str, side: str, label: str, sets: dict, obs) -> str:
    items = {it["item_id"]: it for it in _active_items(conn, batch_id, side)}
    member_hashes = sorted(items[i]["payload_hash"] for i in sets.get(label, []) if i in items)
    obs_hash = sorted(o["payload_hash"] for o in obs if o["status"] == "incorporated")
    return sha_of({"status": label, "members": member_hashes, "observations": obs_hash})


def _common_stage(conn, batch_id: str, report: dict) -> str:
    if not (_confirmed(conn, batch_id, "handover", "inventory")
            and _confirmed(conn, batch_id, "receive", "inventory")):
        return "created"
    if not (_confirmed(conn, batch_id, "handover", "grouping")
            and _confirmed(conn, batch_id, "receive", "grouping")):
        return "inventory"
    # 状态按套组；所有闭合套组双方 status 行齐全才算 status
    for s in report["sets"]:
        label_h = next((l["label"] for l in s["labels"] if l["side"] == "handover"), None)
        label_r = next((l["label"] for l in s["labels"] if l["side"] == "receive"), None)
        if label_h is None or label_r is None:
            return "grouping"
        if not (_confirmed(conn, batch_id, "handover", "status", label_h)
                and _confirmed(conn, batch_id, "receive", "status", label_r)):
            return "grouping"
    return "status"


def _state(conn, batch_id: str, report: Optional[dict] = None) -> dict:
    batch = conn.execute("SELECT frozen_at FROM batch WHERE id=?", (batch_id,)).fetchone()
    report = report or _assemble_report(conn, batch_id)
    sides = {}
    for sd in ("handover", "receive"):
        stages = {}
        for st in STAGES:
            if st == "status":
                rows = conn.execute(
                    "SELECT scope, basis_hash FROM confirmation WHERE batch_id=? AND side=? AND stage='status'",
                    (batch_id, sd),
                ).fetchall()
                stages[st] = {r["scope"]: {"basis_hash": r["basis_hash"], "valid": True}
                              for r in rows}
            else:
                r = _confirmed(conn, batch_id, sd, st)
                stages[st] = {"basis_hash": r["basis_hash"], "valid": True} if r else None
        sides[sd] = stages
    return {"frozen": bool(batch["frozen_at"]),
            "common_stage": _common_stage(conn, batch_id, report),
            "sides": sides}


# ---------------------------------------------------------------- 确认

def confirm_inventory(batch_id: str, side: str, db_path: Optional[str] = None) -> dict:
    with session(db_path) as conn:
        _require_open(conn, batch_id)
        _require_party(conn, batch_id, side)
        if not _active_items(conn, batch_id, side):
            raise ApiError(409, "empty_inventory", "尚未提交清点清单，无法确认")
        report = _assemble_report(conn, batch_id)
        blocking = report["unmatched"]
        mine = [u for u in blocking if u["side"] == side
                and u["reason"] == "no_compatible_partner"]
        if mine:
            raise ApiError(409, "inventory_conflict",
                           "本方存在被硬冲突排除、无法配对的条目；请更正或由见证补证后再确认",
                           {"minimal_conflict_clues": _clues(mine)})
        if _confirmed(conn, batch_id, side, "inventory"):
            return {"already": True}
        basis = _inv_basis(conn, batch_id, side)
        conn.execute(
            "INSERT INTO confirmation(batch_id,side,stage,scope,basis_hash,note,created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (batch_id, side, "inventory", "*", basis, None, utcnow()),
        )
        _event(conn, batch_id, f"party:{side}", "confirmed",
               {"stage": "inventory", "basis_hash": basis})
        return {"stage": "inventory", "basis_hash": basis,
                "common_stage": _common_stage(conn, batch_id, _assemble_report(conn, batch_id))}


def confirm_grouping(batch_id: str, side: str, data: dict,
                     db_path: Optional[str] = None) -> dict:
    note = (data or {}).get("note")
    with session(db_path) as conn:
        _require_open(conn, batch_id)
        _require_party(conn, batch_id, side)
        if not _confirmed(conn, batch_id, side, "inventory"):
            raise ApiError(409, "stage_order", "请先确认清点")
        report = _assemble_report(conn, batch_id)
        sets = _set_membership(conn, batch_id, side)
        if not sets:
            raise ApiError(409, "no_groups", "尚未分配套组，无法确认")
        # 本方视角的阻断（仅清点/套组阶段可判定的问题）：
        # 候选组（证据不足）、未入组、空套组、单侧套组、套组分裂。
        # 缺类/见证未决等留给状态阶段提示。
        grouping_issue_kinds = {
            "set_empty", "set_one_sided", "set_split",
            "pair_member_unassigned", "ambiguous_membership",
        }
        clues: list[dict] = []
        for g in report["candidate_groups"]:
            codes = [c["code"] for c in g[side]]
            if codes:
                clues.append({"issue": "ambiguous_candidates", "kind": g["kind"],
                              "own_codes": codes,
                              "other_codes": [c["code"] for c in g["receive" if side == "handover" else "handover"]]})
        unassigned = [u for u in report["global_issues"] if u["issue"] == "items_not_in_set"]
        mine_unassigned = []
        if unassigned:
            mine_unassigned = [it for it in unassigned[0]["items"] if it["side"] == side]
        own_labels = set(sets)
        bad_sets = []
        for s in report["sets"]:
            own = [l["label"] for l in s["labels"]
                   if l["side"] == side and l["label"] in own_labels]
            if own and not s["closed"]:
                blocking = [i for i in s["issues"] if i["issue"] in grouping_issue_kinds]
                if blocking:
                    bad_sets.append({"set": own[0],
                                     "issues": [i["issue"] for i in blocking]})
        if clues or mine_unassigned or bad_sets:
            raise ApiError(409, "grouping_conflict",
                           "套组归属未闭合（证据不足的候选必须保留，不可强行定案）",
                           {"minimal_conflict_clues": {
                               "ambiguous": clues,
                               "unassigned": [it["code"] for it in mine_unassigned],
                               "sets": bad_sets,
                           }})
        if _confirmed(conn, batch_id, side, "grouping"):
            return {"already": True}
        obs = _observations_for_report(conn, batch_id)
        basis = _grp_basis(conn, batch_id, side, obs)
        conn.execute(
            "INSERT INTO confirmation(batch_id,side,stage,scope,basis_hash,note,created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (batch_id, side, "grouping", "*", basis, note, utcnow()),
        )
        _event(conn, batch_id, f"party:{side}", "confirmed",
               {"stage": "grouping", "basis_hash": basis, "note": note})
        return {"stage": "grouping", "basis_hash": basis,
                "common_stage": _common_stage(conn, batch_id, _assemble_report(conn, batch_id))}


def _set_report_for_label(report: dict, side: str, label: str) -> Optional[dict]:
    for s in report["sets"]:
        if any(l["side"] == side and l["label"] == label for l in s["labels"]):
            return s
    return None


def confirm_status(batch_id: str, side: str, data: dict,
                   db_path: Optional[str] = None) -> dict:
    with session(db_path) as conn:
        _require_open(conn, batch_id)
        _require_party(conn, batch_id, side)
        if not _confirmed(conn, batch_id, side, "grouping"):
            raise ApiError(409, "stage_order", "请先确认套组")
        report = _assemble_report(conn, batch_id)
        sets = _set_membership(conn, batch_id, side)
        asked = {x["set_label"]: x for x in data["sets"]}
        unknown = sorted(set(asked) - set(sets))
        if unknown:
            raise ApiError(404, "set_not_found", "存在本方未分配的套组标签", {"labels": unknown})
        obs = _observations_for_report(conn, batch_id)

        written, skipped = [], []
        for label in sorted(set(sets) | set(asked)):
            if label not in asked:
                raise ApiError(422, "sets_missing",
                               "请逐套给出状态确认（每套一条）",
                               {"missing": sorted(set(sets) - set(asked))})
            sr = _set_report_for_label(report, side, label)
            entry = asked[label]
            # 闭合性阻断（结构/证据/见证类）
            if sr is None or not sr["closed"]:
                hard_issues = [i for i in (sr["issues"] if sr else [])
                               if i["issue"] in ("ambiguous_membership",
                                                 "observation_unresolved",
                                                 "witness_contradiction",
                                                 "set_split", "set_one_sided",
                                                 "set_incomplete_kinds", "set_empty",
                                                 "pair_member_unassigned")]
                raise ApiError(409, "status_set_not_closed",
                               f"套组 {label} 未闭合，不能作状态确认",
                               {"set": label,
                                "minimal_conflict_clues": hard_issues})
            if sr["discrepancies"] and not entry["accept_discrepancy"]:
                raise ApiError(409, "discrepancy_ack_required",
                               f"套组 {label} 存在需知晓的非阻断性差异",
                               {"set": label,
                                "minimal_conflict_clues": sr["discrepancies"]})
            if _confirmed(conn, batch_id, side, "status", label):
                skipped.append(label)
                continue
            basis = _ev_basis(conn, batch_id, side, label, sets, obs)
            conn.execute(
                "INSERT INTO confirmation(batch_id,side,stage,scope,basis_hash,note,created_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (batch_id, side, "status", label, basis, entry.get("note"), utcnow()),
            )
            _event(conn, batch_id, f"party:{side}", "confirmed",
                   {"stage": "status", "scope": label, "basis_hash": basis,
                    "accept_discrepancy": entry["accept_discrepancy"],
                    "note": entry.get("note")})
            written.append(label)
        return {"confirmed": written, "already": skipped,
                "common_stage": _common_stage(conn, batch_id, _assemble_report(conn, batch_id))}


def withdraw_last(batch_id: str, side: str, db_path: Optional[str] = None) -> dict:
    """冻结前任一方撤回最近确认；回退到上一共同状态，不删资料。"""
    with session(db_path) as conn:
        _require_open(conn, batch_id)
        _require_party(conn, batch_id, side)
        # 找到该方当前“最高”确认
        has_status = conn.execute(
            "SELECT scope FROM confirmation WHERE batch_id=? AND side=? AND stage='status'"
            " ORDER BY scope", (batch_id, side)).fetchall()
        target_stage = target_scope = None
        if has_status:
            target_stage, target_scope = "status", has_status[0]["scope"]
        elif _confirmed(conn, batch_id, side, "grouping"):
            target_stage, target_scope = "grouping", "*"
        elif _confirmed(conn, batch_id, side, "inventory"):
            target_stage, target_scope = "inventory", "*"
        else:
            raise ApiError(409, "nothing_to_withdraw", "该方尚无确认可撤回")

        if target_stage == "status":
            # 撤回最近一次：created_at 最大的那条
            last = conn.execute(
                "SELECT scope FROM confirmation WHERE batch_id=? AND side=? AND stage='status'"
                " ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (batch_id, side)).fetchone()
            target_scope = last["scope"]

        conn.execute(
            "DELETE FROM confirmation WHERE batch_id=? AND side=? AND stage=? AND scope=?",
            (batch_id, side, target_stage, target_scope),
        )
        _event(conn, batch_id, f"party:{side}", "withdrew",
               {"stage": target_stage, "scope": target_scope})
        report = _assemble_report(conn, batch_id)
        return {"withdrew": {"stage": target_stage, "scope": target_scope},
                "common_stage": report["state"]["common_stage"]}


def _clues(unmatched: list[dict]) -> list[dict]:
    out = []
    for u in unmatched:
        out.append({"code": u["code"], "kind": u["kind"], "reason": u["reason"],
                    "conflicts": u["conflicts"]})
    return out


# ---------------------------------------------------------------- 见证观察

def add_observation(batch_id: str, witness_id: str, data: dict,
                    db_path: Optional[str] = None) -> dict:
    with session(db_path) as conn:
        _require_open(conn, batch_id)
        w = conn.execute("SELECT * FROM witness WHERE id=? AND batch_id=?",
                         (witness_id, batch_id)).fetchone()
        if not w:
            raise ApiError(404, "witness_not_found", "见证人不在该批次中")
        oid = new_id()
        payload = {k: v for k, v in data.items()
                   if v is not None and k not in
                   ("target_kind", "target_code", "side_hint")}
        h = sha_of(payload)
        conn.execute(
            "INSERT INTO observation(id,batch_id,witness_id,target_kind,target_code,"
            "side_hint,payload,payload_hash,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (oid, batch_id, witness_id, data["target_kind"], data["target_code"],
             data.get("side_hint"), canon(payload), h, utcnow()),
        )
        _event(conn, batch_id, f"witness:{witness_id}", "observation_added",
               {"observation_id": oid, "target_kind": data["target_kind"],
                "target_code": data["target_code"], "side_hint": data.get("side_hint")})
        return {"observation_id": oid, "payload_hash": h}


def decide_observation(batch_id: str, observation_id: str, side: str, data: dict,
                       db_path: Optional[str] = None) -> dict:
    with session(db_path) as conn:
        _require_open(conn, batch_id)
        _require_party(conn, batch_id, side)
        ob = conn.execute("SELECT * FROM observation WHERE id=? AND batch_id=?",
                          (observation_id, batch_id)).fetchone()
        if not ob:
            raise ApiError(404, "observation_not_found", "观察记录不存在")
        conn.execute(
            "INSERT INTO observation_decision(observation_id,side,adopted,note,decided_at)"
            " VALUES (?,?,?,?,?) ON CONFLICT(observation_id,side) DO UPDATE SET"
            " adopted=excluded.adopted, note=excluded.note, decided_at=excluded.decided_at",
            (observation_id, side, 1 if data["adopted"] else 0, data.get("note"), utcnow()),
        )
        _event(conn, batch_id, f"party:{side}", "observation_decided",
               {"observation_id": observation_id, "adopted": data["adopted"],
                "note": data.get("note")})
        obs = _observations_for_report(conn, batch_id)
        cur = next(o for o in obs if o["id"] == observation_id)

        # 并入新证据会改变依据：失效受影响结论（同更正语义）。
        # 清点声明本身未变，清点点头保留；套组头与受影响套组的双方状态确认失效。
        if cur["status"] == "incorporated":
            sets_h = _set_membership(conn, batch_id, "handover")
            sets_r = _set_membership(conn, batch_id, "receive")
            sets_by_side = {"handover": sets_h, "receive": sets_r}
            fresh = _assemble_report(conn, batch_id)
            status_drops: list[tuple[str, str]] = []
            for sd, key in cur["targets"]:
                if cur["target_kind"] == "set":
                    own = [key] if key in sets_by_side[sd] else []
                else:
                    own = [lbl for lbl, ids in sets_by_side[sd].items() if key in ids]
                status_drops += [(sd, l) for l in own]
                status_drops += [(_other(sd), l)
                                 for l in _counterpart_labels(fresh, sd, own)]
            for sd in ("handover", "receive"):
                drops = [("grouping", "*")]
                drops += [("status", l) for (s2, l) in sorted(set(status_drops))
                          if s2 == sd]
                _drop_confirmations(conn, batch_id, sd, drops,
                                    f"observation_incorporated:{observation_id}", "system")
        return {"observation_id": observation_id, "status": cur["status"],
                "decisions": cur["decisions"]}


# ---------------------------------------------------------------- 查询

def get_batch(batch_id: str, db_path: Optional[str] = None) -> dict:
    with session(db_path) as conn:
        b = _get_batch(conn, batch_id)
        parties = {r["side"]: {"name": r["name"], "contact": r["contact"]}
                   for r in conn.execute(
                       "SELECT side,name,contact FROM party WHERE batch_id=?", (batch_id,))}
        witnesses = [dict(r) for r in conn.execute(
            "SELECT id,name,contact,relation FROM witness WHERE batch_id=? ORDER BY created_at",
            (batch_id,))]
        report = _assemble_report(conn, batch_id)
        return {
            "batch_id": b["id"], "title": b["title"], "note": b["note"],
            "created_at": b["created_at"], "frozen_at": b["frozen_at"],
            "parties": parties, "witnesses": witnesses,
            "state": report["state"],
            "report": report,
        }


def get_events(batch_id: str, limit: int = 200, db_path: Optional[str] = None) -> dict:
    with session(db_path) as conn:
        _get_batch(conn, batch_id)
        rows = conn.execute(
            "SELECT at, actor, kind, detail FROM event WHERE batch_id=? "
            "ORDER BY id DESC LIMIT ?", (batch_id, limit),
        ).fetchall()
        return {"batch_id": batch_id, "events": [
            {"at": r["at"], "actor": r["actor"], "kind": r["kind"],
             "detail": json.loads(r["detail"])} for r in rows]}


# ---------------------------------------------------------------- 冻结

def freeze(batch_id: str, data: dict, db_path: Optional[str] = None) -> dict:
    with session(db_path) as conn:
        _require_open(conn, batch_id)
        b = conn.execute("SELECT * FROM batch WHERE id=?", (batch_id,)).fetchone()
        if not _party(conn, batch_id, "handover") or not _party(conn, batch_id, "receive"):
            raise ApiError(409, "parties_incomplete", "交出方与接收方都须登记")
        report = _assemble_report(conn, batch_id)
        state = report["state"]

        problems: list[dict] = []
        if report["unmatched"]:
            problems.append({"issue": "unmatched_items",
                             "clues": _clues(report["unmatched"])})
        if report["candidate_groups"]:
            problems.append({"issue": "ambiguous_candidates_remain",
                             "count": len(report["candidate_groups"])})
        not_closed = [{"set_id": s["set_id"], "issues":
                       [i["issue"] for i in s["issues"]]}
                      for s in report["sets"] if not s["closed"]]
        if not_closed:
            problems.append({"issue": "sets_not_closed", "sets": not_closed})
        if not report["sets"]:
            problems.append({"issue": "no_sets", "detail": "不存在任何套组"})
        if state["common_stage"] != "status":
            problems.append({"issue": "not_jointly_confirmed",
                             "common_stage": state["common_stage"]})
        global_blockers = [i for i in report["global_issues"]
                           if i["issue"] in ("observation_unresolved",
                                             "witness_contradiction")]
        if global_blockers:
            problems.append({"issue": "global_evidence_unresolved",
                             "clues": global_blockers})
        if problems:
            raise ApiError(409, "freeze_blocked", "闭合条件不满足，保管责任不得转移",
                           {"minimal_conflict_clues": problems})

        obs = _observations_for_report(conn, batch_id)

        # —— 输入哈希：覆盖全部原始声明（含历史版本）、见证、确认依据 ——
        item_rows = conn.execute(
            "SELECT side,code,version,payload_hash,superseded_by FROM item "
            "WHERE batch_id=? ORDER BY side,code,version", (batch_id,)).fetchall()
        input_material = {
            "title": b["title"],
            "items": [{"side": r["side"], "code": r["code"], "version": r["version"],
                       "payload_hash": r["payload_hash"]} for r in item_rows],
            "observations": [{"id": o["id"], "payload_hash": o["payload_hash"],
                              "status": o["status"], "decisions": o["decisions"]} for o in obs],
            "confirmations": [dict(r) for r in conn.execute(
                "SELECT side,stage,scope,basis_hash FROM confirmation WHERE batch_id=? "
                "ORDER BY side,stage,scope", (batch_id,))],
            "group_membership": {
                sd: {l: sorted(ids) for l, ids in
                     _set_membership(conn, batch_id, sd).items()}
                for sd in ("handover", "receive")},
        }
        input_hash = sha_of(input_material)

        # —— 原始声明快照：含全部历史版本，题名原样保存但不参与任何认定 ——
        declarations = {}
        for sd in ("handover", "receive"):
            rows = conn.execute(
                "SELECT id,code,version,payload,payload_hash,superseded_by,created_at "
                "FROM item WHERE batch_id=? AND side=? ORDER BY code,version",
                (batch_id, sd)).fetchall()
            declarations[sd] = [{
                "item_id": r["id"], "code": r["code"], "version": r["version"],
                "payload": json.loads(r["payload"]), "payload_hash": r["payload_hash"],
                "superseded_by": r["superseded_by"], "created_at": r["created_at"],
            } for r in rows]

        # —— 分歧处置留痕：从事件日志抽取失效/撤回/见证处置 + 最终闭合报告摘要 ——
        dispute_events = [dict(r) for r in conn.execute(
            "SELECT at,actor,kind,detail FROM event WHERE batch_id=? AND kind IN "
            "('confirmation_invalidated','withdrew','observation_decided',"
            "'item_corrected','item_superseded') ORDER BY id", (batch_id,))]
        for e in dispute_events:
            e["detail"] = json.loads(e["detail"])

        final_report = {
            "locked_pairs": report["locked_pairs"],
            "sets": [{k: s[k] for k in ("set_id", "labels", "closed",
                                        "member_counts", "pairs", "discrepancies")}
                     for s in report["sets"]],
        }

        at = utcnow()
        snapshot = {
            "batch_id": batch_id,
            "frozen_at": at,
            "input_hash": input_hash,
            "parties": {sd: (lambda r: {"name": r["name"], "contact": r["contact"]} if r else None)(
                conn.execute("SELECT name,contact FROM party WHERE batch_id=? AND side=?",
                             (batch_id, sd)).fetchone())
                for sd in ("handover", "receive")},
            "witnesses": [dict(r) for r in conn.execute(
                "SELECT id,name,relation FROM witness WHERE batch_id=?", (batch_id,))],
            "declarations": declarations,
            "observations": [{"id": o["id"], "witness": o["witness_name"],
                              "payload": o["payload"], "payload_hash": o["payload_hash"],
                              "decisions": o["decisions"], "status": o["status"]}
                             for o in obs],
            "dispute_resolution": dispute_events,
            "final_report": final_report,
            "responsibility": {
                "from": "handover", "to": "receive",
                "transferred_at": at,
                "note": "全部套组（木版/包纸/样张）闭合且双方共同确认，保管责任转移",
            },
            "note": data.get("note"),
        }
        snapshot_json = canon(snapshot)
        freeze_hash = sha_of(snapshot_json)
        conn.execute(
            "INSERT INTO freeze_record(batch_id,snapshot,hash,created_at) VALUES (?,?,?,?)",
            (batch_id, snapshot_json, freeze_hash, at),
        )
        conn.execute(
            "UPDATE batch SET frozen_at=?, freeze_hash=? WHERE id=?",
            (at, freeze_hash, batch_id),
        )
        _event(conn, batch_id, "system", "frozen",
               {"frozen_at": at, "input_hash": input_hash, "freeze_hash": freeze_hash,
                "responsibility_transferred": {"from": "handover", "to": "receive"},
                "note": data.get("note")})
        return {"batch_id": batch_id, "frozen_at": at,
                "input_hash": input_hash, "freeze_hash": freeze_hash}


def get_freeze_record(batch_id: str, db_path: Optional[str] = None) -> dict:
    with session(db_path) as conn:
        _get_batch(conn, batch_id)
        r = conn.execute(
            "SELECT snapshot,hash,created_at FROM freeze_record WHERE batch_id=?",
            (batch_id,)).fetchone()
        if not r:
            raise ApiError(404, "not_frozen", "批次尚未冻结")
        return {"batch_id": batch_id, "created_at": r["created_at"],
                "hash": r["hash"], "snapshot": json.loads(r["snapshot"])}
