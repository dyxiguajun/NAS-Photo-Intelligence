# 匿名访问实测报告

测试时间：2026-11-11 18:40–18:46（America/Los_Angeles）
测试方式：`curl`，不带 `Authorization` 头，清除 `GH_TOKEN` / `GITHUB_TOKEN` 环境变量，不使用 GitHub 登录态。
测试对象：`dyxiguajun/NAS-Photo-Intelligence`（public，默认分支 `main`）。
审查快照：`81827209198f1e48d5e39d6b7268bff2e8bec229`（PR #7 合并）。

本报告只记录**实际 HTTP 状态码**。访问失败不自动推断为权限问题；每个非 200 结果都给出可判别的原因。

## 1. 三类链接的匿名结果

| # | 目标 | URL | HTTP | 说明 |
|---:|---|---|---:|---|
| 1 | 仓库主页 | `https://github.com/dyxiguajun/NAS-Photo-Intelligence` | 200 | 285 KB HTML |
| 2 | 普通文件页（blob） | `https://github.com/dyxiguajun/NAS-Photo-Intelligence/blob/main/README.md` | 200 | 256 KB HTML |
| 3 | 普通文件页（源码） | `.../blob/main/src/nas_photo/cli.py` | 200 | 343 KB HTML |
| 4 | 目录页（tree） | `.../tree/main/docs` | 200 | 服务端可读；但见第 4 节 robots 限制 |
| 5 | raw 纯文本 | `https://raw.githubusercontent.com/dyxiguajun/NAS-Photo-Intelligence/main/README.md` | 200 | 13,731 字节纯文本 |
| 6 | raw 固定 SHA 源码 | `https://raw.githubusercontent.com/.../81827209198f1e48d5e39d6b7268bff2e8bec229/src/nas_photo/cli.py` | 200 | 12,518 字节纯文本 |
| 7 | raw 不存在文件（对照组） | `.../main/DOES_NOT_EXIST.md` | **404** | 对照组：证明 404 表示「文件不存在」，不是权限拒绝 |
| 8 | API 仓库元数据 | `https://api.github.com/repos/dyxiguajun/NAS-Photo-Intelligence` | 200 | 6,203 字节 JSON |
| 9 | API 文件内容 | `.../contents/src/nas_photo/cli.py` | 200 | 18,258 字节 JSON |
| 10 | API 完整文件树 | `.../git/trees/main?recursive=1` | 200 | 33,587 字节 JSON |
| 11 | API 限流查询 | `https://api.github.com/rate_limit` | 200 | `x-ratelimit-limit: 60`、`remaining: 57` |
| 12 | jsDelivr 镜像 `@main` | `https://cdn.jsdelivr.net/gh/dyxiguajun/NAS-Photo-Intelligence@main/docs/AI_REVIEW.md` | 200 | 备用纯文本镜像 |
| 13 | jsDelivr 镜像 `@SHA` | `.../NAS-Photo-Intelligence@8182720.../docs/AI_REVIEW.md` | 200 | 固定版本镜像 |
| 14 | github.com `/raw/` 路径 | `.../raw/main/docs/AI_REVIEW.md` | 200 | 服务端可读；但见第 4 节 robots 限制 |
| 15 | Pages 根（启用前） | `https://dyxiguajun.github.io/NAS-Photo-Intelligence/` | **404** | 原因：Pages 站点尚未启用，`GET /repos/.../pages` 同样返回 404；不是权限问题 |

**结论**：public 仓库的三类链接在匿名环境下均返回 200，无需登录。GitHub 普通文件链接、raw 域名与 API 都不要求凭证。

## 2. 不同抓取工具 UA 的服务端响应

同一组 URL 分别以不同 User-Agent 匿名请求，记录服务端返回码：

| User-Agent | github.com blob | github.com tree | raw.githubusercontent.com | api.github.com |
|---|---:|---:|---:|---:|
| `curl/8.7.1` | 200 | 200 | 200 | 200 |
| `Mozilla/5.0 ... Chrome/131.0` | 200 | 200 | 200 | 200 |
| `ChatGPT-User/1.0` | 200 | 200 | 200 | 200 |
| `GPTBot/1.2` | 200 | 200 | 200 | 200 |
| `OAI-SearchBot/1.0` | 200 | 200 | 200 | 200 |
| `Claude-User/1.0` | 200 | 200 | 200 | 200 |
| `ClaudeBot/1.0` | 200 | 200 | 200 | 200 |

**GitHub 在 HTTP 层没有按 UA 拦截任何一类请求。** 因此「某个 AI 工具读不到」不能据此推断为 GitHub 权限不足。

## 3. 未发布文件的匿名结果（404，不是 403）

| 路径 | raw HTTP | API contents HTTP |
|---|---:|---:|
| `data/index.sqlite3` | 404 | 404 |
| `config/local.json` | 404 | — |
| `config/phase3_samples.json` | 404 | — |
| `reports/agent-coordination/STATE.json` | 404 | 404 |
| `PROJECT_STATUS.md` | 404 | — |
| `docs/phases/evidence/` | 404 | — |
| `tools/exiftool/exiftool` | 404 | — |
| `private/backups/docs-pre-sanitize-20261111.tar.gz` | 404 | — |
| `logs/events.jsonl` | 404 | — |

这些路径由 `.gitignore` 默认拒绝规则排除，从未进入版本库。404 表示**未发布**，与账号权限无关；即使登录后也无法从仓库读取。

## 4. 已知限制：抓取端 robots 策略（与仓库权限无关）

`https://github.com/robots.txt` 的第一个 User-agent 分组明确列出：

