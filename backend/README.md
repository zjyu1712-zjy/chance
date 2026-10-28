# Agent 后端与运行说明

这里包含原扣子项目的 Agent 构建代码、14 个工具、规则、公共资料检索、机会卡校验与版本保存、预览接口，以及访客隔离接口。运行规则是 **v1.4**，原模型配置为 **doubao-seed-2-0-lite-260215**。

源码从现有项目白名单导出，未运行模型，也未修改案例记录。`source-manifest.json` 记录来源提交及 44 个文件的 SHA-256；`src/` 等原文件保持原样。原工作区当时已有未提交的访客隔离实现，因此文件哈希比单一 Git 提交号更完整地标识此快照。

## 目录与运行链路

| 位置 | 内容 |
| --- | --- |
| `src/agents/agent.py` | 构建一个 Agent；运行时把 v1.4 规则作为系统指令加载 |
| `src/tools/material_tools.py` | 14 个工具，含资料检索、输入登记、机会卡保存、纠错、来源排除与历史读取 |
| `src/storage/materials_store.py` | 检索随项目提供的 JSON 资料；不依赖向量库，不自动搜索互联网 |
| `src/storage/opportunity_store.py` | 任务、证据、状态校验和机会卡版本管理 |
| `src/visitor_api.py` | PostgreSQL 访客快照、写入锁、配额、历史保护 |
| `src/visitor_worker.py` | 每次请求使用临时目录，只复制规则与公共资料，再载入该访客快照 |
| `src/main.py` | 原扣子平台入口，含平台运行和开发预览接口 |
| `portable/app.py` | 独立部署入口，只开放访客接口；增加服务端鉴权和默认关闭模型的验收开关 |
| `config/agent_llm_config.json` | 原模型参数和回退提示词；不含凭证 |
| `assets/rules/` | 当前 v1.4 与历史规则文本；不会作为业务资料检索 |
| `assets/materials/` | S01—S09、P01—P09 公共资料，保留原日期及局限 |
| `pyproject.toml`、`uv.lock`、`.coze`、`scripts/` | 原扣子依赖与启动配置原件 |
| `portable/requirements.lock` | 独立访客服务所需依赖及下载哈希，版本取自原 uv.lock |

```text
浏览器 → 根目录 Node 网页服务 → portable/app.py
                               → visitor_api.py → 专用 PostgreSQL
                               → 临时目录中的 visitor_worker.py
                                  → 原检索/任务/保存工具
                                  → 实际 Agent 与模型（仅明确允许分析时）
```

三个归档案例仍放在根目录 `public/cases.json`，不灌入可写的访客数据库。新访客从 T-001 开始，与归档 T-016/T-017/T-018 完全分开。数据库、Cookie、临时输入、访客回复和生产环境变量均不在仓库里。旧的可选 `/review` 静态页和对象存储导出包未重复纳入，本仓库使用根目录已有的三个最终案例页面。

## 推荐：Docker Compose 启动完整项目

需要 Docker Engine / Docker Desktop 及 Compose。以下命令在**仓库根目录**执行。

```sh
cp .env.example .env
```

编辑 `.env`，为 `MENGNIU_AGENT_API_TOKEN` 和 `CHANCE_POSTGRES_PASSWORD` 分别设置自己的长随机十六进制字符串。数据库密码采用十六进制可避免连接串的特殊字符转义问题。它们仅供本次自建服务使用，不必复制原项目凭证，不得提交 Git。

保留 `MENGNIU_ALLOW_MODEL_CALLS=0`，先启动不调用模型的完整网页、接口和新数据库：

```sh
docker compose up --build -d
docker compose ps
```

打开 `http://localhost:5000`。可浏览三个归档案例；可创建自己的访客任务、登记技术测试资料、切换模式、刷新和导出。分析按钮此时返回“无模型验收模式”，不会给出固定业务答案。正式公开访问应在 Node 服务前配置 HTTPS 反向代理，并将 `PUBLIC_ORIGIN` 改为网站的准确源地址；保持 Secure/HttpOnly Cookie，不要为上线关闭这些保护。HTTP localhost 的 Cookie 支持取决于浏览器，访客恢复有问题时用本地 HTTPS。

Compose 仅把网页端口绑定本机 `127.0.0.1:5000`，后端和数据库不向外暴露；数据库使用命名卷持久化。常规停止使用 `docker compose stop` 或 `docker compose down`，不要加 `-v`，以免删除自己创建的数据库卷。跨机器迁移需要单独备份访客数据库，备份不得上传这个公共仓库。

## 接入自己的模型服务

模型服务仍是外部依赖，仓库不包含模型权重或付费额度。需要能调用所配置豆包模型并支持工具调用的兼容接口。原后端读取：

