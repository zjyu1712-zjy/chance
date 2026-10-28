# -*- coding: utf-8 -*-
"""v1.2 工具层：由程序管理任务/输入/卡片 ID、版本、时间与执行日志。

说明：
  - 移除模型自报「add_runlog/clear_task/task_id 参数」：执行日志在真实调用处采集，
    “开始新任务”只切换当前任务、绝不删除旧任务。
  - 保存失败保留旧版并返回失败原因，不强制“继续补证”当成功；证据引用必须可解析，
    禁止模型用记忆补齐产品事实或伪造来源。
"""
import json
from typing import Any, Dict, List, Optional

from langchain.tools import tool

from storage.materials_store import get_material_store, VERIFICATION_DATE, PACKAGE_DATE
from storage.opportunity_store import get_opportunity_store

# 单个任务在一次保存流程中允许出现的连续 JSON 解析失败次数上限；达到即强终止，
# 防止因模型反复提交相同坏数据而无限重试直至 RecursionError（不提高 recursion_limit）。
MAX_PARSE_FAIL_BEFORE_STOP = 3


def _fmt(record: Dict[str, Any], label: str) -> Dict[str, Any]:
    return {"record_id": record.get("id") or record.get("record_id") or record.get("product_id"),
            "kind": label, "content": record}


def _ctx():
    # 本组工具均为本地只读/程序落盘，无需远程 ctx；保持链路友好。
    return None


# ---------- 资料概览 / 检索（程序只读，禁止模型记忆补齐） ----------
@tool
def import_overview() -> str:
    """查看基础资料导入概览：来源数、产品/替代记录数、资料核查日期（固定 2026-10-02）与打包日期。不含业务正文。"""
    ms = get_material_store()
    return json.dumps(ms.overview(), ensure_ascii=False)


@tool
def search_materials(query: str = "", source_id: str = "", product_id: str = "") -> str:
    """在基础资料中检索，可按来源ID/产品ID/关键词。返回命中记录及其完整来源字段；
    未命中必须返回“未找到”，不要用模型记忆补齐产品事实。检索条件与命中ID会在真实调用处记入执行日志。"""
    ms = get_material_store()
    store = get_opportunity_store()
    res = ms.search(query=query, source_id=source_id, product_id=product_id)
    hit_ids = []
    if isinstance(res, dict) and res.get("found"):
        hit_ids = [r.get("source_id") for r in res.get("sources_hit", []) if isinstance(r, dict)]
        hit_ids += [r.get("product_id") for r in res.get("products_hit", []) if isinstance(r, dict)]
    store.record_retrieval(query=query, source_id=source_id, product_id=product_id,
                           hit_ids=hit_ids, provided_snapshot=json.dumps(res, ensure_ascii=False))
    return json.dumps(res, ensure_ascii=False)


@tool
def batch_search(queries: str, attribution: str = "") -> str:
    """批量检索多个关键词（JSON 字符串数组），并支持按产品归属聚类（attribution: 蒙牛/竞品/替代）。
    尽量一次性用本工具补齐蒙牛自有、竞品与替代的对照，避免关键对照漏查、也减少反复调用。"""
    ms = get_material_store()
    store = get_opportunity_store()
    qs = []
    try:
        parsed = json.loads(queries)
        qs = [str(x) for x in parsed] if isinstance(parsed, list) else [str(parsed)]
    except Exception:
        qs = [queries]
    attribution = attribution.strip() or ""
    if attribution not in ("", "蒙牛", "竞品", "替代"):
        return json.dumps({"found": False, "error": f"attribution 只能是 蒙牛/竞品/替代，当前 {attribution!r}"}, ensure_ascii=False)
    res = ms.batch_search(queries=qs, attribution=attribution or None)
    hit_ids = []
    if res.get("found"):
        hit_ids = [r.get("source_id") for r in res.get("sources_hit", [])]
        hit_ids += [r.get("product_id") for r in res.get("products_hit", [])]
    store.record_retrieval(query="", queries=qs, hit_ids=hit_ids,
                           provided_snapshot=json.dumps(res, ensure_ascii=False))
    return json.dumps(res, ensure_ascii=False)


@tool
def view_source(source_id: str) -> str:
    """查看一条来源记录 S01—S09 的完整归档字段（摘要、性质、阅读深度、支持/不支持、URL、日期、关联产品）。"""
    ms = get_material_store()
    s = ms.get_source(source_id)
    if not s:
        return json.dumps({"found": False, "reason": f"未找到来源 {source_id}"}, ensure_ascii=False)
    return json.dumps(s, ensure_ascii=False)


