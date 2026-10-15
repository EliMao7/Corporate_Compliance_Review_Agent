# Corporate_Compliance_Review_Agent

**企业合规审查 Agent 系统** —— 基于公共法规知识图谱的企业制度 / 合同合规排查系统。

> 把"一份文件该对照哪些法规、有哪些风险、归口谁管"从人工逐条翻查，
> 变成一次**可溯源、可复核**的问答。

---

## 一、核心定位（三条边界）

1. **只给依据、不下结论** —— 系统提供法规依据检索与风险提示，**不提供法律意见，不判"合法/违法"**。
2. **每条判断必须可溯源** —— 无法找到依据的，如实说明"未检索到直接依据"，不编造。
3. **高风险强制提示** —— 无依据增设许可、擅自加设处罚、超越权限等，置顶提示并建议咨询主管部门/律师。

这三条不是"免责话术"，而是**产品设计约束**：它们直接决定了 `safety_checker` 节点的纯规则实现、
`answer_fusion` 的结论性表述拦截，以及评测门禁里的三项硬指标（见 `TDD.md` §6 / §13）。

---

## 二、快速开始

```bash
# 1. 基础设施（Neo4j + Redis + MySQL）
docker compose up -d

# 2. Python 环境
python -m venv .venv
# Windows: .venv\Scripts\activate
source .venv/bin/activate
pip install -r requirements.txt

# 3. 环境变量
cp .env.example .env
# 编辑 .env：填入 MODEL_API_KEY（必填）

# 4. 模型：下载 BGE-M3 到 models/bge-m3/

# 5. 环境验证（6 项检查；退出码 0 = 全绿）
python validate_env.py

# 6. 离线流水线：建知识库
python scripts/run_crawler.py --max 500      # 采集法规
python scripts/run_extraction.py             # 抽取实体/关系（无 API Key 时用 --from-seed）
python scripts/build_graph.py                # 建图（clear + build）
python scripts/build_index.py                # 建 FAISS 向量索引

# 7. 前端构建（首次或前端改动后）
cd frontend && npm install && npm run build && cd ..

# 8. 启动（同时托管 API + 前端静态文件，单进程）
python scripts/start_api.py
# → http://localhost:8000 即是完整应用
```

> **数据即产物（R5）**：`data/` 与 `models/` 下的 JSON / 索引文件**不入库**。
> 克隆后必须自己跑上面第 6 步重建知识库。唯一例外是 `data/reference/` 的三个数据凭证文件。

---

## 三、项目结构

```
config/          配置层（settings / paths / port_check）—— 所有模块的地基
crawler/         离线数据采集（法规爬虫 + 清洗），与应用平级、解耦
src/
  common/        公共工具（LLM / 日志 / Neo4j 客户端 / 嵌入模型 / 会话记忆）
  extraction/    LLM 实体关系抽取（事项 / 风险点 / 措施 / 归口部门）
  knowledge_graph/  ★ schema.py 是图谱契约唯一主人；建图 / 查询 / 校验 / Text2Cypher
  vector_store/  FAISS 语义召回 + 实体链接
  agents/        ★ state.py 是 Agent 契约唯一主人；LangGraph 9 节点编排
  history/       对话历史冷热分层（Redis 热缓存 + MySQL 事实源）
  a2a/           A2A 对外协作端点（Agent Card + JSON-RPC）
  api/           ★ models.py 是前后端字段契约唯一主人
tool_layer/      工具业务实现（唯一实现源，零框架依赖）
mcp_server/      MCP 外壳（独立进程，stdio）
evals/           评测闭环（金标集 + 硬门禁）
frontend/        Vue 3 + Element Plus 审查界面
scripts/         入口脚本（采集 / 抽取 / 建图 / 建索引 / 启动 / 契约检查）
```

工程规范 R1–R8（单一事实来源、契约先行、数据即产物、模块自检…）见 `TDD.md` 第 0 章。

---

## 四、API

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/auth/register` | 注册（用户名 + 密码，返回 JWT） |
| POST | `/api/auth/login` | 登录（返回 JWT） |
| POST | `/api/consult` | 合规审查（60s 超时，并发上限可配） |
| POST | `/api/consult/stream` | SSE 流式：进度事件 + token 增量 |
| GET | `/api/health` | 健康检查（**唯一免登录端点**） |
| GET | `/api/stats` | 知识图谱 / 向量索引 / API 统计 |
| GET | `/api/conversations` | 本人对话列表 |
| GET | `/api/conversations/{id}` | 会话详情（仅本人） |
| DELETE | `/api/conversations/{id}` | 删除会话（仅本人，越权返回 404） |
| GET | `/.well-known/agent-card.json` | A2A Agent Card（能力发现） |
| POST | `/api/a2a` | A2A JSON-RPC 入口 |

> **认证（PRD 功能4 / TDD §6.4）**：除 `/api/health` 外所有端点都要求
> `Authorization: Bearer <token>`；未登录一律 **401**。越权读取/删除他人会话返回 **404**
> （不是 403——不泄露"该资源是否存在"）。可用 `AUTH_ENABLED=false` 关闭（仅限本地联调）。

**响应字段（`src/api/models.py` 为唯一主人，12 项）**：
`answer` · `intent` · `intent_confidence` · `doc_type` · `matters` · `risk_signals` ·
`authorities` · `measures` · `disclaimers`（**必有**） · `warnings` · `linked_entities` · `duration_ms`

```bash
# 1) 注册（或登录）拿令牌
TOKEN=$(curl -s -X POST http://localhost:8000/api/auth/register \
  -H "Content-Type: application/json" \
  -d '{"username":"demo","password":"demo123456"}' | python -c "import sys,json;print(json.load(sys.stdin)['access_token'])")

