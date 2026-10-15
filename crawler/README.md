# crawler —— 法规数据采集

本目录是**离线数据管道**，与应用（`src/`）解耦、平级。原因见 `TDD.md` §2.2.1：
爬虫只写数据、应用只读数据，两者生命周期完全不同。

---

## 一、数据来源

| 来源 | 域名 | 权威性 | 采集内容 | 状态 |
|---|---|---|---|---|
| **国家法律法规数据库** | `flk.npc.gov.cn` | 全国人大官方 | 法规分类体系、分类统计、法规条文 | ✅ 已接入 |
| 中国政府网政策文件库 | `sousuo.www.gov.cn` | 国务院 | 行政法规、国务院文件 | 🔶 接口已定位，检索参数待调 |
| 司法部 / 地方政府规章库 | `moj.gov.cn` 等 | 部门/地方 | 部门规章、地方规章 | ⏭ 规划中 |

**为什么以国家法律法规数据库为主**：它是全国人大维护的官方数据库，分类体系权威、结构化程度高（有稳定的分类码 `codeId`），且提供公开 JSON 接口。

---

## 二、已探明的接口

> 以下接口均从站点 JS bundle **逆向得到**（不是猜测）。`flk.npc.gov.cn` 是纯 SPA，
> 任何未匹配路径都返回同一个 `index.html`，因此**不能用"路径返回 200"来判断接口存在**——
> 必须看返回的是 JSON 还是 HTML。

| 接口 | 方法 | 用途 |
|---|---|---|
| `/law-search/search/enumData` | **GET** | 法规分类树（宪法/法律/行政法规/监察法规/地方法规/司法解释 + 子类） |
| `/law-search/index/aggregateData` | **GET** | 各类法规数量统计 |
| `/law-search/search/list` | **POST** | 法规条目列表（参数需进一步逆向） |
| `/law-search/search/flfgDetails` | GET | 法规详情 |

**已踩的坑**：

1. **方法用错会返回 `{"msg":"Request method 'GET' not supported","code":500}`**——
   条目列表必须是 POST，分类/统计必须是 GET。
2. **bundle 里的 `searchType` 不是列表接口参数**——它是"高级搜索"里"并且/或/不含"的取值，
   误当成列表参数会一直返回 `{"msg":"系统异常","code":500}`。
3. 后端是 **RuoYi（Java）** 框架（响应里有 `com.ruoyi.law.domain.*`），
   分页参数风格参照 RuoYi。

---

## 三、用法

```bash
# 采集参考数据（分类树 + 分类统计）
python crawler/fetch_reference_data.py
```

产出：

```
data/reference/law_catalog.json    # 法规分类树
data/reference/law_stats.json      # 分类统计
```

**采集结果（2026-10-14 实测）**：

| 类别 | 数量 |
|---|---|
| 宪法 | 1 |
| 法律 | 302 |
| 行政法规 | 605 |
| 监察法规 | 2 |
| 地方法规 | 15710 |
| 司法解释 | 562 |

---

## 四、爬虫礼仪

| 项 | 做法 |
|---|---|
| 请求间隔 | 接口之间 `sleep 3s`（条目级采集时 3~5s 随机） |
| UA | 使用真实浏览器 UA（池轮换） |
| Referer | 带上来源站 |
| 重试 | `tenacity` 指数退避，最多 3 次 |
| robots.txt | 在线解析后遵守 |
| 断点续爬 | 写入 `.crawl_checkpoint.json`，中断后可续 |
| 鉴权 | **不绕过任何鉴权**，只访问公开接口 |

---

## 五、数据落地约定

- **原始数据** → `data/raw/`（爬虫直接输出）
- **清洗后数据** → `data/processed/`（JSON 供程序 + Excel 供人工查看）
- **参考数据** → `data/reference/`（分类体系、统计、领域种子）

⚠️ `data/` 下所有内容**不入库**（`.gitignore` 有 blanket `*.json`），见 `TDD.md` R5「数据即产物」。
因此 `data/reference/` 是**例外**：它们是采集脚本的产物但被显式入库，作为项目的数据凭证。

---

## 六、待办

- [ ] 逆向 `/law-search/search/list` 的 POST 参数，打通**条目级**采集
- [ ] 调整政府网政策文件库的检索参数，接入第二数据源
- [ ] 条目级采集落地后，接入 `scripts/run_extraction.py` 做条文级实体抽取
- [ ] 法规时效性跟踪（立改废释）—— 见 PRD §7.0