@tool
def view_product(product_id: str) -> str:
    """查看一条产品/替代记录 P01—P09 的完整字段（规格/版本、来源、对比维度、未知项、模拟标记）。"""
    ms = get_material_store()
    p = ms.get_product(product_id)
    if not p:
        return json.dumps({"found": False, "reason": f"未找到产品/替代记录 {product_id}"}, ensure_ascii=False)
    return json.dumps(p, ensure_ascii=False)


# ---------- 任务管理（程序分配 ID，不删除旧任务） ----------
@tool
def start_new_task(mode: str = "", scope: str = "非模拟资料", simulation: bool = False, note: str = "") -> str:
    """开始一个新任务：程序分配新 task_id 并切换为当前任务。绝不删除旧任务/导出记录。
    scope 必须是 非模拟资料 或 仅模拟。模拟任务不引入 S/P 真实资料作为非模拟证据。"""
    store = get_opportunity_store()
    try:
        act = store.start_new_task(mode=mode, scope=scope, simulation=simulation, note=note)
        ms = get_material_store()
        return json.dumps({"started": True, "task": act, "overview": ms.overview()}, ensure_ascii=False)
    except ValueError as e:
        return json.dumps({"started": False, "error": str(e)}, ensure_ascii=False)


@tool
def current_task() -> str:
    """查看当前任务的 task_id、判断范围、模拟标记与资料范围；无任务时提示先「开始新任务」。"""
    store = get_opportunity_store()
    act = store.get_active_task()
    if not act:
        return json.dumps({"has_task": False, "error": "尚无当前任务，请先调用 start_new_task。"}, ensure_ascii=False)
    ms = get_material_store()
    cards = len(store.current_cards())
    return json.dumps({"has_task": True, "task": act, "current_cards": cards,
                       "verification_date": VERIFICATION_DATE, "packaged_date": PACKAGE_DATE}, ensure_ascii=False)


@tool
def register_input(mode: str, raw_text: str, simulation: bool = False,
                   material_kind: str = "user_unverified") -> str:
    """登记本轮用户提供的材料为输入记录。程序分配 input_id(IN-xxx) 与时间。
    material_kind: 真实来源(historical_archive)/用户未核验陈述(user_unverified)/企业想法(enterprise_idea)/合成测试(simulation)。
    新链接若未实际打开，只能记为“用户提供、未核验”，不得声称已读网页。"""
    store = get_opportunity_store()
    try:
        rec = store.register_input(mode=mode, raw_text=raw_text, simulation=simulation,
                                   material_kind=material_kind)
        return json.dumps({"saved": True, "input_id": rec["input_id"], "record": rec}, ensure_ascii=False)
    except ValueError as e:
        return json.dumps({"saved": False, "error": str(e)}, ensure_ascii=False)


