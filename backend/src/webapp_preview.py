# -*- coding: utf-8 -*-
"""蒙牛新品机会智能体 —— 预览 Web UI 后端。
复用现有 OpportunityStore / MaterialStore 与 agent 图，保证
"界面 / 保存卡片 / 导出" 使用同一份已保存数据。
"""
import glob
from t018_presentation import build_review, exact_diff, render_review_text, read_reply_archive
import asyncio
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse
from langchain_core.messages import HumanMessage

logger = logging.getLogger(__name__)

VERIFICATION_DATE = "2026-10-02"

router = APIRouter(prefix="/preview", tags=["preview"])

_GRAPH_PROVIDER = None  # set by main.py lifespan
_RUN_LOCK = {"task_id": None}  # 分析运行锁：本轮期间锁定发起 task

def set_graph_provider(fn):
    global _GRAPH_PROVIDER
    _GRAPH_PROVIDER = fn


def _store():
    from storage.opportunity_store import get_opportunity_store
    return get_opportunity_store()


def _materials():
    from storage.materials_store import get_material_store
    return get_material_store()


def _task_dirs() -> List[str]:
    base = os.path.join(os.environ.get("COZE_WORKSPACE_PATH", "/workspace/projects"), "assets", "opportunity")
    if not os.path.isdir(base):
        return []
    return sorted([os.path.basename(p) for p in glob.glob(os.path.join(base, "*")) if os.path.isdir(p)])


def _status_for(field_present: bool, is_gap: bool = False) -> str:
    if field_present:
        return "已完成"
    if is_gap:
        return "缺资料"
    return "未开始"


