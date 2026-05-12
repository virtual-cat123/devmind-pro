# DevMind Pro

轻量级个人知识库工具，CLI 驱动，DeepSeek 作为分析引擎。

## 特性

- **纯 Python 3.10+**，仅依赖 `requests` 和 `python-dotenv`
- **SQLite** 本地存储，零配置，自动建表
- **DeepSeek API** 直连 HTTP，不引入任何 SDK
- 三个子命令覆盖知识库完整工作流：采集 → 分析 → 问答

## 安装

```bash
pip install -r requirements.txt
cp .env.example .env
# 编辑 .env，填入你的 DeepSeek API Key
```

## 用法

### 添加网页

```bash
python devmind.py add --url https://example.com/article
```

输出：`Note added: id=1, title=文章标题`

网页内容经过去标签清洗后存入数据库，状态为 `raw`（待处理）。

### 处理笔记

处理单条：

```bash
python devmind.py process --note-id 1
```

批量处理全部未处理的笔记：

```bash
python devmind.py process --all
```

启用深度推理（`reasoning_effort=high`，单次调用消耗数万 token）：

```bash
python devmind.py process --all --deep
```

处理过程会：
1. 读取数据库中**全部**已处理笔记作为上下文（不截断，token 用量随知识库增长）
2. 调用 DeepSeek 提取概念、生成摘要、发现关联
3. 将结果写回数据库，状态变为 `processed`

### 提问

```bash
python devmind.py ask "什么是注意力机制"
python devmind.py ask "Transformer 架构的核心创新" --deep
```

系统会自动：
- 对问题分词，在已处理笔记中搜索相关条目
- 取最相关的 10 篇作为上下文
- 生成基于上下文的回答，并列出引用来源

## 数据库结构

首次运行自动在脚本同级目录创建 `devmind.db`。

**notes 表**

| 字段 | 类型 | 说明 |
|---|---|---|
| id | INTEGER | 主键 |
| url | TEXT | 来源 URL |
| title | TEXT | 文章标题 |
| content | TEXT | 清洗后的纯文本 |
| concepts | TEXT | 提取的概念，逗号分隔 |
| summary | TEXT | LLM 生成的中文摘要 |
| added_at | DATETIME | 添加时间 |
| processed_at | DATETIME | 处理完成时间 |
| status | TEXT | `raw` / `processed` |

**relations 表**

| 字段 | 类型 | 说明 |
|---|---|---|
| id | INTEGER | 主键 |
| source_note_id | INTEGER | 源笔记 ID |
| target_note_id | INTEGER | 目标笔记 ID |
| reason | TEXT | 关联理由 |
| strength | TEXT | 关联强度：强 / 中 / 弱 |

## 依赖

| 包 | 用途 |
|---|---|
| `requests` | HTTP 请求（抓取网页 + 调用 API） |
| `python-dotenv` | 从 `.env` 加载环境变量 |

## License

MIT
