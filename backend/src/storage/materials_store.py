# -*- coding: utf-8 -*-
"""业务资料存取与检索层。

将【业务资料包】中解析出的来源记录(S01—S09)与产品/替代记录(P01—P09)
从 assets/materials/*.json 载入内存，并对外提供只读检索能力。

保持"归档转述"标注：所有摘要都是既有核验记录的转述，不是完整原文。
"""
import json
import os
import re
from typing import Any, Dict, List, Optional

WORKSPACE = os.getenv("COZE_WORKSPACE_PATH", "/workspace/projects")
_MATERIALS_DIR = os.path.join(WORKSPACE, "assets", "materials")

# 资料核查确认，固定不变，不随生成日期变化
VERIFICATION_DATE = "2026-10-02"
PACKAGE_DATE = "2026-10-11"
# 四种工作模式
MODES = ["消费者捕手", "热点捕手", "市场缺口捕手", "企业灵感捕手"]


class MaterialStore:
    """载入并缓存业务资料，提供检索。单例风格，进程内缓存。"""

    def __init__(self) -> None:
        self._sources: Optional[Dict[str, Dict[str, Any]]] = None
        self._products: Optional[Dict[str, Dict[str, Any]]] = None
        self._meta: Dict[str, Any] = {}

    def _ensure_loaded(self) -> None:
        if self._sources is not None:
            return
        with open(os.path.join(_MATERIALS_DIR, "sources.json"), "r", encoding="utf-8") as f:
            src_doc = json.load(f)
        with open(os.path.join(_MATERIALS_DIR, "products.json"), "r", encoding="utf-8") as f:
            prod_doc = json.load(f)
        self._meta = src_doc.get("meta", {})
        self._sources = {s["source_id"]: s for s in src_doc.get("sources", [])}
        self._products = {p["product_id"]: p for p in prod_doc.get("products", [])}

    def overview(self) -> Dict[str, Any]:
        self._ensure_loaded()
        assert self._sources is not None and self._products is not None
        return {
            "来源数": len(self._sources),
            "产品/替代记录数": len(self._products),
            "资料核查日期": VERIFICATION_DATE,
            "打包日期": PACKAGE_DATE,
            "来源": sorted(self._sources.keys()),
            "产品": sorted(self._products.keys()),
        }

    def get_source(self, source_id: str) -> Optional[Dict[str, Any]]:
        self._ensure_loaded()
        return self._sources.get(source_id)

    def get_product(self, product_id: str) -> Optional[Dict[str, Any]]:
        self._ensure_loaded()
        return self._products.get(product_id)

    def list_sources(self) -> List[Dict[str, Any]]:
        self._ensure_loaded()
        assert self._sources is not None
        return [dict(s) for s in self._sources.values()]

    def list_products(self) -> List[Dict[str, Any]]:
        self._ensure_loaded()
        assert self._products is not None
        return [dict(p) for p in self._products.values()]

    def known_source_ids(self) -> set:
        self._ensure_loaded()
        return set(self._sources.keys())

    def known_product_ids(self) -> set:
        self._ensure_loaded()
        return set(self._products.keys())

    # 产品归属由程序判定（基础档案不改，仅检索/对照时提供分类）
    ATTRIBUTION = {
        "P01": "蒙牛", "P02": "蒙牛", "P03": "蒙牛", "P04": "蒙牛",
        "P05": "竞品", "P06": "竞品", "P07": "蒙牛", "P08": "蒙牛",
        "P09": "替代",
    }

    def product_attribution(self, product_id: str) -> str:
        return self.ATTRIBUTION.get((product_id or "").strip().upper(), "未知")

    def search(self, query: str = "", source_id: str = "", product_id: str = "") -> Dict[str, Any]:
        """按来源ID / 产品ID / 关键词检索，返回完整来源字段。

        没有命中时返回 {"found": False}，由调用方给出"未找到"。
        """
        self._ensure_loaded()
        assert self._sources is not None and self._products is not None

        q = (query or "").strip().lower()

        # 1) 精确按来源ID
        if source_id:
            s = self._sources.get(source_id.strip().upper())
            if s:
                return {"found": True, "type": "source", "record": s}
            return {"found": False, "reason": f"未找到来源 {source_id}"}

        # 2) 精确按产品ID
        if product_id:
            p = self._products.get(product_id.strip().upper())
            if p:
                payload = dict(p)
                payload["attribution"] = self.product_attribution(product_id)
                payload["sources"] = [
                    self._sources[sid] for sid in p.get("source_ids", []) if sid in self._sources
                ]
                return {"found": True, "type": "product", "record": payload}
            return {"found": False, "reason": f"未找到产品/替代记录 {product_id}"}

        # 3) 关键词检索（来源/产品）
        tokens = [t for t in re.split(r"[\s,，、/]+", q) if t]
        if not q:
            return {"found": True, "sources_hit": self.list_sources(), "products_hit": self.list_products()}

        def _match(haystack: str) -> bool:
            # 所有词元都作为子串出现才算命中（避免把"坚果 酸奶"当整串找不到）
            return all(tok in haystack for tok in tokens)

        hit_sources = []
        for s in self._sources.values():
            haystack = " ".join([
                s.get("title", ""), s.get("summary", ""), s.get("supports", ""),
                s.get("nature", ""), ",".join(s.get("related_products", [])),
            ]).lower()
            if _match(haystack):
                hit_sources.append(dict(s))

        hit_products = []
        for p in self._products.values():
            haystack = " ".join([
                p.get("name", ""), p.get("version_spec", ""),
                p.get("comparison_dims", ""), p.get("unknowns", ""),
                ",".join(p.get("source_ids", [])),
            ]).lower()
            if _match(haystack):
                # 一并附带来源记录，便于模型看到支撑该产品规格的原始来源
                payload = dict(p)
                payload["attribution"] = self.product_attribution(p.get("product_id", ""))
                payload["sources"] = [
                    self._sources[sid] for sid in p.get("source_ids", []) if sid in self._sources
                ]
                hit_products.append(payload)

        if not hit_sources and not hit_products:
            return {"found": False, "reason": f"资料库中未命中关键词「{query}」，不要用模型记忆补齐产品事实。"}

        return {
            "found": True,
            "query": query,
            "sources_hit": hit_sources,
            "products_hit": hit_products,
        }

    def batch_search(self, queries: Optional[List[str]] = None,
                     attribution: Optional[str] = None) -> Dict[str, Any]:
        """批量检索（每个 query 一次检索，作为一次程序调用批量记录）。

        attribution 只在 蒙牛 / 竞品 / 替代 时按产品归属过滤结果。
        """
        self._ensure_loaded()
        queries = [q.strip() for q in (queries or []) if q and q.strip()]
        out_sources: List[Dict[str, Any]] = []
        out_products: List[Dict[str, Any]] = []
        miss = []
        for q in queries:
            r = self.search(query=q)
            if not r.get("found"):
                miss.append(q)
                continue
            for s in r.get("sources_hit", []):
                if s not in out_sources:
                    out_sources.append(s)
            for p in r.get("products_hit", []):
                attr = self.product_attribution(p.get("product_id", ""))
                if attribution and attr and attr != attribution:
                    continue
                if p not in out_products:
                    out_products.append(p)
        return {
            "found": bool(out_sources or out_products),
            "given_queries": queries,
            "attribution_filter": attribution or "",
            "missed_queries": miss,
            "sources_hit": out_sources,
            "products_hit": out_products,
        }


_store = MaterialStore()


def get_material_store() -> MaterialStore:
    return _store