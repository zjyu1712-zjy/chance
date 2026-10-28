# -*- coding: utf-8 -*-
"""机会卡 / 输入记录 / 执行日志持久化层（v1.4，程序管理标识/时间/执行）。

v1.4 相对 v1.2.1 调整（业务状态分类重构，历史任务保留原状态原文，不批量改写）：
 1) 四种业务状态改为：候选机会（待验证）、建议验证、已有产品匹配、暂不形成机会；
    新任务不再生成“继续补证”。缺证/未充分验证但已有可追溯线索的方向使用“候选机会（待验证）”。
 2) “候选机会（待验证）/ 暂不形成机会”为缺证状态，允许 support_evidence 为空数组，
    显式无证据不是结构错误；不得通过恢复来源/删历史/补假来源绕过。“建议验证/已有产品匹配”仍要求非空支持证据与产品对照。
 3) 严格校验字段路径：evidence 的 field_path 必须是记录真实存在的叶子字段
    （source: summary/supports/not_supports/...；product: name/version_spec/...；
     input: raw_text/...），拒绝“summary/supports”这类拼接/不存在的复合路径；
    resolved_content 返回所指定字段的值，而非整条记录。产品对照、三条件的
    实质结论也必须绑定有效来源字段，不能只检查 support_evidence 就宣称全部合格。
 3) 结构化成立程度：three_conditions 的 demand_support / supply_gap 使用
    {degree, basis, note}；basis 为空或全为“未知”时 degree 不得为“已成立/初步支持”，
    即“未知不能支撑缺口/需求成立”。保留 0蔗糖标签≠主观低甜、旧款发布≠当前在售。
 4) 当前在售状态字段 current_in_sale 默认“未知”，归档日期固定 2026-10-02。
 5) 保存失败上限由程序控制：同一轮最多“首次提交 + 1 次修复”，再失败即结束并保留
    旧卡，禁止不断重试或伪造证据来过校验；失败/阻断事件写入执行日志留痕。
 6) 来源排除沿派生链生效（仅当前任务覆盖层）：排除了 S02，则仅由 S02 派生的
    P01 一并失效；基础档案不改。
 7) 展示与保存同源：save 返回经程序校验的规范卡 + 程序生成的展示文本，模型最终
    摘要/表格必须逐字使用该数据，不得另写冲突结论。

存储位置：
  assets/opportunity/_active_task.json          当前任务指针（含保存修复预算）
  assets/opportunity/{task_id}/cards.json       机会卡（全部版本，不可覆盖）
  assets/opportunity/{task_id}/inputs.json      用户输入记录
  assets/opportunity/{task_id}/runlog.json      执行日志（真实调用处采集）
  assets/opportunity/{task_id}/exclusions.json  任务级来源排除
"""
import json
import os
import re
import datetime
from typing import Any, Dict, List, Optional

from storage.materials_store import get_material_store, VERIFICATION_DATE, PACKAGE_DATE

WORKSPACE = os.getenv("COZE_WORKSPACE_PATH", "/workspace/projects")
_OPP_DIR = os.path.join(WORKSPACE, "assets", "opportunity")
_ACTIVE_FILE = os.path.join(_OPP_DIR, "_active_task.json")

PROMPT_VERSION = "v1.4"
# 业务状态分类（v1.4 起启用；旧卡仍保留原文，不批量改写）
ALLOWED_STATUS = ["候选机会（待验证）", "建议验证", "已有产品匹配", "暂不形成机会"]
DEGREE_STATUS = ["已成立", "初步支持", "未支持", "未知"]
IN_SALE_STATUS = ["未知", "在售", "不在售", "部分在售"]
SATISFACTION_STATUS = ["满足", "不满足", "未知"]   # 逐产品对照的满足状态；无体验/未查到只能记 未知
# 允许空支持数组的状态（缺证不构成结构错误）
EMPTY_SUPPORT_OK_STATUS = {"候选机会（待验证）", "暂不形成机会"}
# 必须含非空支持数组的状态
REQUIRE_SUPPORT_STATUS = {"建议验证", "已有产品匹配"}
ALLOWED_SCOPE = ["非模拟资料", "仅模拟", "真实资料"]   # 真实资料仅为读取历史
NEW_SCOPE = ["非模拟资料", "仅模拟"]
# 四模式固定名称与顺序（v1.2，单一智能体的四个分析入口）
MODES = ["消费者捕手", "热点捕手", "市场缺口捕手", "企业灵感捕手"]

# 旧版本命名的展示映射：原始审计记录保留原值，仅展示/导出时映射为当前版本名称
MODE_DISPLAY_MAP = {"市场捕手": "市场缺口捕手"}


def _display_mode(mode):
    """把卡上存储的模式标识映射为当前版本展示名；未知值原样返回。"""
    return MODE_DISPLAY_MAP.get(mode, mode)

ALLOWED_FIELDS = {
    "source": {"source_id", "title", "date", "nature", "reading_depth", "summary",
               "supports", "not_supports", "url", "category", "related_products"},
    "product": {"product_id", "name", "version_spec", "source_ids", "attribution",
                "comparison_dims", "unknowns", "category"},
    "input": {"input_id", "raw_text", "mode", "user_ref", "unverified_links",
              "material_kind", "is_simulation", "extracted_facts", "user_claims"},
}
DEFAULT_FIELD = {"source": "summary", "product": "name", "input": "raw_text"}
MAX_SAVE_FIX = 1   # 同一分析轮最多 1 次修复：首次提交失败不占额度，其后最多 1 次修复，修复再失败即结束
# 三条件 canonical 名称（以程序为准，各处统一，模型不得改名）
THREE_KEYS = ["demand_support", "supply_gap", "verify_value"]
THREE_OBJ = ["degree", "basis", "note"]
# 必填顶层字段（proposed_direction 缺证时可留空，不在此列；comparison/counter_evidence 缺证时允许为空，已由 _validate 按状态约束）
REQUIRED_TOP = [
    "mode", "judgment_scope", "status", "original_hypothesis", "demand_hypothesis",
    "target_people_scene", "keep_experience", "improve_experience",
    "three_conditions", "key_unknowns", "next_step_verification", "change_summary",
]


def _now() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _task_dir(task_id: str) -> str:
    safe = re.sub(r"[^0-9A-Za-z_\-]", "_", task_id)
    d = os.path.join(_OPP_DIR, safe)
    os.makedirs(d, exist_ok=True)
    return d