def derive_stages(card: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """把一张机会卡拆成五个阶段的展示产物（同一份已保存数据）。"""
    if not card:
        return [
            {"stage": 1, "name": "形成候选假设", "status": "未开始", "content": None},
            {"stage": 2, "name": "核对需求", "status": "未开始", "content": None},
            {"stage": 3, "name": "核对现有产品", "status": "未开始", "content": None},
            {"stage": 4, "name": "判断验证价值", "status": "未开始", "content": None},
            {"stage": 5, "name": "给出行动决定", "status": "未开始", "content": None},
        ]

    s1 = {
        "既有事实": {
            "人群/场景": card.get("target_people_scene", {}).get("description") if isinstance(card.get("target_people_scene"), dict) else card.get("target_people_scene"),
            "人群/场景来源分类": card.get("target_people_scene", {}).get("source_classification") if isinstance(card.get("target_people_scene"), dict) else None,
            "希望保留": card.get("keep_experience"),
            "希望改善": card.get("improve_experience"),
        },
        "候选需求假设": card.get("demand_hypothesis"),
        "原始假设": card.get("original_hypothesis"),
    }
    s2_ev = card.get("support_evidence") or []
    _src_ids = sorted({ (e.get("record_id") if isinstance(e, dict) else str(e)) for e in s2_ev if e })
    s2 = {
        "支持证据": s2_ev,
        "反对证据": card.get("counter_evidence"),
        "覆盖范围": ("去重来源 " + str(len(_src_ids)) + " 个（" + "、".join(_src_ids) + "），含 " + str(len(s2_ev)) + " 条反馈依据；同一来源文件内的多名评价者按其原记录分别保留，不计为独立来源") if s2_ev else "未检索到支持证据（需求尚未获得充分支持）",
    }
    s3 = {
        "市场是否在当前在售": card.get("current_in_sale"),
        "产品对照": card.get("comparison") or [],
    }
    s4 = {
        "三条件判断": card.get("three_conditions"),
        "关键未知项": card.get("key_unknowns"),
    }
    s5 = {
        "当前决定": card.get("status"),
        "可能的验证方向/设计变量": card.get("proposed_direction"),
        "下一步验证": card.get("next_step_verification"),
        "变更说明": card.get("change_summary"),
    }
    stages = [
        {"stage": 1, "name": "形成候选假设", "status": _status_for(bool(card.get("demand_hypothesis"))), "content": s1},
        {"stage": 2, "name": "核对需求", "status": _status_for(bool(s2_ev), is_gap=True) if not s2_ev else "已完成", "content": s2},
        {"stage": 3, "name": "核对现有产品", "status": _status_for(bool(card.get("comparison")), is_gap=True) if not card.get("comparison") else "已完成", "content": s3},
        {"stage": 4, "name": "判断验证价值", "status": _status_for(bool(card.get("three_conditions"))), "content": s4},
        {"stage": 5, "name": "给出行动决定", "status": _status_for(bool(card.get("status"))), "content": s5},
    ]
    return stages


def _overview(detail: bool = True) -> Dict[str, Any]:
    store = _store()
    mat = _materials()
    act = store.get_active_task()
    task_id = act["task_id"] if act else None
    cards = store.current_cards() if task_id else []
    latest = cards[-1] if cards else None
    excls = {}
    if task_id:
        try:
            exc_path = os.path.join(
                os.environ.get("COZE_WORKSPACE_PATH", "/workspace/projects"),
                "assets", "opportunity", task_id, "exclusions.json")
            if os.path.isfile(exc_path):
                with open(exc_path, "r", encoding="utf-8") as f:
                    excls = json.load(f)
        except Exception:  # noqa: BLE001
            excls = {}
    history_ids = _task_dirs()
    # 历史列表按每个任务自身 task_id 读取其实际卡片/版本与元数据，不受当前活动任务影响
    history_tasks = {}
    for tid in history_ids:
        entry = dict(store.get_task_meta(tid))
        try:
            entry["cards"] = store.load_cards(tid)
        except Exception as _e:  # noqa: BLE001
            entry["cards"] = []
            entry["read_error"] = "读取失败"
        # 老空任务缺 mode/created_at（独立于机会卡的信息）：先由卡片兜底，再依据该任务自身 runlog 既有可靠记录恢复，不编造、不取当前时间
        if not entry.get("mode") and entry.get("cards"):
            _c = entry["cards"][-1].get("mode")
            if _c:
                entry["mode"] = _c
        if not entry.get("created_at") and entry.get("cards"):
            _c = entry["cards"][0].get("created_at") or entry["cards"][0].get("updated_at")
            if _c:
                entry["created_at"] = _c
        if (not entry.get("mode")) or (not entry.get("created_at")):
            _rl_c, _rl_m, _rl_s = store._derive_from_runlog(tid)  # noqa: SLF001 (同 store 内部逻辑)
            if not entry.get("mode") and _rl_m:
                entry["mode"] = _rl_m
            if not entry.get("created_at") and _rl_c:
                entry["created_at"] = _rl_c
            if not entry.get("scope") and _rl_s:
                entry["scope"] = _rl_s
        history_tasks[tid] = entry
    return {
        "active_task": act,
        "task_id": task_id,
        "inputs": store.list_inputs() if task_id else [],
        "cards": cards,
        "latest_card": latest,
        "t018_review": build_review(latest, store.list_inputs()) if task_id == "T-018" else None,
        "stages": derive_stages(latest),
        "exclusions": excls,
        "task_meta": store.get_task_meta(task_id) if task_id else {},
        "history_task_ids": history_ids,
        "history_tasks": history_tasks,
        "materials": {"sources": mat.list_sources(), "products": mat.list_products()},
        "verification_date": VERIFICATION_DATE,
    }


@router.get("/ui")
async def ui_index():
    base = os.path.join(os.environ.get("COZE_WORKSPACE_PATH", "/workspace/projects"), "assets", "web", "index.html")
    return FileResponse(base, media_type="text/html")


@router.get("/api/state")
async def api_state():
    return _overview()


@router.post("/api/start")
async def api_start(payload: Dict[str, Any]):
    store = _store()
    if _RUN_LOCK.get("task_id"):
        raise HTTPException(status_code=409, detail=f"分析进行中（任务 {_RUN_LOCK['task_id']}），期间禁止开始新任务")
    mode = payload.get("mode", "")
    scope = payload.get("scope", "非模拟资料")
    note = payload.get("note", "")
    sim = bool(payload.get("simulation", False))
    if sim:
        scope = "仅模拟"
    try:
        act = store.start_new_task(mode=mode, scope=scope, simulation=sim, note=note)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _overview()


@router.post("/api/input")
async def api_input(payload: Dict[str, Any]):
    store = _store()
    act = store.get_active_task()
    if not act:
        raise HTTPException(status_code=400, detail="请先选择入口并开始新任务")
    raw = (payload.get("raw_text") or "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="原始输入不能为空")
    rec = store.register_input(
        mode=act.get("mode", ""),
        raw_text=raw,
        simulation=bool(payload.get("simulation", act.get("simulation", False))),
        user_ref=payload.get("user_ref", ""),
        unverified_links=payload.get("unverified_links", ""),
        material_kind=payload.get("material_kind", "user_unverified"),
    )
    # 若为模拟测试输入，程序已锁定本轮为 仅模拟
    act = store.get_active_task()
    logger.info("input registered: %s task=%s simulation=%s", rec.get("input_id"), act.get("task_id"), act.get("simulation"))
    return _overview()


def _is_new_input(it: Dict[str, Any], cards: List[Dict[str, Any]]) -> bool:
    if not cards:
        return True
    last_ts = max([(c.get("updated_at") or c.get("created_at") or "") for c in cards] or [""])
    return (it.get("recorded_at") or "") > last_ts


def _build_analyze_prompt(act: Dict[str, Any], inputs: List[Dict[str, Any]],
                          cards: List[Dict[str, Any]]) -> str:
    mode = act.get("mode", "")
    scope = act.get("scope", "非模拟资料")
    task_id = act.get("task_id")
    lines = [
        f"当前任务信息：任务ID={task_id}；入口模式={mode}；判断范围={scope}；核验日期={VERIFICATION_DATE}。",
    ]
    if cards:
        lines.append("本任务已保存的机会卡（历史版本必须保留；本轮若为补充请更新相应卡，不得覆盖或删除旧版）：")
        for c in cards:
            ev_ids = [e.get("record_id") for e in (c.get("support_evidence") or [])]
            lines.append(
                f"- {c.get('card_id')} v{c.get('version')}｜{c.get('status')}｜需求假设={c.get('demand_hypothesis')}｜"
                f"支持证据={ev_ids}｜反证={c.get('counter_evidence')}｜"
                f"下一步验证={c.get('next_step_verification')}｜变更说明={c.get('change_summary')}")
        # 更新基线：明确给出最新原卡的完整结构，供多轮更新逐字沿用未变更字段；首次尚未建卡时无此段落
        _latest = cards[-1]
        lines.append(f"【本轮更新基线】最新原卡 = {_latest.get('card_id')} v{_latest.get('version')}，"
                     f"其完整结构如下（更新时以此为基线：card_id 必须等于此 ID，仅按本轮证据增改应改字段，"
                     f"未变更字段逐字保留，程序自动生成 v+1 并保留上一版本）：")
        lines.append(json.dumps(_latest, ensure_ascii=False))
    lines.append("以下输入已由程序登记（请勿调用 start_new_task 或 register_input）：")
    for it in inputs:
        if it.get("duplicate"):
            tag = "（与既有多轮输入重复，已去重，不作为新来源）"
        elif _is_new_input(it, cards):
            tag = "（【本轮新增】据此评估是否需要更新已有结论）"
        else:
            tag = "（历史轮次输入，作为既有证据一并参考）"
        lines.append(f"- {it.get('input_id')}（{it.get('material_kind')}，is_simulation={it.get('is_simulation')}）{tag}：{it.get('raw_text')}")
    lines.append("")
    lines.append("请基于以上输入严格执行五阶段分析，并在最后调用 save_opportunity_card 保存机会卡（这是本轮的最终交付物）：")
    lines.append("【多轮更新规则】")
    lines.append("- 若本轮存在【本轮新增】输入且本任务已有机会卡：更新目标必须等于【本轮更新基线】给出的最新原卡 card_id（不是新建、不是其他卡）；版本自动递增为 v+1 并保留上一版本；用 change_summary 说明相对上一版改了什么。")
    lines.append("- 是否改变结论由证据决定：有新证据支持才更新业务状态；无新证据支持时不强制改变业务状态（保留原 status）。")
    lines.append("- 仅当出现一个与已有卡不同的独立新需求假设时，才创建新的机会卡（关联原卡），并保留原卡。")
    lines.append("- 每一版都要重新核对：需求假设、证据、产品对照、业务状态、下一步验证建议。")
    lines.append("【证据与引用契约】（硬性引用规则，违反即校验失败）")
    lines.append("- 只可引用系统已登记 ID：S/P 来源记录、P 产品记录、或本轮输入的实际 input_id（如 IN-004）。输入正文中出现的 MNCASE_C01_R01 等内部别名【不是】系统登记 ID，不得用作 record_id，也不得把它们新增为独立来源。")
    lines.append("- 引用本轮补充文本：统一用该输入的实际 input_id（如 IN-004）+ field_path=raw_text（路径须真实存在于该记录，用前请核实），并在 summary 注明正文中的 MNCASE 标签及其同源关系；同源复核内容不新增消费者样本、不重复计为新证据。")
    lines.append("- 引用公共 S/P：只用其实际存在的叶子字段（如 summary）；绝不拼接字段路径（如 summary|MNCASE_C01_R01），也不得把多个字段拼成一个 field_path。")
    lines.append("- save_opportunity_card 校验失败时，会返回可用记录与叶子字段提示：请按提示修正引用（改用正确的 input_id/S-P ID 与真实 field_path），不得自动改写引用、不得伪造证据、不得虚构来源硬过校验。")
    lines.append("阶段1 形成候选假设：提出值得核查的候选假设（什么人/场景/希望/限制），注明原文明示 vs AI推测；信息不足允许只保存线索。")
    lines.append("阶段2 核对需求：用 batch_search 检索材料（覆盖 蒙牛/竞品/替代），列出支持/反对证据表；区分 消费者体验/品牌介绍/研究报告/用户未核验陈述/企业构想/模拟材料；找不到证据必须写\"需求尚未获得充分支持\"。")
    lines.append("阶段3 核对现有产品：逐产品 满足/不满足/未知，绑定需求条件/证据/适用范围；未查到只能记 未知；单个产品不满足不等于整个市场缺口；历史产品结论不得套用到当前在售版本。")
    lines.append("阶段4 判断验证价值：重要性/现有满足/蒙牛价值/关键不确定性/成本可控验证；无依据不要编造销量规模或精确评分。")
    lines.append("阶段5 给出行动决定：A候选机会（待验证） / B建议验证 / C已有产品匹配 / D暂不形成机会；缺资料不要强行\"建议验证\"。")
    lines.append("严禁编造市场数据、消费者反馈或模型运行记录；缺资料如实写\"需求尚未获得充分支持\"与关键未知。")
    lines.append("strictly区分原文明示与AI推测。保存机会卡后再结束。")
    return "\n".join(lines)


@router.post("/api/analyze")
async def api_analyze(payload: Dict[str, Any]):
    store = _store()
    act = store.get_active_task()
    if not act:
        raise HTTPException(status_code=400, detail="请先选择入口并开始新任务")
    inputs = store.list_inputs()
    if not inputs:
        raise HTTPException(status_code=400, detail="请先登记原始输入")
    # 并发保护：本轮期间锁定发起 task，阻止切换/启动/激活/重复分析
    if _RUN_LOCK.get("task_id"):
        raise HTTPException(status_code=409,
                            detail=f"已有分析进行中（任务 {_RUN_LOCK['task_id']}），请等待完成后再操作")
    task_id = act["task_id"]
    _RUN_LOCK["task_id"] = task_id
    saved_new = False
    try:
        # 本轮分析的环境：既有机会卡（供多轮更新判断）与保存修复状态复位
        cards = store.current_cards()
        before = {(c.get("card_id"), c.get("version")) for c in cards}
        store._reset_save_state()
        graph = (_GRAPH_PROVIDER() if _GRAPH_PROVIDER else None)
        if graph is None:
            try:
                from coze_coding_utils.helper import graph_helper
                graph = graph_helper.get_agent_instance("agents.agent", None)
            except Exception as e:  # noqa: BLE001
                raise HTTPException(status_code=500, detail=f"无法构建分析图：{e}")
        prompt = _build_analyze_prompt(act, inputs, cards)
        thread = f"ui-{task_id}-{uuid.uuid4().hex[:6]}"
        cfg = {"configurable": {"thread_id": thread}}
        asyncio.get_running_loop()  # 确保在事件循环内
        out = await graph.ainvoke({"messages": [HumanMessage(content=prompt)]}, config=cfg)

        msgs = out.get("messages", [])
        last_ai = None
        for m in msgs:
            if getattr(m, "type", "") == "ai":
                c = getattr(m, "content", "")
                if isinstance(c, str):
                    last_ai = c
                elif isinstance(c, list):
                    txts = [b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text"]
                    if txts:
                        last_ai = "\n".join(txts)
        # 核验本轮保存回执 / 新版本：区分 模型运行 与 保存成功/失败
        act2 = store.get_active_task()
        after = store.current_cards() if (act2 and act2.get("task_id") == task_id) else []
        after_k = {(c.get("card_id"), c.get("version")) for c in after}
        saved_new = bool(after_k - before)
        run = {
            "task_id": task_id,
            "status": "ok" if saved_new else ("blocked" if bool(act2 and act2.get("save_blocked")) else "no_save"),
            "saved_new": saved_new,
            "blocked": bool(act2 and act2.get("save_blocked")),
            "new_cards": sorted({"%sv%d" % kv for kv in (after_k - before)}),
            "retry_remaining": (int(act2.get("save_fix_used", 0)) if act2 else 0),
            "last_ai_text": last_ai,
        }
        state = _overview()
        state["run"] = run
        state["last_ai_text"] = last_ai
        state["analyze_error"] = None
        return state
    except Exception as e:  # noqa: BLE001
        logger.error("analyze failed: %s", e, exc_info=True)
        state = _overview()
        if saved_new:
            state["run"] = {"task_id": task_id, "status": "ok", "saved_new": True, "blocked": False}
        else:
            state["run"] = {"task_id": task_id, "status": "error", "saved_new": False, "blocked": False}
        state["analyze_error"] = f"分析执行异常：{e}"
        state["last_ai_text"] = None
        return state
    finally:
        if _RUN_LOCK.get("task_id") == task_id:
            _RUN_LOCK["task_id"] = None


@router.post("/api/exclude")
async def api_exclude(payload: Dict[str, Any]):
    store = _store()
    card_id = payload.get("card_id")
    source_id = payload.get("source_id")
    reason = payload.get("reason", "预览界面假设排除")
    if not card_id or not source_id:
        raise HTTPException(status_code=400, detail="需要 card_id 与 source_id")
    try:
        store.exclude_from_card(card_id=card_id, source_id=source_id, reason=reason)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(e))
    return _overview()


@router.post("/api/restore")
async def api_restore(payload: Dict[str, Any]):
    store = _store()
    source_id = payload.get("source_id")
    if not source_id:
        raise HTTPException(status_code=400, detail="需要 source_id")
    if source_id == "__all__":
        try:
            exc_path = os.path.join(
                os.environ.get("COZE_WORKSPACE_PATH", "/workspace/projects"),
                "assets", "opportunity", store.get_active_task()["task_id"], "exclusions.json")
            if os.path.isfile(exc_path):
                with open(exc_path, "r", encoding="utf-8") as f:
                    cur = json.load(f)
                for sid in list(cur.keys()):
                    store.restore_source(source_id=sid, reason="预览恢复真实来源")
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(e))
    else:
        store.restore_source(source_id=source_id, reason=payload.get("reason", "恢复来源"))
    return _overview()


@router.get("/api/export")
async def api_export():
    store = _store()
    act = store.get_active_task()
    if not act:
        raise HTTPException(status_code=400, detail="当前没有任务")
    try:
        text = store.export_task_text()
        if act["task_id"] == "T-018":
            text += "\n\n## 历史机会卡原始数据（未改写）\n" + json.dumps(store.get_card_versions("OC-001"), ensure_ascii=False, indent=2)
            archive = read_reply_archive(os.environ.get("COZE_WORKSPACE_PATH", "/workspace/projects"))
            if archive.get("exists"):
                text += "\n\n## 首轮完整模型原始回复（本地归档补存，不是当前修订版）\n"
                text += json.dumps({k:v for k,v in archive.items() if k != "content"}, ensure_ascii=False, indent=2)
                text += "\n" + archive["content"]
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(e))
    return PlainTextResponse(text, media_type="text/plain; charset=utf-8")


