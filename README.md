# 🔬 Chem-Literature: 化学与材料科学自动化文献发现与全文/SI双配对挖掘工作流

> **面向化学与电池材料科学的工程化文献工作流**  
> 一键完成从「自然语言指定研究主题」→「学术数据库候选筛选（自动剔除综述与会议摘要）」→「多高校/机构 WebVPN 账号集群并行爬取正文与支持信息 (SI)」→「物理落盘真实性校验与双配对基准数据库构建」全流程。

---

## 🌟 核心设计与特性

1. **零 Token 试错损耗（Zero-Token Waste Engine）**
   - 彻底摒弃传统 LLM 每次爬取都需要反复分析网页 DOM、试错下载链接造成的海量 Token 浪费。
   - 固化了主流出版社的最佳物理落盘策略：
     - **Elsevier (ScienceDirect)**: Crossref DOI 换取 PII → 无头浏览器 DOM 静默渲染 → 剔除打印遮罩与侧边栏 → 驱动 `Page.printToPDF` 导出高保真正文 → MMC API 提取补充材料。
     - **Wiley**: 直连二进制流 `/doi/pdfdirect/{doi}?download=true` → 自动探测解析 `downloadSupplement` 资产接口。
     - **IOP Publishing / ECS**: 结构化直接流路由，自动探测会议摘要与主文献边界。
     - **ACS / RSC / Springer / MDPI**: 统一的多级防反爬路由与文件头 Magic Byte (`%PDF-`, `PK\x03\x04` 等) 校验。

2. **多账号 WebVPN 动态负载均衡与自愈（Multi-Account Cluster）**
   - 只要将 1 到 N 个机构 WebVPN Cookie（JSON 格式）放入 `cookies/` 目录，引擎会自动扫描并启动对应数量的独立隔离浏览器实例与会话。
   - 任务以轮询（Round-Robin）均匀分发，避免单账号高频触发高校反爬拦截。
   - 具备自动重连机制：若遇到超时、断网或临时连接中断，自动恢复会话并从断点继续，无需从头重跑。

3. **原生研究型论文与支持信息双配对（Dual-Paired Baseline Corpus）**
   - 自动在检索阶段识别并过滤综述（Review）、社论、会议摘要（Meeting Abstracts）。
   - 下载完成后，自动执行物理文件完整性校验（大小阈值与文件签名），输出物理落地配齐（同时具备 PDF 和 SI）的原始研究数据集（`research_corpus_paired.csv` 及清单 `manifest.json`），为后续的大模型结构化抽取、材料知识图谱构建提供扎实底座。

4. **对用户桌面零干扰（Zero-Interference）**
   - 内置 `--window-position=-32000,-32000` 屏幕外极坐标静默启动，运行时绝不抢夺屏幕焦点与鼠标键盘。

---

## 📂 项目结构

```text
chem-literature/
├── config/
│   └── settings.yaml            # 工作流全局配置（检索关键词、年份、VPN前缀映射、下载安全限速等）
├── cookies/
│   ├── account_template.json    # WebVPN Cookie 模板文件
│   └── (用户放入的 account1.json, account2.json ...)
├── core/
│   ├── topic_discoverer.py      # 主题文献发现模块（OpenAlex API 接口，智能类型过滤）
│   ├── vpn_session.py           # WebVPN 隔离会话管理与 CDP 浏览器生命周期控制
│   └── paper_harvester.py       # 出版社定制化下载核心（支持各大主流期刊体系）
├── run_workflow.py              # 工作流总调度 CLI 入口
├── requirements.txt             # Python 依赖项
└── README.md                    # 本文档
```

---

## 🚀 快速使用指南

### 1. 安装依赖环境

建议在 Python 3.9+ 环境下运行：

```bash
pip install -r requirements.txt
```

### 2. 配置 WebVPN 账号凭据

从浏览器扩展（如 Cookie-Editor）导出高校 WebVPN（例如国科大、清华等）的 Cookie 为 JSON 格式：
1. 打开 `cookies/` 文件夹。
2. 保存为任意命名的 JSON 文件（如 `account1.json`，若有多个账号可继续添加 `account2.json`，支持 1~N 个账号自由扩展）：
   ```json
   [
     {
       "name": "wengine_vpn_ticket",
       "value": "your_ticket_value",
       "domain": "webvpn.your-university.edu.cn",
       "path": "/",
       "secure": true,
       "httpOnly": false
     }
   ]
   ```

### 3. 一键启动文献发现与挖掘

只需通过命令行传入自然语言研究主题或关键词，即可全自动执行：

```bash
# 示例 1：指定主题与起止年份
python run_workflow.py --topic "zinc-ion battery electrolyte" --year_start 2021 --year_end 2026 --max_papers 1000

# 示例 2：使用 config/settings.yaml 中的默认配置启动
python run_workflow.py
```

### 4. 获取研究型配对基准数据库

流水线执行完毕后，将在本地工作目录生成：
- `literature_candidates.csv`：检索发现的全部文献候选池元数据。
- `papers_pdf/`：物理下载的高清正文 PDF。
- `papers_si/`：物理下载的支持信息文件（PDF, Word, Excel, ZIP 等）。
- `research_corpus_paired.csv`：**仅包含正文与 SI 均物理落盘的研究型论文清单**，标明每个文件的物理路径、大小与出版社。
- `research_corpus_paired_manifest.json`：该数据集的宏观统计报告（总篇数、正文/SI总存储占用、各大出版社占比分布）。

---

## ⚙️ 配置文件说明 (`config/settings.yaml`)

你可以通过直接修改 `config/settings.yaml` 来持久化你的实验习惯：

```yaml
# 检索主题与范围配置
topic:
  query: "aqueous zinc battery"
  year_start: 2021
  year_end: 2026
  max_results: 5000

# 下载安全与速率控制（保护个人与机构账号安全）
harvest:
  min_delay_seconds: 16
  max_delay_seconds: 28
  cooldown_on_fail_seconds: 60
  max_consecutive_fails: 3
  browser_offscreen: true

# WebVPN 门户配置
webvpn:
  portal_url: "https://webvpn.ucas.edu.cn"
  cookies_dir: "cookies"
```

---

## 🛡️ 安全与合规说明

1. **防封号保护**：内置动态随机时延机制（16~28秒随机退避）及多账号负载均衡轮询，遇到持续拦截自动进入冷却挂起状态，切实保障学校和个人账号安全。
2. **凭据安全**：`.gitignore` 默认忽略 `cookies/*.json`，杜绝任何实际登录凭据误提交至代码仓库。

---

## 📄 License
MIT License.