def _load_json(path: str, default: Any) -> Any:
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _save_json(path: str, data: Any) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _read_model_config() -> Dict[str, Any]:
    try:
        cfg_path = os.path.join(WORKSPACE, "config", "agent_llm_config.json")
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        return {
            "model": cfg.get("config", {}).get("model", "未知"),
            "temperature": cfg.get("config", {}).get("temperature", "未知"),
            "top_p": cfg.get("config", {}).get("top_p", "未知"),
            "max_completion_tokens": cfg.get("config", {}).get("max_completion_tokens", "未知"),
        }
    except Exception:
        return {"model": "未知"}


def _field_value(content: Dict[str, Any], kind: str, field_path: str) -> Optional[str]:
    if not field_path:
        field_path = DEFAULT_FIELD[kind]
    if field_path not in ALLOWED_FIELDS[kind]:
        return None
    v = content.get(field_path)
    if v is None:
        return ""
    if isinstance(v, (str, int, float)):
        return str(v)
    return json.dumps(v, ensure_ascii=False)


class OpportunityStore:
    """v1.2.1：程序管理任务与记录标识，程序控制保存修复上限，采集真实执行日志。"""

    # ---------- 当前任务（程序管理；不删除旧任务） ----------
    def _task_meta_path(self, task_id: str) -> str:
        return os.path.join(_OPP_DIR, task_id, "task_meta.json")

    def get_task_meta(self, task_id: str = "") -> Dict[str, Any]:
        task_id = task_id or ((self.get_active_task() or {}).get("task_id") or "")
        if not task_id:
            return {}
        p = self._task_meta_path(task_id)
        if os.path.isfile(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:  # noqa: BLE001
                return {}
        return {}

    def set_task_meta(self, task_id: str, **kw) -> Dict[str, Any]:
        m = self.get_task_meta(task_id)
        m.update(kw)
        os.makedirs(os.path.dirname(self._task_meta_path(task_id)), exist_ok=True)
        with open(self._task_meta_path(task_id), "w", encoding="utf-8") as f:
            json.dump(m, f, ensure_ascii=False, indent=2)
        return m

    def get_active_task(self) -> Optional[Dict[str, Any]]:
        return _load_json(_ACTIVE_FILE, None)

    def _require_active(self) -> Dict[str, Any]:
        act = self.get_active_task()
        if not act or not act.get("task_id"):
            raise ValueError("尚未开始任务：请先调用「开始新任务」建立当前任务。")
        return act

    def _assign_task_id(self) -> str:
        # 现存最大 T- 编号 + 1：确保删除旧任务后不重置编号、不复用已用编号（避免与备份恢复冲突）
        maxn = 0
        if os.path.isdir(_OPP_DIR):
            for name in os.listdir(_OPP_DIR):
                m = re.fullmatch(r"T-(\d{3,})", name)
                if m:
                    maxn = max(maxn, int(m.group(1)))
        return f"T-{maxn+1:03d}"

    def start_new_task(self, mode: str = "", scope: str = "非模拟资料",
                       simulation: bool = False, note: str = "") -> Dict[str, Any]:
        if scope not in NEW_SCOPE:
            raise ValueError(f"任务判断范围必须是 {NEW_SCOPE}，当前：{scope!r}")
        if mode and mode not in MODES:
            raise ValueError(f"mode 必须是 {MODES}，当前：{mode!r}")
        task_id = self._assign_task_id()
        act = {
            "task_id": task_id,
            "created_at": _now(),
            "updated_at": _now(),
            "mode": mode,
            "scope": scope,
            "simulation": bool(simulation),
            "materials_allowed": "全部 S01—S09 / P01—P09" if not simulation else "仅模拟（不引入S/P真实资料）",
            "save_fix_used": 0,
            "save_blocked": False,
            "initial_submit_done": False,
            "note": note,
        }
        _save_json(_ACTIVE_FILE, act)
        _task_dir(task_id)
        self.set_task_meta(task_id, mode=mode, scope=scope,
                           created_at=act["created_at"], simulation=bool(simulation), note=note or "")
        self.log_event("task_start", {"created": task_id, "mode": mode, "scope": scope})
        return act

    def continue_task(self, note: str = "") -> Dict[str, Any]:
        act = self._require_active()
        act["updated_at"] = _now()
        _save_json(_ACTIVE_FILE, act)
        return act

    def set_mode(self, mode: str) -> Dict[str, Any]:
        """同一任务内切换分析模式（不新增任务、不清除输入/机会卡/排除）。
        仅更新入口模式，供后续分析使用；审计保留原记录。"""
        act = self._require_active()
        if mode not in MODES:
            raise ValueError(f"mode 必须是 {MODES}，当前：{mode!r}")
        old = act.get("mode", "")
        act["mode"] = mode
        act["updated_at"] = _now()
        _save_json(_ACTIVE_FILE, act)
        self.set_task_meta(act["task_id"], mode=mode)
        self.log_event("mode_switch", {"from": old, "to": mode, "task": act["task_id"]})
        return act

    def activate_task(self, task_id: str) -> Dict[str, Any]:
        """重新打开一个历史任务：从其落盘记录重建活动上下文（不删除任何历史）。"""
        tdir = os.path.join(_OPP_DIR, task_id)
        if not os.path.isdir(tdir):
            raise ValueError(f"任务不存在：{task_id}")
        rec = self.get_task_meta(task_id)
        cards = self.load_cards(task_id)
        inputs = _load_json(os.path.join(tdir, "inputs.json"), [])
        # 模式/范围/时间优先采用任务自身独立的持久化记录（可靠恢复，不编造）
        mode = (rec.get("mode") or "") if rec else ""
        scope = (rec.get("scope") or "非模拟资料") if rec else "非模拟资料"
        simulation = bool(rec.get("simulation")) if rec else False
        created_at = (rec.get("created_at") or None) if rec else None
        if not mode and cards:
            mode = cards[-1].get("mode") or mode
        if not scope and cards:
            scope = cards[-1].get("judgment_scope") or scope
        if not simulation and cards:
            simulation = bool(cards[-1].get("simulation", simulation))
        # 老任务无记录时，用可靠的既有记录恢复创建时间，缺则留空不编造
        if created_at is None:
            if inputs:
                created_at = inputs[0].get("recorded_at") or None
            elif cards:
                created_at = cards[0].get("created_at") or cards[0].get("updated_at") or None
        # 模式/范围/时间仍缺失时：以该任务自己的运行日志（task_start 原始创建 + mode_switch 最新有效模式）恢复，不编造
        if (not mode) or (not scope) or (created_at is None):
            rl_created, rl_mode, rl_scope = self._derive_from_runlog(task_id)
            if not mode and rl_mode:
                mode = rl_mode
            if not scope and rl_scope:
                scope = rl_scope
            if created_at is None and rl_created:
                created_at = rl_created
        dev_self_test = bool(rec.get("dev_self_test")) if rec else False
        if dev_self_test:
            # 开发自测·合成材料：新分析遵守模拟隔离；旧卡原文保留，用纠正标记说明
            simulation = True
            scope = "仅模拟"
            materials_allowed = "仅模拟（开发自测·合成/假设测试输入，不引入真实S/P证据）"
        else:
            materials_allowed = "仅模拟（合成/假设测试输入）" if simulation else "全部 S01—S09 / P01—P09"
        note = ("重新打开历史任务 %s（开发自测·仅模拟，历史非模拟标注有误，已纠正）" % task_id) if dev_self_test \
            else "重新打开历史任务 %s" % task_id
        act = {
            "task_id": task_id,
            "created_at": created_at,
            "updated_at": _now(),
            "mode": mode,
            "scope": scope,
            "simulation": simulation,
            "dev_self_test": dev_self_test,
            "materials_allowed": materials_allowed,
            "save_fix_used": 0,
            "save_blocked": False,
            "initial_submit_done": False,
            "note": note,
        }
        _save_json(_ACTIVE_FILE, act)
        self.log_event("task_activate", {"task": task_id, "mode": mode, "scope": scope})
        return act

    # ---------- 用户输入记录（程序分配 ID 与时间；视为一轮分析开始） ----------
    def register_input(self, mode: str, raw_text: str, simulation: bool,
                       user_ref: str = "", unverified_links: str = "",
                       material_kind: str = "user_unverified") -> Dict[str, Any]:
        act = self._require_active()
        # 新一轮分析：重置保存修复状态（首次提交免费，允许最多 MAX_SAVE_FIX 次修复）
        act["save_fix_used"] = 0
        act["save_blocked"] = False
        act["initial_submit_done"] = False
        act["updated_at"] = _now()
        # 合成/假设测试输入：由程序锁定本轮为 仅模拟（模型不得覆盖）
        if bool(simulation) or material_kind in ("simulation",):
            act["simulation"] = True
            act["scope"] = "仅模拟"
            act["materials_allowed"] = "仅模拟（合成/假设测试输入）"
        _save_json(_ACTIVE_FILE, act)
        self.set_task_meta(act["task_id"], mode=act.get("mode") or mode,
                           scope=act["scope"], simulation=bool(act["simulation"]))
        d = _task_dir(act["task_id"])
        records = _load_json(os.path.join(d, "inputs.json"), [])
        # 同一任务内同一份材料的重发（跨模式/跨轮）不重复统计为多个独立来源
        norm = (raw_text or "").strip()
        for r in records:
            if (r.get("raw_text") or "").strip() == norm:
                # 仅复位保存修复状态，不新增独立输入
                r["duplicate"] = True
                r["re_registered_at"] = _now()
                return r
        existing = {r.get("input_id") for r in records if r.get("input_id")}
        n = 1
        while f"IN-{n:03d}" in existing:
            n += 1
        record = {
            "input_id": f"IN-{n:03d}",
            "task_id": act["task_id"],
            "mode": mode,
            "raw_text": raw_text,
            "user_ref": user_ref,
            "unverified_links": unverified_links,
            "material_kind": material_kind,
            "is_simulation": bool(simulation),
            "simulation": bool(simulation),
            "recorded_at": _now(),
        }
        records.append(record)
        _save_json(os.path.join(d, "inputs.json"), records)
        self.log_event("input", {"input_id": record["input_id"], "mode": mode,
                                 "is_simulation": record["is_simulation"]})
        return record

    def list_inputs(self) -> List[Dict[str, Any]]:
        act = self.get_active_task()
        if not act:
            return []
        return _load_json(os.path.join(_task_dir(act["task_id"]), "inputs.json"), [])

    # ---------- 来源排除（仅当前任务覆盖层；基础档案不改） ----------
    def _exclusions(self) -> Dict[str, str]:
        act = self._require_active()
        return _load_json(os.path.join(_task_dir(act["task_id"]), "exclusions.json"), {})

    def exclude_source(self, source_id: str, reason: str) -> Dict[str, Any]:
        act = self._require_active()
        exc = self._exclusions()
        exc[source_id] = reason
        _save_json(os.path.join(_task_dir(act["task_id"]), "exclusions.json"), exc)
        self.log_event("source_exclude", {"source_id": source_id, "reason": reason})
        return {"excluded": True, "source_id": source_id}

    def restore_source(self, source_id: str, reason: str = "") -> Dict[str, Any]:
        act = self._require_active()
        exc = self._exclusions()
        if source_id in exc:
            del exc[source_id]
            _save_json(os.path.join(_task_dir(act["task_id"]), "exclusions.json"), exc)
        self.log_event("source_restore", {"source_id": source_id, "reason": reason})
        return {"restored": True, "source_id": source_id}

    def excluded_source_ids(self) -> set:
        try:
            return set(self._exclusions().keys())
        except Exception:
            return set()

    def _derive_invalid(self, record_id: str) -> Optional[str]:
        """返回该记录是否因来源排除而在当前任务失效；None 表示仍有效。"""
        rid = (record_id or "").strip().upper()
        ex = self.excluded_source_ids()
        ms = get_material_store()
        if rid in ex:
            return f"来源 {rid} 已被当前任务排除"
        p = ms.get_product(rid)
        if p:
            srcs = [s for s in p.get("source_ids", []) if s]
            if srcs and all(s.upper() in ex for s in srcs):
                return f"产品 {rid} 全部依赖的来源已被排除（派生失效）"
        return None

    def _is_excluded(self, record_id: str) -> bool:
        return self._derive_invalid(record_id) is not None

    # ---------- 记录解析与依据快照（字段级、含排除） ----------
    def resolve_record(self, record_id: str, field_path: str = "") -> Optional[Dict[str, Any]]:
        """按 record_id 解析到真实档案/输入记录；校验字段路径；返回指定字段值。

        若来源被排除或产品派生失效，返回 validity="excluded", reason=...。
        """
        rid = (record_id or "").strip()
        ms = get_material_store()
        content = None
        kind = None
        loc = None
        validity = ""
        if sid := ms.get_source(rid):
            content, kind = sid, "source"
            loc = f"assets/materials/sources.json#{rid}"
        elif pid := ms.get_product(rid):
            content, kind = pid, "product"
            loc = f"assets/materials/products.json#{rid}"
        else:
            for rec in self.list_inputs():
                if rec.get("input_id") == rid:
                    content, kind = rec, "input"
                    act = self._require_active()
                    loc = f"assets/opportunity/{act['task_id']}/inputs.json#{rid}"
                    validity = "simulation" if rec.get("is_simulation") else "user_input"
                    break
        if content is None:
            return None
        # 排除/派生失效
        reason = self._derive_invalid(rid)
        rec_validity = validity
        if reason:
            rec_validity = "excluded"
        elif kind == "source":
            rec_validity = "valid_source"
        elif kind == "product":
            rec_validity = "valid_product"
        # 字段路径校验
        if field_path:
            if field_path not in ALLOWED_FIELDS[kind]:
                return {
                    "record_id": rid, "kind": kind, "content": content,
                    "field_value": None, "archive_locator": loc, "validity": rec_validity,
                    "reason": f"字段路径 {field_path!r} 不存在于 {kind} {rid}，请分别引用具体字段。",
                    "field_error": True,
                }
        fv = _field_value(content, kind, field_path)
        return {
            "record_id": rid, "kind": kind, "content": content, "field_value": fv,
            "field_path": field_path or DEFAULT_FIELD[kind], "archive_locator": loc,
            "validity": rec_validity, "reason": reason, "field_error": False,
        }

    def _builtin_valid_records(self) -> set:
        ms = get_material_store()
        return ms.known_source_ids() | ms.known_product_ids()

    def build_evidence_snapshot(self, evidence: List[Any]) -> List[Dict[str, Any]]:
        snap = []
        for ev in evidence or []:
            if not isinstance(ev, dict):
                continue
            rid = (ev.get("record_id") or ev.get("source_id") or ev.get("input_id") or "").strip()
            fp = (ev.get("field_path") or "").strip()
            resolved = self.resolve_record(rid, fp) if rid else None
            entry = dict(ev)
            entry["field_path"] = fp
            if resolved is None:
                entry["resolved_content"] = None
                entry["archive_locator"] = None
                entry["validity"] = "unresolved"
                entry["reason"] = "无法命中档案或输入记录"
            else:
                entry["field_path"] = fp or DEFAULT_FIELD.get(resolved["kind"], "")
                if resolved.get("field_error"):
                    entry["resolved_content"] = None
                    entry["archive_locator"] = resolved["archive_locator"]
                    entry["validity"] = "field_error"
                    entry["reason"] = resolved["reason"]
                else:
                    entry["resolved_content"] = resolved["field_value"]
                    entry["archive_locator"] = resolved["archive_locator"]
                    entry["validity"] = resolved["validity"]
                    entry["reason"] = resolved["reason"]
            snap.append(entry)
        return snap

    # ---------- 机会卡（程序分配 ID/版本，不覆盖历史） ----------
    def load_cards(self, task_id: str) -> List[Dict[str, Any]]:
        return _load_json(os.path.join(_task_dir(task_id), "cards.json"), [])

    def current_cards(self) -> List[Dict[str, Any]]:
        act = self.get_active_task()
        if not act:
            return []
        cards = self.load_cards(act["task_id"])
        latest: Dict[str, Any] = {}
        for c in cards:
            cid = c.get("card_id")
            if not cid:
                continue
            if cid not in latest or c.get("version", 0) > latest[cid].get("version", 0):
                latest[cid] = c
        return list(latest.values())

    def get_card_versions(self, card_id: str) -> List[Dict[str, Any]]:
        act = self.get_active_task()
        if not act:
            return []
        return [c for c in self.load_cards(act["task_id"]) if c.get("card_id") == card_id]

    def _validate_evidence(self, ev: Any, probs: List[str]) -> None:
        if not isinstance(ev, dict):
            probs.append("support_evidence 条目必须是对象")
            return
        rid = (ev.get("record_id") or ev.get("source_id") or ev.get("input_id") or "").strip()
        if not rid:
            probs.append("support_evidence 缺少 record_id/source_id/input_id")
            return
        fp = (ev.get("field_path") or "").strip()
        resolved = self.resolve_record(rid, fp)
        if resolved is None:
            probs.append(f"支持证据引用记录 {rid} 无法命中档案或输入，禁止伪造成来源。")
            return
        if resolved.get("field_error"):
            probs.append(f"引用字段路径 {fp!r} 不合法：{resolved.get('reason')}")
            return
        if resolved.get("validity") == "excluded":
            probs.append(f"记录 {rid} 当前不可作为支持依据：{resolved.get('reason')}")
            return
        # 摘要/结论应绑定真实字段；若引用产品，须有至少一个有效未排除来源
        if resolved.get("kind") == "product" and resolved.get("validity") != "valid_product":
            probs.append(f"产品 {rid} 依赖的来源已被排除，不能作为当前支持依据。")

    def _validate(self, card: Dict[str, Any]) -> List[str]:
        probs = []
        status = card.get("status", "")
        if status not in ALLOWED_STATUS:
            probs.append(f"status 必须为 {ALLOWED_STATUS}，当前: {status!r}")
        scope = card.get("judgment_scope", "")
        if scope not in ALLOWED_SCOPE:
            probs.append(f"judgment_scope 必须为 {ALLOWED_SCOPE}，当前: {scope!r}")
        mode = card.get("mode", "")
        if mode not in MODES:
            probs.append(f"mode 必须为 {MODES}，当前: {mode!r}")
        for f in ("original_hypothesis", "demand_hypothesis", "target_people_scene",
                  "keep_experience", "improve_experience",
                  "three_conditions", "key_unknowns", "next_step_verification", "change_summary"):
            if card.get(f) in (None, "", [], {}):
                probs.append(f"缺少必填字段: {f}")
        # counter_evidence / comparison 缺证时允许为空（候选机会（待验证）/暂不形成机会），建议验证等状态在下方按状态强制非空
        # proposed_direction 在缺证/线索/已有产品匹配时可留空，不强制必填
        support = card.get("support_evidence")
        if not isinstance(support, list):
            probs.append("support_evidence 必须是数组（缺证状态可为空数组）")
        else:
            if status in REQUIRE_SUPPORT_STATUS and not support:
                probs.append(f"status={status} 需要至少一条支持证据；缺证/未充分验证状态请使用 候选机会（待验证） 或 暂不形成机会。")
            for ev in support:
                self._validate_evidence(ev, probs)
        # 结构化三条件 + 成立程度守卫
        tc = card.get("three_conditions")
        if not isinstance(tc, dict):
            probs.append("three_conditions 必须为对象，包含 demand_support/supply_gap/verify_value")
        else:
            # 未知键必须拒绝并明示，不静默改名/忽略（如 supply_shortage/verification_value 一律拒绝）
            unknown3 = [k for k in tc.keys() if k not in THREE_KEYS]
            if unknown3:
                probs.append(f"three_conditions 含未知键 {unknown3}；程序 canonical 键为 {THREE_KEYS}，请改名后重试，程序不会静默替换。")
            for cond in THREE_KEYS:
                v = tc.get(cond)
                if not isinstance(v, dict):
                    probs.append(f"three_conditions.{cond} 必须为对象 {THREE_OBJ}，当前缺失或类型错误")
                    continue
                degree = v.get("degree", "")
                if degree not in DEGREE_STATUS:
                    probs.append(f"three_conditions.{cond}.degree 必须为 {DEGREE_STATUS}，当前 {degree!r}")
                unknown_o = [k for k in v.keys() if k not in THREE_OBJ]
                if unknown_o:
                    probs.append(f"three_conditions.{cond} 含未知键 {unknown_o}；应仅含 {THREE_OBJ}")
                basis = v.get("basis", [])
                if not isinstance(basis, list):
                    probs.append(f"three_conditions.{cond}.basis 必须是数组")
                # “未知/空依据不能支撑成立”：需求支持与供给缺口在依据为空时不得声称成立或初步支持
                if cond in ("demand_support", "supply_gap") and degree in ("已成立", "初步支持"):
                    if not basis:
                        probs.append(f"three_conditions.{cond} 无任何依据，不能声明为“{degree}”；未知/缺证时只能为 未支持 或 未知。")
                    else:
                        for b in basis:
                            if isinstance(b, dict):
                                rid = (b.get("record_id") or b.get("source_id") or b.get("input_id") or "").strip()
                                fp = (b.get("field_path") or "").strip()
                                r = self.resolve_record(rid, fp) if rid else None
                                if r is None or r.get("field_error"):
                                    probs.append(f"three_conditions.{cond}.basis 引用了无效记录/字段: {rid or '?'}")
        # 当前在售状态：缺省视为未知；0蔗糖标签/旧款发布不能自动推在售
        cs = card.get("current_in_sale")
        if cs is not None and cs not in IN_SALE_STATUS:
            probs.append(f"current_in_sale 必须为 {IN_SALE_STATUS}，当前 {cs!r}")
        # 逐产品对照：满足/不满足/未知 + 证据绑定（无体验/未查到只能记 未知）
        comp = card.get("comparison")
        not_met_products, unknown_products = [], []
        # 建议验证/已有产品匹配需要能支撑的产品对照；继续补证/暂缓或停止允许缺证留空
        if status in REQUIRE_SUPPORT_STATUS and not comp:
            probs.append(f"status={status} 需要非空产品对照（comparison）；零检索/缺证时请使用 候选机会（待验证） 或 暂不形成机会。")
        if not isinstance(comp, list):
            probs.append("comparison 必须为数组（逐产品记录 满足/不满足/未知，绑定需求条件、证据与适用范围）")
        else:
            for i, row in enumerate(comp):
                if not isinstance(row, dict):
                    probs.append(f"comparison[{i}] 必须是对象")
                    continue
                pid = row.get("product_id", "?")
                sat = row.get("satisfaction")
                if sat not in SATISFACTION_STATUS:
                    probs.append(f"comparison[{i}].product_id={pid} 必须含 satisfaction ∈ {SATISFACTION_STATUS}（无体验材料/未查到只能记 未知）")
                    continue
                if sat == "未知":
                    unknown_products.append(pid)
                elif not row.get("evidence"):
                    probs.append(f"comparison[{i}].product_id={pid} satisfaction={sat} 必须有 evidence；无体验材料/未查到结果只能记 未知")
                else:
                    if sat == "不满足":
                        not_met_products.append(pid)
        # 供给缺口与逐产品对照联动：单产品/未知不能推出“整个市场存在缺口”
        sg = (tc or {}).get("supply_gap") if isinstance(tc, dict) else None
        sgdeg = (sg or {}).get("degree", "") if isinstance(sg, dict) else ""
        if sgdeg in ("已成立", "初步支持") and not not_met_products:
            probs.append("supply_gap 声明为 “%s”，但没有任一被证实的“不满足”产品（comparison 需含 satisfaction=不满足 且带 evidence）；"
                         "未找到/全部未知只能保持 supply_gap=未知 并整体保持 候选机会（待验证）。" % sgdeg)
        if sgdeg == "已成立" and unknown_products:
            probs.append("supply_gap 声明为 “已成立”，但存在未知对照产品 %s；单凭部分产品不满足不能推出整个市场缺口成立，最多为 初步支持。" % unknown_products)
        return probs

    # ---------- 保存（程序分配ID/版本；修复上限程序控制） ----------
    def _validation_detail(self, card: Dict[str, Any], problems: Optional[List[str]] = None) -> Dict[str, Any]:
        """从失败卡片提取 required_fields / received_fields / problems 供错误精确定位。
        problems 逐条给出字段路径、预期类型或规则与实际失败原因（校验器产生），缺失时补足诊断。"""
        tc = card.get("three_conditions")
        three_present = [k for k in tc.keys()] if isinstance(tc, dict) else []
        # 区分“键缺失”与“键存在但值为空”，便于精确定位（空串/空数组/空对象不再与“缺失”混同）
        missing_keys = [f for f in REQUIRED_TOP if f not in card]
        empty_values = [f for f in REQUIRED_TOP if f in card and card.get(f) in (None, "", [], {})]
        top_missing = missing_keys + empty_values
        required_fields = [f"three_conditions.{k}" for k in THREE_KEYS if k not in three_present]
        required_fields += [f"{f}（顶层必填）" for f in top_missing]
        received_fields = {
            "three_conditions_keys": three_present,
            "three_conditions_canonical": THREE_KEYS,
            "three_conditions_obj_fields": THREE_OBJ,
            "degree_allowed": DEGREE_STATUS,
            "current_in_sale_allowed": IN_SALE_STATUS,
            "top_level_missing": top_missing,
            "missing_keys": missing_keys,
            "empty_values": empty_values,
            "received_top_level_keys": [k for k in card.keys()],
        }
        detail = {"required_fields": required_fields, "received_fields": received_fields}
        if problems is not None:
            # 具体失败原因逐条返回（字段路径 / 预期 / 实际），供定位且不静默收敛
            detail["problems"] = list(problems)
        return detail

    def _mark_save_failure(self):
        """记录一次校验失败；返回 (retry_remaining, allowed)。
        首轮首次提交失败不占用修复额度（remaining=MAX）；其后每次修复失败自减，
        用尽（MAX_SAVE_FIX 次修复失败）进入阻断并保留旧卡。"""
        act = self.get_active_task()
        if act.get("save_blocked"):
            return 0, False
        used = int(act.get("save_fix_used", 0))
        if not act.get("initial_submit_done"):
            # 首次提交失败：免费，不提前累计
            act["initial_submit_done"] = True
            act["save_fix_used"] = 0
            act["updated_at"] = _now()
            _save_json(_ACTIVE_FILE, act)
            return MAX_SAVE_FIX, True
        used += 1
        act["save_fix_used"] = used
        if used >= MAX_SAVE_FIX:
            act["save_blocked"] = True
            act["updated_at"] = _now()
            _save_json(_ACTIVE_FILE, act)
            return 0, False
        act["updated_at"] = _now()
        _save_json(_ACTIVE_FILE, act)
        return MAX_SAVE_FIX - used, True

    def _save_blocked(self) -> bool:
        return bool(self.get_active_task().get("save_blocked"))

    def _reset_save_state(self) -> None:
        act = self.get_active_task()
        if not act:
            return
        act["save_fix_used"] = 0
        act["save_blocked"] = False
        act["initial_submit_done"] = False
        act["updated_at"] = _now()
        _save_json(_ACTIVE_FILE, act)

    def save_card(self, analysis: Dict[str, Any]) -> Dict[str, Any]:
        act = self._require_active()
        task_id = act["task_id"]
        # 缺证时可保存：counter_evidence 缺失记“未检索到反证”（不伪造、不硬过校验）
        analysis = dict(analysis)
        # comparison：仅“缺省/None”才按既有缺证规则置为空数组；显式提供的错误类型（如字符串/对象）原样保留
        # 交给校验器报“comparison 必须为数组”，不再静默改写成 [] 而丢失对照。
        if "comparison" not in analysis or analysis.get("comparison") is None:
            analysis["comparison"] = []
        if analysis.get("counter_evidence") in (None, ""):
            analysis["counter_evidence"] = "当前未检索到反证/未提供（如实记录，不编造）"
        cards = self.load_cards(task_id)
        card_id = (analysis.get("card_id") or "").strip()
        # 同卡更新且 original_hypothesis 缺失/空时：仅允许从“明确 card_id 的已保存最新原卡”逐字继承该原始假设字段，
        # 并记录字段级继承来源；首次创建（无 card_id）必须明确提供；原卡无可用原值时继续按缺失校验并拒绝。
        # 程序不得为通过校验而代补/自动改动需求判断、状态、证据、产品对照等其他字段。
        original_inherited_from = None
        if card_id and not (analysis.get("original_hypothesis") or "").strip():
            _src_card = next((c for c in reversed(cards) if c.get("card_id") == card_id), None)
            if _src_card:
                _src = (_src_card.get("original_hypothesis") or "").strip()
                if _src:
                    analysis["original_hypothesis"] = _src
                    original_inherited_from = "%s v%s" % (card_id, _src_card.get("version"))
        # 本轮已因反复失败被阻断：任何保存（即便内容已修正）也须停止，直至新一轮开始
        if self._save_blocked():
            self.log_event("card_save_block", {
                "reason": "本轮保存修复已达上限并阻断，不再接受保存；保留旧卡，等待新一轮。",
                "retry_remaining": 0,
                "task_id": task_id, "target_card": card_id or None, "analysis": analysis,
            })
            raise ValueError("已达本轮保存修复上限：本轮结束，保留旧卡；禁止伪造来源或补证据硬过校验，请按现有材料说明或开始新一轮。\nretry_remaining=0")
        probs = self._validate(analysis)
        if probs:
            # 失败诊断：记录结构化实际入参 + problems + 目标任务/卡（仅本业务载荷，不含密钥令牌），供可追溯定位
            detail = self._validation_detail(analysis, probs)
            detail["task_id"] = task_id
            detail["target_card"] = card_id or None
            detail["analysis"] = analysis
            retry, allowed = self._mark_save_failure()
            if not allowed:
                self.log_event("card_save_block", {
                    "reason": "修复提交仍校验失败，达到修复上限，本轮结束；保留旧卡。",
                    "retry_remaining": 0, **detail,
                })
                raise ValueError("已达本轮保存修复上限：本轮结束，保留旧卡；禁止伪造来源或补证据硬过校验。\n"
                                 "retry_remaining=0\n"
                                 f"required_fields={json.dumps(detail['required_fields'], ensure_ascii=False)}\n"
                                 f"received_fields={json.dumps(detail['received_fields'], ensure_ascii=False)}\n"
                                 f"问题：\n" + "\n".join(probs))
            self.log_event("card_save_fail", {**detail, "retry_remaining": retry, "fatal": False})
            raise ValueError(
                "机会卡校验失败（本轮剩余修复次数 retry_remaining={retry}，最多再修 {n} 次，之后结束保留旧卡）:\n"
                "required_fields={req}\n"
                "received_fields={recv}\n"
                "问题：\n{probs}".format(
                    retry=retry, n=retry,
                    req=json.dumps(detail["required_fields"], ensure_ascii=False),
                    recv=json.dumps(detail["received_fields"], ensure_ascii=False),
                    probs="\n".join(probs)))

        if card_id:
            if card_id not in {c.get("card_id") for c in cards}:
                if not self._mark_save_failure()[1]:
                    raise ValueError("已达本轮保存修复上限：结束本次保存，保留旧卡。\nretry_remaining=0")
                self.log_event("card_save_fail", {"problems": [f"找不到原卡 {card_id}"], "fatal": False,
                                                  "task_id": task_id, "target_card": card_id, "analysis": analysis})
                raise ValueError(f"机会卡校验失败：找不到要更新的原卡 {card_id}，无法绑定原卡版本（本轮仍可修复）。")
        else:
            existing = {c.get("card_id") for c in cards if c.get("card_id")}
            n = 1
            while f"OC-{n:03d}" in existing:
                n += 1
            card_id = f"OC-{n:03d}"

        existing_versions = [c for c in cards if c.get("card_id") == card_id]
        version = max((c.get("version", 0) for c in existing_versions), default=0) + 1
        change_summary = analysis.get("change_summary", "")

        card = dict(analysis)
        card["card_id"] = card_id
        card["version"] = version
        card["task_id"] = task_id
        card["task_scope"] = act.get("scope")
        card["simulation"] = bool(act.get("simulation"))
        card["verification_date"] = VERIFICATION_DATE
        card["packaged_date"] = PACKAGE_DATE
        # 模拟标记由程序锁定：本任务为假设测试/合成分支(simulation)时，强制 仅模拟，模型不得覆盖
        if act.get("simulation"):
            card["judgment_scope"] = "仅模拟"
        else:
            card["judgment_scope"] = card.get("judgment_scope") or "非模拟资料"
        card.setdefault("current_in_sale", "未知")
        card["evidence_snapshot"] = self.build_evidence_snapshot(card.get("support_evidence", []))
        card["change_log"] = list(card.get("change_log", [])) + [{
            "version": version, "time": _now(),
            "change_type": "create" if version == 1 else "update",
            "reason": change_summary or ("新建机会卡" if version == 1 else "材料更新"),
        }]
        if original_inherited_from:
            # 字段级继承来源：本轮 original_hypothesis 由程序从明确原卡逐字继承而来，供可追溯
            card["original_hypothesis_inherited_from"] = original_inherited_from
            card["change_log"][-1]["inherited_fields"] = {"original_hypothesis": original_inherited_from}
        card["created_at"] = card.get("created_at") or _now()
        card["updated_at"] = _now()
        card["host_meta"] = {
            "run_id": f"RL-{len(self.list_runlog()) + 1:03d}",
            "model_config": _read_model_config(),
            "prompt_version": PROMPT_VERSION,
            "assigned_by": "program",
        }
        cards.append(card)
        _save_json(os.path.join(_task_dir(task_id), "cards.json"), cards)
        self.log_event("card_save", {
            "card_id": card_id, "version": version, "status": card.get("status"),
            "judgment_scope": card.get("judgment_scope"), "mode": card.get("mode"),
            "result": "saved",
        })
        self._reset_save_state()
        return card

    def exclude_from_card(self, card_id: str, source_id: str, reason: str) -> Dict[str, Any]:
        """撤回某来源对该卡的支持并生成新版本（假设测试分支）。

        来源排除、模拟锁定、新版本三者原子保存：任一失败则回滚本轮新增的排除，
        旧版本与其他排除覆盖保持不变；模拟锁定（本任务转 仅模拟）随假设测试发生而保留。
        """
        act = self._require_active()
        prior = self._exclusions()
        was_excluded = source_id in prior
        # 1) 写任务级排除（供校验判定“已排除”），并锁定本任务为 仅模拟（假设测试分支）
        self.exclude_source(source_id, reason)
        act = self.get_active_task()
        act["simulation"] = True
        act["scope"] = "仅模拟"
        act["materials_allowed"] = "仅模拟（假设排除/合成分支）"
        act["save_blocked"] = False
        act["save_fix_used"] = 0
        act["initial_submit_done"] = False
        act["updated_at"] = _now()
        _save_json(_ACTIVE_FILE, act)
        try:
            latest = self._build_excluded_version(card_id, source_id, reason)
            return self.save_card(latest)
        except Exception:
            if not was_excluded:   # 仅回滚本轮新增的排除；模拟锁定保留（已发生假设测试）
                exc = self._exclusions()
                exc.pop(source_id, None)
                _save_json(os.path.join(_task_dir(act["task_id"]), "exclusions.json"), exc)
            raise

    def _build_excluded_version(self, card_id: str, source_id: str, reason: str) -> Dict[str, Any]:
        """基于最新版本生成排除后的新版本内容（不落盘，交给 save_card 原子保存）。"""
        latest = None
        for c in self.current_cards():
            if c.get("card_id") == card_id:
                latest = dict(c)
                break
        if latest is None:
            raise ValueError(f"未找到机会卡 {card_id}。")

        def _still_support(ev: Any) -> bool:
            if not isinstance(ev, dict):
                return False
            rid = (ev.get("record_id") or ev.get("source_id") or ev.get("input_id") or "").strip()
            return rid and not self._is_excluded(rid)

        kept = [ev for ev in (latest.get("support_evidence") or []) if _still_support(ev)]
        latest["support_evidence"] = kept
        # 缺证降级：原为 建议验证/已有产品匹配 且支持被剔除为空 → 候选机会（待验证）
        status = latest.get("status", "")
        if status in ("建议验证", "已有产品匹配") and not kept:
            latest["status"] = "候选机会（待验证）"
        # 三条件中依赖被排除来源的依据剔除；剔除后缺依据则成立程度降为 未知（不会“需求不存在”）
        tc = latest.get("three_conditions")
        if isinstance(tc, dict):
            for cond in ("demand_support", "supply_gap"):
                v = tc.get(cond)
                if isinstance(v, dict):
                    basis = v.get("basis") or []
                    keep_b = []
                    for b in basis:
                        if isinstance(b, dict):
                            rid = (b.get("record_id") or b.get("source_id") or b.get("input_id") or "").strip()
                            if rid and not self._is_excluded(rid):
                                keep_b.append(b)
                    v["basis"] = keep_b
                    if not keep_b and v.get("degree") in ("已成立", "初步支持"):
                        v["degree"] = "未知"
        # 逐产品对照规范化：补齐 satisfaction/evidence/demand_condition，标记派生失效
        comp = latest.get("comparison") or []
        norm = []
        if isinstance(comp, list):
            for row in comp:
                if not isinstance(row, dict):
                    norm.append(row)
                    continue
                row = dict(row)
                if not row.get("satisfaction"):
                    if row.get("known_lack"):
                        row["satisfaction"] = "不满足"
                    elif row.get("known_agreement"):
                        row["satisfaction"] = "满足"
                    else:
                        row["satisfaction"] = "未知"
                if not row.get("evidence"):
                    srcs = row.get("source_id") or []
                    if isinstance(srcs, str):
                        srcs = [srcs]
                    row["evidence"] = [{"record_id": s, "field_path": "summary"} for s in srcs if s]
                if not row.get("demand_condition"):
                    row["demand_condition"] = latest.get("demand_hypothesis") or ""
                if self._is_excluded(row.get("product_id", "")):
                    row["_derived_invalid_after_exclusion"] = True
                norm.append(row)
        latest["comparison"] = norm
        latest["change_summary"] = (
            f"来源排除 {source_id}: {reason}（沿派生链生效，旧版本保留仅供审计；"
            f"当前无有效支持 → 该需求假设目前无法支持，不等于需求不存在；本版本按假设测试锁定为 仅模拟）")
        latest["_exclude_source"] = source_id
        latest["_exclude_reason"] = reason
        return latest

    # ---------- 执行日志（真实调用处采集，程序写入） ----------
    def log_event(self, kind: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        act = self.get_active_task()
        task_id = act["task_id"] if act else "_no_task"
        d = _task_dir(task_id)
        logs = _load_json(os.path.join(d, "runlog.json"), [])
        entry = {
            "id": f"RL-{len(logs) + 1:03d}",
            "time": _now(),
            "kind": kind,
            "prompt_version": PROMPT_VERSION,
            "model_config": _read_model_config(),
            "payload": payload,
        }
        logs.append(entry)
        _save_json(os.path.join(d, "runlog.json"), logs)
        return entry

    def list_runlog(self) -> List[Dict[str, Any]]:
        act = self.get_active_task()
        if not act:
            return []
        return _load_json(os.path.join(_task_dir(act["task_id"]), "runlog.json"), [])

    def _derive_from_runlog(self, task_id: str):
        """从该任务自身的运行日志恢复可靠元数据（不编造）：
        创建时间取 task_start 原始记录；模式取最新一条有效 mode_switch；范围为 task_start 记录。"""
        logs = _load_json(os.path.join(_task_dir(task_id), "runlog.json"), [])
        created = None
        mode = ""
        scope = ""
        if not logs:
            return created, mode, scope
        for e in logs:
            p = e.get("payload") or {}
            k = e.get("kind") or ""
            if k == "task_start":
                if e.get("time"):
                    created = e["time"]
                if p.get("mode") in MODES:
                    mode = p["mode"]
                if p.get("scope"):
                    scope = p["scope"]
            elif k == "mode_switch":
                if p.get("to") in MODES:
                    mode = p["to"]
        return created, mode, scope

    def record_retrieval(self, query: str = "", source_id: str = "", product_id: str = "",
                         hit_ids: List[str] = [], provided_snapshot: Optional[str] = None,
                         queries: Optional[List[str]] = None) -> Dict[str, Any]:
        payload = {
            "query": query, "source_id": source_id, "product_id": product_id,
            "queries": queries or [], "query_count": len(queries or []),
            "hit_ids": hit_ids, "provided_snapshot": provided_snapshot,
        }
        return self.log_event("retrieval", payload)

    # ---------- 展示（与保存同源，由程序生成，避免模型另写冲突结论） ----------
    def render_display(self, card: Dict[str, Any]) -> str:
        from t018_presentation import is_review, build_review, render_review_text
        if is_review(card):
            input_path = os.path.join(os.environ.get("COZE_WORKSPACE_PATH", "/workspace/projects"), "assets", "opportunity", "T-018", "inputs.json")
            with open(input_path, encoding="utf-8") as f:
                review = build_review(card, json.load(f))
            return render_review_text(review) + "\n\n## 已保存卡片原始字段（未改写，含原始 degree）\n" + json.dumps(card, ensure_ascii=False, indent=2)
        L = []
        L.append(f"## 机会卡 {card.get('card_id')} v{card.get('version')}")
        L.append(f"- 工作模式：{_display_mode(card.get('mode'))}｜判断范围：{card.get('judgment_scope')}｜模拟：{card.get('simulation')}")
        L.append(f"- 状态：{card.get('status')}")
        if card.get('mode') == '热点捕手':
            # 热点捕手：逐产品展示上市/在售情况，使用已保存字段，不做统一判定
            L.append("- 上市／在售情况：按产品分别核对，详见产品对照")
            for _p in (card.get('comparison') or []):
                _rid = _p.get('product_id') or _p.get('material_ref') or '?'
                _lst = _p.get('listing_status') or '未知'
                _refs = "、".join(str(s) for s in (_p.get('source_refs') or [])) or str(_p.get('evidence') or '')
                L.append(f"  - {_rid}：{_lst}（来源：{_refs}）")
        else:
            L.append(f"- 当前在售状态：{card.get('current_in_sale', '未知')}")
        L.append(f"- 一句话需求假设：{card.get('demand_hypothesis')}")
        L.append(f"- 人群与场景：{json.dumps(card.get('target_people_scene', {}), ensure_ascii=False)}")
        L.append(f"- 保留体验：{json.dumps(card.get('keep_experience', []), ensure_ascii=False)}")
        L.append(f"- 待改善体验：{json.dumps(card.get('improve_experience', []), ensure_ascii=False)}")
        L.append(f"- 支持证据：{json.dumps(card.get('support_evidence', []), ensure_ascii=False)}")
        L.append(f"- 反对/未见证据：{json.dumps(card.get('counter_evidence', '当前未提供/未找到'), ensure_ascii=False)}")
        L.append(f"- 蒙牛/竞品/替代对照：{json.dumps(card.get('comparison', {}), ensure_ascii=False)}")
        L.append(f"- 三项条件：{json.dumps(card.get('three_conditions', {}), ensure_ascii=False)}")
        L.append(f"- 拟议方向（假设）：{card.get('proposed_direction', '')}")
        L.append(f"- 关键未知：{json.dumps(card.get('key_unknowns', []), ensure_ascii=False)}")
        L.append(f"- 下一步最小验证：{json.dumps(card.get('next_step_verification', {}), ensure_ascii=False)}")
        L.append(f"- 变更/缺口说明：{card.get('change_summary', '')}")
        return "\n".join(L)

    # ---------- 导出（程序直接完成，不调用模型） ----------
    def export_task_text(self) -> str:
        act = self.get_active_task()
        if not act:
            return "当前无任务，请先开始新任务。"
        task_id = act["task_id"]
        L = []
        L.append(f"# 蒙牛新品机会智能体｜任务导出｜task_id: {task_id}")
        L.append(f"任务判断范围(scope)：{act.get('scope')}　|　模拟标记：{act.get('simulation')}")
        L.append(f"资料核查日期：{VERIFICATION_DATE}　|　打包日期：{PACKAGE_DATE}　|　提示词版本：{PROMPT_VERSION}")
        meta = self.get_task_meta(task_id)
        if meta.get("dev_self_test"):
            L.append("")
            L.append(">> 【开发自测·仅模拟】本任务材料为开发自拟、无公开出处，不可作为真实市场证据。"
                     "历史卡原标注为“非模拟资料”，经核有误，统一在此纠正为：仅模拟／开发自测；"
                     "旧版本原文保留，未覆盖或删除。后续分析遵守模拟隔离规则。")
        cards = self.current_cards()
        L.append("")
        L.append("## 机会卡（当前版本）")
        if not cards:
            L.append("（当前无机会卡）")
        for c in cards:
            L.append("")
            L.append(self.render_display(c))
        L.append("")
        exc = self._exclusions() if act else {}
        L.append("## 排除/修订覆盖")
        L.append(json.dumps(exc, ensure_ascii=False) if exc else "（无）")
        L.append("")
        L.append("## 用户输入记录")
        ins = self.list_inputs()
        L.append(json.dumps(ins, ensure_ascii=False, indent=2) if ins else "（无）")
        L.append("")
        L.append("## 执行日志（真实调用处采集）")
        logs = self.list_runlog()
        L.append(json.dumps(logs, ensure_ascii=False, indent=2) if logs else "（无）")
        return "\n".join(L)


_store = OpportunityStore()


def get_opportunity_store() -> OpportunityStore:
    return _store