# 2) 带令牌发起合规审查
curl -X POST http://localhost:8000/api/consult \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"query":"我们准备发一份《供应商管理办法》，有没有跟法规冲突的地方？"}'
```

---

## 五、质量门禁

```bash
python scripts/check_contracts.py    # R1 契约扫描（历史错误名 / 枚举位置 / 前后端字段）
make eval-fast                       # 快速子集
make eval-full                       # 全量金标集
make eval-e2e                        # 经已启动的 API 做端到端
```

**硬门禁**（不达标 → 非零退出码 → 可进 CI）：

| 指标 | 判定 |
|---|---|
| 安全告警召回 | **= 100%** |
| 免责声明覆盖 | **= 100%** |
| 结论性表述出现次数 | **= 0** |
| 意图识别准确率 | ≥ 90% |

输出 `evals/last_report.md`，可直接出示。

---

## 六、业务背景

| 维度 | 现状 |
|---|---|
| 使用者 | 企业法务 / 合规团队，**3–5 人** |
| 工作量 | **每周审 5–8 份**文件（制度、合同、对外公告） |
| 单份耗时 | 需向上追溯**几十部法规、几十条相关条款**，人工约 **2 小时** |
| 最危险的问题 | 不是查不到，而是**没想到该查哪部法** |
| 典型风险 | 无依据增设行政许可、擅自加设处罚、变相设置审批前置 |
| 验收口径 | 单份初筛 **2 小时 → 10 分钟以内**（机器 p95 + 人工复核，端到端口径） |

---

## 七、技术架构（骨架）

```
前端 Vue 3 ──→ FastAPI ──→ LangGraph 9 节点编排 ──→ Neo4j 知识图谱
                                    │                FAISS 向量索引
                                    └──→ MySQL + Redis（会话）
对外协作：A2A（Agent Card + JSON-RPC）· 工具层双外壳（@tool + MCP）
质量保障：离线评测门禁（金标集 + 硬规则非零退出）
```

| 层 | 技术 |
|---|---|
| 大模型 | 阿里云 DashScope（qwen-plus） |
| 编排 | LangGraph 1.2（状态图 + 条件边） |
| 知识图谱 | Neo4j 5.26 LTS |
| 向量检索 | FAISS + BGE-M3（1024 维，本地部署） |
| Web | FastAPI + Uvicorn |
| 前端 | Vue 3 + Element Plus + Vite |
| 会话 | MySQL 8.0（事实源）+ Redis（热缓存） |
| 工具协议 | LangChain `@tool` + MCP |
| 对外协作 | A2A SDK 1.2.2 |

详见 `TDD.md` §1 / §4。

---

## 八、数据

### 8.1 已采集（可复现）

| 文件 | 来源 | 内容 |
|---|---|---|
| `data/reference/law_catalog.json` | 国家法律法规数据库 `enumData` | 法规分类树（6 大类 + 子类） |
| `data/reference/law_stats.json` | 国家法律法规数据库 `aggregateData` | 各类法规数量统计 |
| `data/reference/compliance_seed.json` | 领域模型（人工整理） | 8 类节点 55 实体 + 45 关系，图谱建模种子 |

**法规库规模（2026-10-14 实测）**：宪法 1 · 法律 302 · 行政法规 605 · 监察法规 2 · 地方法规 15710 · 司法解释 562。

### 8.2 复现方式

```bash
python crawler/fetch_reference_data.py
```

数据来源、接口清单、爬虫礼仪与踩坑记录见 `crawler/README.md`。

---

## 九、与姊妹项目的关系

本项目与**智慧问诊 Agent 系统**共用同一套工程骨架（约 80% 复用）：
LangGraph 9 节点编排 · FAISS+BGE-M3 混合检索 · Neo4j + Text2Cypher 六层防护 ·
工具层双外壳 · A2A 服务端 · 评测门禁 · 冷热分层会话。

迁移工作量集中在三处：① 数据与 Schema 的域替换；② 各 Agent 的提示词；③ 安全规则
（合规域多了"拦截结论性表述"这条）。逐项对照见 `TDD.md` 附录 A。

---

## 十、文档版本

| 文档 | 版本 | 日期 |
|---|---|---|
| PRD.md | v1.0 | 2026-10-14 |
| TDD.md | v1.0 | 2026-10-14 |

---

## 十一、免责声明

本项目为**技术示范项目**，用于展示大模型 + 知识图谱在企业合规场景的落地形态。

- 系统输出**不构成法律意见**，不得作为决策唯一依据
- 法规数据来自公开渠道，**使用时应核对官方最新版本**
- 正式使用前请由法务或执业律师复核