```
User-agent: GPTBot
User-agent: OAI-SearchBot
User-agent: ChatGPT-User
User-agent: ClaudeBot
User-agent: Claude-SearchBot
User-agent: Claude-User
User-agent: PerplexityBot
User-agent: Perplexity-User
Crawl-delay: 1
...
Disallow: /*/raw/
Disallow: /*/tree/
Disallow: /*/blame/
Disallow: /*/archive/
```

实测该分组内**没有**针对 `/owner/repo/blob/...` 的禁止规则，因此 blob 文件页对 AI 抓取工具是允许的。但 `github.com/.../tree/...`（目录页）与 `github.com/.../raw/...`（仓库内 raw 按钮路径）被明确禁止。遵守 robots.txt 的 ChatGPT / Claude 网页抓取工具可能拒绝打开这两类路径，**即便 curl 请求返回 HTTP 200**。

- `raw.githubusercontent.com/robots.txt` 返回 404 → 按 robots 约定该源无禁止规则。
- `dyxiguajun.github.io/robots.txt` 返回 404 → Pages 源同样无禁止规则。

因此本静态入口统一使用 `raw.githubusercontent.com` 纯文本直链和 Pages 站点，不依赖 github.com 的 tree/raw 路径。

**必须明确的结论**：
1. 服务端对所有测试的 UA 都返回 200，GitHub 没有做基于 UA 的 HTTP 拦截。
2. 若 ChatGPT 的某个特定网页工具仍无法读取，可能是该工具自身的 robots 遵守策略、URL 白名单、渲染限制或网络策略；这些属于**工具侧限制**。
3. **修改 GitHub 仓库权限、可见性、分支保护或账号授权，不能保证解决上述工具侧限制**，不应据此声称「权限改了就好」。
4. 匿名 GitHub API 限额为 60 次/小时，超出会返回 403，属于限流而非权限拒绝。

## 5. 已核对的现有工作（避免覆盖）

| 成果 | 归属 | 本改动的处理 |
|---|---|---|
| PR #1 Git 基线 + CI、默认拒绝 `.gitignore`、hooks | OpenCode | 未修改 |
| PR #2 / #4 外部 AI 审查入口 `docs/AI_REVIEW.md` | OpenCode | 未修改，仅链接引用 |
| PR #3 lockfile 恢复测试 | OpenCode | 未修改 |
| PR #5 DataRestore1 脱敏审计文档 | DSH | 未修改，仅链接引用 |
| PR #6 C 层 NCC 验证文档 | DSH | 未修改，仅链接引用 |
| PR #7 公开访问说明 + raw/jsDelivr 纯文本地址 | OpenCode | **未修改任何被 PR #7 触碰的文件**；本入口补充源码目录、固定 SHA 直链、Pages 站点与访问实测 |

本改动新增的文件：`docs/index.html`、`docs/AI_PORTAL.md`、`docs/.nojekyll`、`docs/ai-portal/manifest.json`、`docs/ai-portal/ACCESS_REPORT.md`。全部位于独立分支 `agent/public-ai-review-portal`，通过 PR 交付审核。

## 6. 文档现状说明（供审核者参考，本 PR 不修改）

- PR #7 已把 README / AI_REVIEW / SECURITY_AUDIT / STATE / TASKS 中的「私有仓库」表述更正为 public，并给出 6 个 raw 文档链接；这些文件属于 OpenCode 的成果，本 PR 不覆盖。
- `SECURITY_AUDIT.md` 已记录 DataRestore1 公开文档中仍有 1 个示例容器名与 3 个对象 SHA-256 前缀待 DSH 复核；本入口不引用这些标识符。
- 文档中的 `main` SHA 是「核对时」快照，仓库仍在活跃更新；最新 HEAD 请以 GitHub API 为准。
- 本入口的链接固定到 `8182720...`，该快照包含 PR #7 的文档修正。

## 7. GitHub Pages 启用后的复测

第 1 节第 15 项记录的是 Pages **启用前**的 404（API `GET /repos/.../pages` 同样 404，原因明确为站点未创建）。随后通过 API 启用 Pages：

```
POST /repos/dyxiguajun/NAS-Photo-Intelligence/pages
{"build_type":"legacy","source":{"branch":"agent/public-ai-review-portal","path":"/docs"}}
```

首次构建记录：`GET /pages/builds/latest` → `status: built`，`commit: a545204c705bcfdfc6481887a00fa142edf1b8ec`，`error: null`（构建耗时约 21 秒）。后续分支推送会触发新的构建，`latest` 中的 commit 会随之变化。

匿名复测（无凭证）：

| URL | HTTP | Content-Type |
|---|---:|---|
| `https://dyxiguajun.github.io/NAS-Photo-Intelligence/` | 200 | text/html |
| `.../AI_PORTAL.md` | 200 | text/markdown |
| `.../ai-portal/manifest.json` | 200 | application/json |
| `.../ai-portal/ACCESS_REPORT.md` | 200 | text/markdown |
| `.../ARCHITECTURE.md` | 200 | text/markdown |

`.nojekyll` 生效：`.md` 以 `text/markdown` 原样提供，便于纯文本抓取。`dyxiguajun.github.io/robots.txt` 返回 404，按 robots 约定该源无禁止规则。

**配置说明**：Pages 源指向本 PR 的独立分支，以便合并前验证；`main` 的文件未被修改。PR 合并后需由 Codex / OpenCode 决定是否把源改为 `main` + `/docs`，否则删除该分支会导致站点失效。这是仓库级站点配置，不是权限或可见性变更。
