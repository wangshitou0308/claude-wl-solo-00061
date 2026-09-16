"""Pydantic 模型：交接核对 API 的入参结构。

设计原则：
- 校验层只做结构与必填校验；归一化（色版角色、刻面方向等同义归并）放在 recon 层。
- 题名 (title) 只登记、不参与任何同一认定，因此这里不设唯一约束。
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator

# 交出方 / 接收方 / 见证人
Side = Literal["handover", "receive"]

# 实物三类：木版 / 包纸 / 试印样
ItemKind = Literal["block", "wrapper", "sample"]

# 状态机三阶段：清点 -> 套组 -> 状态
Stage = Literal["inventory", "grouping", "status"]


class Person(BaseModel):
    name: str = Field(min_length=1)
    contact: Optional[str] = None


class BatchCreate(BaseModel):
    title: Optional[str] = Field(default=None, description="交接批次题名，仅供检索，不参与认定")
    note: Optional[str] = None


class PartyRegister(BaseModel):
    """交出方 / 接收方登记。同一批次两个角色各只能登记一人。"""
    side: Side
    person: Person


class WitnessRegister(BaseModel):
    person: Person
    relation: Optional[str] = Field(default=None, description="与家属关系说明")


class Dimensions(BaseModel):
    """毫米为单位；缺测维度可只填一个。"""
    width_mm: Optional[float] = Field(default=None, ge=0)
    height_mm: Optional[float] = Field(default=None, ge=0)


class ItemIn(BaseModel):
    """清点条目。调用方自定条目码（同方内唯一），后续更正引用。"""
    kind: ItemKind
    code: str = Field(min_length=1)
    title: Optional[str] = None
    dims: Optional[Dimensions] = None

    # —— 木版 ——
    color_role: Optional[str] = Field(default=None, description="色版角色，如 线版/墨版/色版（红/黄…）")
    gaps: Optional[list[tuple[float, float]]] = Field(
        default=None, description="套准缺口坐标列表 (x_mm, y_mm)，取自版面参考点"
    )
    face_dir: Optional[str] = Field(default=None, description="刻面方向，如 正刻/反刻")
    damage: Optional[list[str]] = Field(default=None, description="损伤描述")
    inscriptions: Optional[list[str]] = Field(default=None, description="题记原文/释文")

    # —— 包纸 ——
    material: Optional[str] = None

    # —— 试印样 ——
    colors: Optional[list[str]] = Field(default=None, description="样张上呈现的色")
    clues: Optional[list[str]] = Field(default=None, description="样张线索：与木版对应的缺口/题记/色名等观察")

    note: Optional[str] = None

    @model_validator(mode="after")
    def _check_kind_fields(self) -> "ItemIn":
        if self.kind == "wrapper" and not self.material:
            raise ValueError("包纸(wrapper)必须提供 material")
        return self


class InventorySubmit(BaseModel):
    """一方的整批清点。整单 PUT 替换；确认后改条目需走更正/撤回。"""
    items: list[ItemIn] = Field(default_factory=list)


class GroupAssign(BaseModel):
    """把本方条目分配到本方套组标签。未列出的条目视为未入组。"""
    assignments: dict[str, list[str]] = Field(
        description="套组标签 -> 本方条目码列表"
    )


class GroupConfirm(BaseModel):
    note: Optional[str] = None


class StatusConfirmSet(BaseModel):
    set_label: str
    # 对该套全部非阻断性差异（损伤/题记/缺口等）的知晓确认
    accept_discrepancy: bool = False
    note: Optional[str] = None


class StatusConfirm(BaseModel):
    sets: list[StatusConfirmSet]


class ObservationCreate(BaseModel):
    """见证人补录实物观察。target 可为条目或套组。"""
    target_kind: Literal["item", "set"] = "item"
    # item: 条目码（需注明观察所依据的一方，必要时两方都要补）；set: 任一方套组标签
    target_code: str = Field(min_length=1)
    side_hint: Optional[Side] = Field(
        default=None, description="观察的是哪一方清单中的该条目码；留空表示两方同名条目均适用"
    )
    color_role: Optional[str] = None
    gaps: Optional[list[tuple[float, float]]] = None
    face_dir: Optional[str] = None
    damage: Optional[list[str]] = None
    inscriptions: Optional[list[str]] = None
    material: Optional[str] = None
    colors: Optional[list[str]] = None
    dims: Optional[Dimensions] = None
    note: Optional[str] = None


class DecisionIn(BaseModel):
    adopted: bool
    note: Optional[str] = None


class ItemCorrection(BaseModel):
    """资料更正：替换单条，仅使受影响结论失效。"""
    item: ItemIn


class FreezeIn(BaseModel):
    note: Optional[str] = None
