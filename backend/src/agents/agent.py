# -*- coding: utf-8 -*-
"""蒙牛新品机会智能体（参赛原型）。

- 单一智能体支持四种工作模式（消费者捕手/热点捕手/市场缺口捕手/企业灵感捕手），
  共用同一套业务资料与机会记录。
- 通过工具真实检索资料库(S01—S09/P01—P09)，产出有依据的机会卡，支持版本更新与导出。
- 使用 LangChain create_agent 构建，带短期记忆（滑动窗口）。
"""
import json
import os
from typing import Annotated

from langchain.agents import create_agent
from langchain_openai import ChatOpenAI
from langgraph.graph import MessagesState
from langgraph.graph.message import add_messages
from langchain_core.messages import AnyMessage
from coze_coding_utils.runtime_ctx.context import default_headers

from storage.memory.memory_saver import get_memory_saver
from tools.material_tools import ALL_TOOLS

LLM_CONFIG = "config/agent_llm_config.json"
# v1.2：运行规则独立存放，运行时实际加载为系统提示词（不把资料/搭建说明塞进模型消息）
RULES_FILE = "assets/rules/05_智能体运行提示词_v1.4.txt"

# 默认保留最近 20 轮对话 (40 条消息)
MAX_MESSAGES = 40


def _windowed_messages(old, new):
    """滑动窗口: 只保留最近 MAX_MESSAGES 条消息"""
    return add_messages(old, new)[-MAX_MESSAGES:]  # type: ignore


class AgentState(MessagesState):
    messages: Annotated[list[AnyMessage], _windowed_messages]


def build_agent(ctx=None):
    workspace_path = os.getenv("COZE_WORKSPACE_PATH", "/workspace/projects")
    config_path = os.path.join(workspace_path, LLM_CONFIG)

    with open(config_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    api_key = os.getenv("COZE_WORKLOAD_IDENTITY_API_KEY")
    base_url = os.getenv("COZE_INTEGRATION_MODEL_BASE_URL")

    llm = ChatOpenAI(
        model=cfg["config"].get("model"),
        api_key=api_key,
        base_url=base_url,
        temperature=cfg["config"].get("temperature", 0.3),
        streaming=True,
        timeout=cfg["config"].get("timeout", 600),
        extra_body={
            "thinking": {
                "type": cfg["config"].get("thinking", "disabled")
            }
        },
        default_headers=default_headers(ctx) if ctx else {},
    )

    # 实际系统提示词：优先加载 v1.2 独立运行规则文件；文件缺失时回退到配置 sp
    system_prompt = cfg.get("sp")
    rules_path = os.path.join(workspace_path, RULES_FILE)
    if os.path.exists(rules_path):
        with open(rules_path, "r", encoding="utf-8") as rf:
            content = rf.read().strip()
        if content:
            system_prompt = content

    return create_agent(
        model=llm,
        system_prompt=system_prompt,
        tools=ALL_TOOLS,
        checkpointer=get_memory_saver(),
        state_schema=AgentState,
    )