# ---------- 机会卡（程序分配 ID/版本/时间） ----------
@tool
def save_opportunity_card(analysis_json: str) -> str:
    """保存或新建机会卡。必须传入 JSON 字符串。字段/类型/枚举如下（仅结构空模板，不含业务结论；字段属性的 canonical 名称以程序为准，模型不得改名）：

    {
      "mode": "(enum: 消费者捕手/热点捕手/市场缺口捕手/企业灵感捕手)",
      "judgment_scope": "(enum: 非模拟资料/仅模拟)",
      "status": "(enum: 候选机会（待验证）/建议验证/已有产品匹配/暂不形成机会)",
      "original_hypothesis": "string",
      "demand_hypothesis": "string",
      "target_people_scene": {"description": "string", "source_classification": "(enum: 原文明确/推断/未知)"},
      "keep_experience": ["string"],
      "improve_experience": ["string"],
      "support_evidence": [{"record_id": "string(Sxx/Pxx/INxxx)", "field_path": "叶子字段(如 summary|supports|not_supports|name|version_spec...)", "summary": "string", "limitation": "string"}],
      "counter_evidence": "string",
      "comparison": [{"product_id": "string", "satisfaction": "(enum: 满足/不满足/未知)", "demand_condition": "string(具体需求条件)", "evidence": "[{\"record_id\": \"string\", \"field_path\": \"string\"}] 或 string", "scope": "string(适用范围) 或省略"}],
      "current_in_sale": "(enum: 未知/在售/不在售/部分在售；缺省视为未知)",
      "three_conditions": {
        "demand_support": {"degree": "(enum: 已成立/初步支持/未支持/未知)", "basis": [{"record_id": "string", "field_path": "string"}], "note": "string"},
        "supply_gap": {"degree": "(enum同上)", "basis": [], "note": "string"},
        "verify_value": {"degree": "(enum同上)", "basis": [], "note": "string"}
      },
      "proposed_direction": "string(缺证/已有匹配时可为空) 或省略",
      "key_unknowns": ["string"],
      "next_step_verification": {"question": "string", "compare_object": "string", "observation": "string", "continue_adjust_stop": "string"},
      "change_summary": "string",
      "card_id": "仅更新同卡时必传【本轮更新基线】给出的最新原卡ID（必须等于该原卡ID，版本由程序自动递增并保留上一版）；首次创建（尚无原卡）省略"
    }

    关键约束：three_conditions 键名必须为 demand_support/supply_gap/verify_value（程序 canonical，会直接拒绝 supply_shortage/verification_value 等未知键并明示）；每个条目仅含 degree/basis/note。
    comparison 按产品逐行记录 satisfaction(满足/不满足/未知)：无体验材料或未查到结果只能记 未知；记录 满足/不满足 必须带 evidence。单个产品不满足不能推出“整个市场存在缺口”；supply_gap 声明 已成立/初步支持 必须有被证实的 不满足 产品行。模拟标记（仅模拟/非模拟）由程序按任务锁定并强制写入，模型填写的 judgment_scope 不得覆盖任务级 simulation。
    引用契约（硬性，违反即校验失败）：只可引用系统已登记 ID——S/P 来源记录、P 产品、或本轮输入的实际 input_id（如 IN-004）。输入正文中出现的 MNCASE_C01_R01 等内部别名【不是】系统登记 ID，不得用作 record_id，也不得新增为独立来源。引用本轮补充文本：统一用该输入实际 input_id（如 IN-004）+ field_path=raw_text（路径须真实存在于该记录，用前请核实），并在 summary 注明正文中的 MNCASE 标签及其同源关系；同源复核不新增消费者样本、不重复计为新证据。引用公共 S/P：只用其实际存在的叶子字段（如 summary），绝不拼接字段路径（如 summary|MNCASE_C01_R01），也不得把多个字段拼成一个 field_path。校验失败返回的 required_fields / received_fields / problems 及可用记录与叶子字段提示只用于修正引用，不得自动改写引用、伪造证据或虚构来源硬过校验。
    程序负责：分配/校验 card_id 与版本、时间、依据快照、变更记录、宿主元数据（模型配置/提示词版本）。
    保存失败或校验失败时保留输入与旧版，按本工具返回的错误（含 retry_remaining / required_fields / received_fields）修订后重试；以本工具返回为准，不得在返回失败后自称已修复或引用其他任务历史卡充当当前结果。"""
    store = get_opportunity_store()
    try:
        analysis = json.loads(analysis_json)
        if not isinstance(analysis, dict):
            raise ValueError("analysis_json 必须是 JSON 对象。")
    except Exception as e:
        # JSON 解析失败：保留原始入参供恢复 + 熔断防无限重试。
        # 1) 原文结构不入 token/密钥，存 analysis_raw 供人工/流程后续恢复（不覆盖任何历史）。
        # 2) 同一任务连续解析失败达到阈值即返回强终止信号，避免 agent 反复回到业务模型
        #    提交相同错误数据、直至触顶 RecursionError（不提高 recursion_limit）。
        raw = analysis_json if isinstance(analysis_json, str) else ""
        entry = store.log_event("card_save_parse_fail", {
            "error": f"无法解析分析内容 JSON: {e}",
            "analysis_raw": raw,
        })
        # 熔断判断：统计同一任务内最近连续 parse_fail 事件数（含本次）
        consecutive_parse_fail = 0
        try:
            for ev in reversed(store.list_runlog()):
                if ev.get("kind") == "card_save_parse_fail":
                    consecutive_parse_fail += 1
                else:
                    break
        except Exception:
            consecutive_parse_fail = 1
        stop_hint = ""
        if consecutive_parse_fail >= MAX_PARSE_FAIL_BEFORE_STOP:
            stop_hint = (f" [已连续 {consecutive_parse_fail} 次解析失败，达到上限"
                         f"{MAX_PARSE_FAIL_BEFORE_STOP}] 本轮保存已终止，请停止反复重试；"
                         f"原始入参已留存，待人工核对后再处理。")
        return json.dumps({"saved": False, "error": f"无法解析分析内容 JSON: {e}" + stop_hint,
                           "parse_fail_count": consecutive_parse_fail},
                          ensure_ascii=False)
    try:
        card = store.save_card(analysis)
        return json.dumps({
            "saved": True, "card_id": card["card_id"], "version": card["version"],
            "status": card.get("status"), "judgment_scope": card.get("judgment_scope"),
            "current_in_sale": card.get("current_in_sale", "未知"),
            "evidence_snapshot": card.get("evidence_snapshot"),
            "display": store.render_display(card),
        }, ensure_ascii=False)
    except ValueError as e:
        return json.dumps({"saved": False, "error": str(e),
                           "hint": "本轮保存失败：保留输入与旧版不提交新版本，请按错误修订后重试或说明失败。"}, ensure_ascii=False)