@router.get("/api/export/{card_id}")
async def api_export_card(card_id: str):
    store = _store()
    act = store.get_active_task()
    if not act:
        raise HTTPException(status_code=400, detail="当前没有任务")
    cards = store.current_cards()
    card = next((c for c in cards if c.get("card_id") == card_id), None)
    if card is None:
        raise HTTPException(status_code=404, detail=f"未找到卡片 {card_id}")
    from storage.opportunity_store import get_opportunity_store
    text = get_opportunity_store().render_display(card)
    # 任务级“开发自测·仅模拟”纠正标记：覆盖单卡导出展示（不改写卡原文）
    meta = store.get_task_meta(act["task_id"]) if act else {}
    if meta.get("dev_self_test"):
        text = ("【开发自测 · 仅模拟】本任务材料为开发自拟、无公开出处，当前有效判断范围为“仅模拟／开发自测”；"
                "本卡原标注“非模拟资料／模拟False”为历史原始标记，已纠正（原文保留，不代表当前有效判断）。\n\n"
                + text)
    return PlainTextResponse(text, media_type="text/plain; charset=utf-8")


@router.post("/api/switch_mode")
async def api_switch_mode(payload: Dict[str, Any]):
    store = _store()
    if _RUN_LOCK.get("task_id"):
        raise HTTPException(status_code=409, detail=f"分析进行中（任务 {_RUN_LOCK['task_id']}），期间禁止切换模式")
    mode = payload.get("mode", "")
    if not mode:
        raise HTTPException(status_code=400, detail="缺少 mode")
    try:
        act = store.set_mode(mode=mode)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _overview()