| 服务端变量 | 作用 |
| --- | --- |
| `COZE_WORKLOAD_IDENTITY_API_KEY` | 你有权使用的模型 API 凭证 |
| `COZE_INTEGRATION_MODEL_BASE_URL` | 对应模型服务的兼容 API 基地址；不要填写网页或 WebSDK 地址 |
| `MENGNIU_ALLOW_MODEL_CALLS` | 独立入口的运行开关；0 拒绝分析，1 允许真实调用 |
| `PGDATABASE_URL` | 专用 PostgreSQL 连接串；Compose 自动组装 |
| `MENGNIU_AGENT_API_TOKEN` | 网页服务与独立后端共享的服务间凭证，不是模型 API Key |
| `VISITOR_DEPLOYMENT_ENABLED` | 访客入口开关；Compose 配置为 1 |

原扣子的工作负载身份不随源码迁移，不能把原来环境里自动注入的短期身份视为可长期复用的公开凭证。模型名称和参数在 `config/agent_llm_config.json` 中；更换模型须自行确认兼容性，所得结果不应冒充历史案例原文。当前代码实际传给模型的是 model、temperature、timeout、thinking 等；配置文件虽还保留 top_p 和 max_completion_tokens，原构建函数并未传入这两个参数，本次没有悄悄改变其行为。

填入有效模型服务变量后，使用者可自行将 `MENGNIU_ALLOW_MODEL_CALLS` 改成 1，再执行：

```sh
docker compose up -d backend web
```

这只重建对应服务；此后用户点击分析才会产生真实模型调用和相应费用。本次整理没有执行这一步，也没有为检验效果重跑案例。

## 不使用 Docker 的本地启动

需要 Python 3.12、Node.js 20+ 和独立 PostgreSQL 16。先安装依赖：

```sh
cd backend
python3.12 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r portable/requirements.lock
```

在服务管理器或当前终端中配置上表变量，`PGDATABASE_URL` 必须指向新建的专用库。初次验收保持 `MENGNIU_ALLOW_MODEL_CALLS=0`。然后运行：

```sh
.venv/bin/python -m uvicorn portable.app:app --host 127.0.0.1 --port 8000
```

另开终端，回到仓库根目录，设置同一个 `MENGNIU_AGENT_API_TOKEN`、`MENGNIU_AGENT_UPSTREAM_URL=http://127.0.0.1:8000/visitor/v1`、`PUBLIC_ORIGIN=http://localhost:5000` 后执行 `npm start`。直接启动的 Node 服务不会自动读取 `.env`，需由终端或服务管理器传入变量。

`/healthz` 仅表示进程可响应。完整健康检查是带服务端凭证的 `/visitor/v1/health`，会核对 PostgreSQL、资料库和规则哈希，不调用模型；根目录网页的 `/api/bridge` GET 会代为请求它，浏览器不会看到服务端凭证。

## 在扣子复现

如使用扣子原生 Agent 工程，将 **backend 目录内容**作为工程根目录，使用其中的 `.coze`、`pyproject.toml` 与 `uv.lock`，由平台配置模型、数据库和身份。原生入口 `src/main.py` 依赖扣子运行时，并包含共用当前任务的开发接口；它保留用于原工程复现，不作为脱离扣子网关后匿名开放的入口。自建主机使用 `portable/app.py`。

原工程中的依赖还包括桌面/办公文件处理库，完整安装通常需要相应 Linux 系统开发包；通用服务器不必安装这些无关包，使用已锁定的 portable 依赖即可。不要把 backend 的 `.coze` 覆盖到仓库根目录：根目录配置属于 Node 网页项目。

## 无模型验证

在仓库根目录运行：

```sh
backend/.venv/bin/python -m unittest discover -s backend/tests -v
npm test
npm run build
python3 scripts/verify_publication.py
```

Python 测试检查服务鉴权、默认禁止分析、实际 Agent 图构建、资料读取、独立进程间的任务快照恢复、四模式切换和非法路径拒绝。图构建时禁止网络连接，不进行推理。没有设置 `CHANCE_TEST_DATABASE_URL` 时，数据库集成测试明确跳过；设置时仅允许本机或 Compose 的测试数据库，会创建并清理自己的随机测试访客，不能指向生产库。

GitHub Actions 使用临时 PostgreSQL 完成真实保存、重读、并发锁及原输入保护测试，并构建两个容器镜像；不配置模型凭证，不调用模型。结果以 Actions 实际记录为准。验收覆盖工程复现与保存边界，不证明模型结论恒定或新产品商业价值。

## 快照与限制

- 原公共资料含历史日期，未在本次打包时更新或重写。
- 模型生成、格式校验与保存失败可能仍发生；程序不会用固定案例结果替代分析。原失败处理与历史修订代码均保留。
- v1.4 规则原件 SHA-256：`bb7604b90c6fd78cc454ee02daba4268124f8bfd58fb0edcaf1417986481beae`。
- 仓库能复现运行结构、资料与程序，不能保证外部模型每次生成相同文本。三个最终案例继续注明经人工复核修订。
- 从原项目复制的依赖在导入 Agent 时会出现一条 LangGraph 弃用提示；不影响本次图构建测试，本次没有升级依赖或重写业务代码。