@tool
def view_cards(card_id: str = "") -> str:
    """查看机会卡：未指定 card_id 时列出当前任务全部卡（状态/结论/下一步），
    否则返回该卡全部版本的变更记录与逐字段依据快照（可展开）。"""
    store = get_opportunity_store()
    if card_id:
        cards = store.get_card_versions(card_id)
        if not cards:
            return json.dumps({"found": False, "reason": f"当前任务无卡片 {card_id}"}, ensure_ascii=False)
        return json.dumps({"found": True, "card_id": card_id, "versions": cards}, ensure_ascii=False)
    cards = store.current_cards()
    summary = [{"card_id": c.get("card_id"), "version": c.get("version"), "mode": c.get("mode"),
                "status": c.get("status"), "judgment_scope": c.get("judgment_scope"),
                "simulation": bool(c.get("simulation")),
                "模拟标记": "仅模拟（演示数据）" if c.get("simulation") else ("非模拟" if c.get("judgment_scope") == "非模拟资料" else "模拟"),
                "conclusion": c.get("change_summary") or c.get("demand_hypothesis")}
               for c in cards]
    return json.dumps({"found": bool(summary), "cards": summary}, ensure_ascii=False)


@tool
def exclude_source_from_card(card_id: str, source_id: str, reason: str) -> str:
    """撤回某来源对该机会卡的支持并生成新版本：程序剔除该来源、沿派生链生效、旧版本保留仅供审计。
    撤回后该来源不计入当前任务的有效支持；需恢复请用 restore_source。"""
    store = get_opportunity_store()
    try:
        card = store.exclude_from_card(card_id, source_id, reason)
        return json.dumps({"excluded": True, "card_id": card["card_id"], "new_version": card["version"],
                           "support_evidence": card.get("support_evidence")}, ensure_ascii=False)
    except ValueError as e:
        return json.dumps({"excluded": False, "error": str(e)}, ensure_ascii=False)


@tool
def restore_source(source_id: str, reason: str = "") -> str:
    """显式恢复某个被当前任务排除的来源（需用户明确指示），并记录执行日志。"""
    store = get_opportunity_store()
    store.restore_source(source_id, reason)
    return json.dumps({"restored": True, "source_id": source_id}, ensure_ascii=False)


# ---------- 导出 / 执行日志（程序直接完成，不调用模型） ----------
@tool
def export_task() -> str:
    """导出当前任务全部记录为可复制文本：任务信息、机会卡（当前+历史）、排除覆盖、输入记录、执行日志。
    供验收/存档使用；导出后提示可开始新任务。"""
    store = get_opportunity_store()
    return store.export_task_text()


@tool
def view_runlog() -> str:
    """查看当前任务执行日志（真实调用处采集）：每个检索的条件与命中ID、保存结果、时间、模型配置、提示词版本。
    不采信模型自报的成功；未取得字段标未知。"""
    store = get_opportunity_store()
    return json.dumps({"runlog": store.list_runlog()}, ensure_ascii=False)


ALL_TOOLS = [
    import_overview, search_materials, batch_search, view_source, view_product,
    start_new_task, current_task, register_input,
    save_opportunity_card, view_cards, exclude_source_from_card, restore_source,
    export_task, view_runlog,
]