@router.post("/api/activate")
async def api_activate(payload: Dict[str, Any]):
    store = _store()
    if _RUN_LOCK.get("task_id"):
        raise HTTPException(status_code=409, detail=f"分析进行中（任务 {_RUN_LOCK['task_id']}），期间禁止切换任务")
    task_id = payload.get("task_id", "")
    if not task_id:
        raise HTTPException(status_code=400, detail="缺少 task_id")
    try:
        act = store.activate_task(task_id=task_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _overview()


_DIFF_FIELDS = [
    ("demand_hypothesis", "需求假设"),
    ("status", "业务状态"),
    ("support_evidence", "支持证据"),
    ("comparison", "产品对照"),
    ("three_conditions", "三条件判断"),
    ("key_unknowns", "关键未知"),
    ("proposed_direction", "产品方向"),
    ("next_step_verification", "下一步验证"),
]


def _norm_ev(rows):
    """把证据行/产品行归一为可比较的 (key, dict)；key 取 record_id / product_id 等。"""
    out = []
    for x in rows or []:
        if not isinstance(x, dict):
            continue
        key = str(x.get("record_id") or x.get("product_id") or x.get("id") or x.get("source_id") or "")
        out.append((key, x))
    return out


def _mini(v):
    """精简展示：长数组只保留标识字段。"""
    if isinstance(v, list):
        ids = []
        for i in v[:8]:
            ids.append(str(i.get("record_id") or i.get("product_id") or i.get("source_id") or i) if isinstance(i, dict) else str(i))
        return "[" + ", ".join(ids) + (" …" if len(v) > 8 else "") + "]"
    return v


def _subdiff(a, b):
    """两 dict 的内容级差异：逐字段比较（含 evidence 内层级、basis/note 等）。"""
    res = {}
    for f in sorted(set(a) | set(b)):
        x, y = a.get(f), b.get(f)
        if json.dumps(x, ensure_ascii=False, sort_keys=True) != json.dumps(y, ensure_ascii=False, sort_keys=True):
            res[f] = (x, y)
    return res


def _diff_value(label, p, c):
    """单字段内容级差异；返回差异行列表。"""
    if json.dumps(p, ensure_ascii=False, sort_keys=True) == json.dumps(c, ensure_ascii=False, sort_keys=True):
        return []
    if isinstance(p, list) and isinstance(c, list):
        pmap, cmap = {k: v for k, v in _norm_ev(p)}, {k: v for k, v in _norm_ev(c)}
        unkeyed = not pmap and p
        if unkeyed:  # 元素本身无 record_id/product_id，退化为整列表差异
            return [{"field": label, "from": p, "to": c}]
        out = []
        for k in sorted(set(pmap) | set(cmap)):
            a, b = pmap.get(k), cmap.get(k)
            if a is None:
                out.append({"field": f"{label}·新增·{k}", "from": "(新增)", "to": _mini(b), "note": "+新增"})
            elif b is None:
                out.append({"field": f"{label}·移除·{k}", "from": _mini(a), "to": "(移除)", "note": "−移除"})
            else:
                sub = _subdiff(a, b)
                if sub:
                    for sk, (x, y) in sub.items():
                        out.append({"field": f"{label}·{k}.{sk}", "from": _mini(x), "to": _mini(y),
                                    "note": "字段级纠错/依据变化"})
                else:
                    out.append({"field": f"{label}·{k}", "from": _mini(a), "to": _mini(b)})
        return out
    if isinstance(p, dict) and isinstance(c, dict):
        sub = _subdiff(p, c)
        out = []
        for sk, (x, y) in sub.items():
            if isinstance(x, dict) and isinstance(y, dict):
                sub2 = _subdiff(x, y)
                if sub2:
                    for sk2, (x2, y2) in sub2.items():
                        out.append({"field": f"{label}·{sk}.{sk2}", "from": _mini(x2), "to": _mini(y2),
                                    "note": "内容级差异（含依据/说明）"})
                    continue
            out.append({"field": f"{label}·{sk}", "from": _mini(x), "to": _mini(y)})
        if not out:
            out.append({"field": label, "from": p, "to": c})
        return out
    return [{"field": label, "from": p, "to": c}]


def _diff_between(prev: Dict[str, Any], cur: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows = []
    for key, label in _DIFF_FIELDS:
        p, c = prev.get(key), cur.get(key)
        try:
            rows += _diff_value(label, p, c)
        except Exception:  # noqa: BLE001  单字段异常不阻断差异展示
            rows.append({"field": label, "from": p, "to": c, "note": "<解析异常>"})
    return rows


@router.get("/api/card/{card_id}/versions")
async def api_card_versions(card_id: str):
    store = _store()
    act = store.get_active_task()
    if not act:
        raise HTTPException(status_code=400, detail="当前没有任务")
    versions = store.get_card_versions(card_id)
    versions = sorted(versions, key=lambda c: c.get("version", 0))
    if not versions:
        raise HTTPException(status_code=404, detail=f"未找到卡片 {card_id}")
    result = []
    for i, c in enumerate(versions):
        prev = versions[i - 1] if i > 0 else None
        result.append({
            "card_id": card_id,
            "version": c.get("version"),
            "created_at": c.get("created_at"),
            "updated_at": c.get("updated_at"),
            "status": c.get("status"),
            "judgment_scope": c.get("judgment_scope"),
            "simulation": c.get("simulation"),
            "demand_hypothesis": c.get("demand_hypothesis"),
            "proposed_direction": c.get("proposed_direction"),
            "support_evidence": c.get("support_evidence"),
            "key_unknowns": c.get("key_unknowns"),
            "next_step_verification": c.get("next_step_verification"),
            "change_summary": c.get("change_summary"),
            "key_fields": c,
            "diff_from_prev": (exact_diff(prev, c) if act["task_id"] == "T-018" else _diff_between(prev, c)) if prev else None,
        })
    return {"task_id": act["task_id"], "card_id": card_id, "versions": result}


@router.get("/api/model_reply")
async def api_model_reply(task_id: str = "", card_id: str = "", version: int = 0):
    """读取机会卡某个 version 的关联原始模型回复（本地运行归档补存）。"""
    store = _store()
    act = store.get_active_task()
    tid = task_id or (act.get("task_id") if act else "")
    if not tid:
        raise HTTPException(status_code=400, detail="缺少 task_id")
    if not card_id or not version:
        raise HTTPException(status_code=400, detail="缺少 card_id 或 version")
    if tid == "T-018":
        if card_id != "OC-001" or version != 1:
            return {"exists": False, "reason": "仅补存首轮 v1 对应原始回复"}
        return read_reply_archive(os.environ.get("COZE_WORKSPACE_PATH", "/workspace/projects"))
    # 关联文件命名规则：raw_reply_v{version}.txt（如生成时未登录模型消息，由本地运行归档补存）
    name = os.path.join(os.environ.get("COZE_WORKSPACE_PATH", "/workspace/projects"),
                        "assets", "opportunity", tid, f"raw_reply_v{version}.txt")
    if not os.path.exists(name):
        return {"exists": False, "task_id": tid, "card_id": card_id, "version": version,
                "reason": "应用端未保存该版本的完整原始模型回复"}
    with open(name, "r", encoding="utf-8") as f:
        content = f.read()
    return {
        "exists": True,
        "task_id": tid,
        "card_id": card_id,
        "version": version,
        "content": content,
        "acquired_from": "由用户提供的本地运行归档补存",
        "related_log": f"RL-028（OC-001 v4 保存日志）",
        "archived_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "note": ("原始回复与当前复核修订（v5）为两份独立内容。以下是生成 v4 时的模型回复，由本地运行归档补存；"
                 "文中“尚未出现……”属于当时的原始表述，已在 v5 复核修订，当前分析结果以 v5 为准。"),
    }