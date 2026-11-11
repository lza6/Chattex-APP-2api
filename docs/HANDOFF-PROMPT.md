# Chattex 2API · new-api 对接会话启动 Prompt

把下面整段粘给一个**没有本会话上下文**的新会话，它就能独立完成对接。
本文件与 `docs/NEW-API-INTEGRATION.md` 配套：前者是"怎么开工"，后者是"填哪张表"。

---

## 可直接粘贴的 Prompt

```
任务：把 Chattex 2API 网关接进 new-api，让 5 个模型在 new-api 侧真实可用。

## 环境事实（不要重新验证，直接采信）
- 网关代码：Desktop/逆向产物/Chattex/server/，入口 gateway.py，默认监听 127.0.0.1:18611
- 启动：cd server && CHATTEX_MODE=mock python gateway.py
- 真实模式需要 CHATTEX_ANTHROPIC_KEY 与 CHATTEX_GEMINI_KEY（BYOK，仓库不含任何厂商凭证）
- new-api 仓库：New-Api-tingfeng（Go + React，分支 main）
- 网关是 OpenAI 兼容的，不是私有协议

## 硬性约束
1. Base URL 只填 http://127.0.0.1:18611，不带 /v1。
   new-api 的 OpenAI 渠道用 baseURL + 原始请求路径拼接，原始路径已含 /v1，
   多写一层会变成 /v1/v1/... → 404。
2. 不要改 new-api 仓库代码。走"零代码渠道"路线即可（见 docs/NEW-API-INTEGRATION.md §3）。
3. 不要透传自定义字段。new-api 的 ImageRequest 在 MarshalJSON 里刻意不合并 Extra，
   aspect_ratio 这类字段会被丢弃；画风走 style，尺寸走 size，这两个是已定义字段。
4. gemini-2.5-flash-image 不在 new-api 的 ImageGenerationModels 内置表里，
   必须手工配模型倍率，否则命中兜底倍率 37.5（比常规高一个数量级）。
5. 图像渠道的"测试"按钮会红叉——它打的是 /v1/chat/completions。用生图 curl 验，不要因此判失败。
6. 改渠道状态用单独的状态接口；PUT /api/channel/ 带 status 字段会回"无效的参数"。

## 验收标准（缺一不可，每条都要贴真实输出）
- GET /health → 200，effective.chat 与 effective.image 都不是 mock
- chat 非流式：curl 拿到 choices[0].message.content 非空
- chat 流式：能逐块收到 data: 且以 [DONE] 结束
- 生图：返回的 url 能下载到真实 PNG，文件头 89 50 4E 47
- new-api /api/log 有 5 个模型的调用记录，扣费正常
- 并发 8 打 30 秒无 500，/health 的 errors 计数保持 0

## 先做这三件事，别一上来就建渠道
1. cd server && python gateway.py，确认 /health 返回 200
2. 跑 python test_edges.py（29 项边界契约，必须全 PASS）—— 网关自身有毛病时先修网关，别去 new-api 侧找原因
3. 按 docs/NEW-API-INTEGRATION.md §3 建两个渠道，先只用 curl 验证网关，再验证 new-api

## 反模式（本类任务踩过的）
- 不要因为 new-api 渠道测试按钮红就重建渠道三次，那是测试端点的语义问题
- 不要把 503 gateway_busy 当成网关坏了：那是并发闸门在工作，调 CHATTEX_MAX_CONCURRENCY 或 CHATTEX_SLOT_WAIT
- 不要把 502 upstream_auth_error 归因到网关：那是 BYOK 凭证无效，先看 /health 的 anthropic_key / gemini_key
- 不要在网关还在 mock 模式时宣布对接完成：/health 的 effective 字段是唯一的判定依据
```

---

## 交接事实速查（给人工核对用）

| 项 | 值 |
|---|---|
| 网关入口 | `server/gateway.py`，`python gateway.py` |
| 默认端口 | `18611`（可用 `CHATTEX_PORT` 改） |
| 模式 | `auto`（默认）/ `real` / `mock` |
| 判定真实模式的唯一依据 | `GET /health` → `effective.chat` / `effective.image` |
| 产物来源标注 | 响应头 `X-Chattex-Source: real\|mock` |
| 边界契约 | `python test_edges.py`（29 项） |
| 压测 | `python stress_test.py --rounds 20 --concurrency 1,4,8` |
| 浏览器验收 | `python verify_demo.py`（Playwright，12 项） |
| 对话模型 | `claude-sonnet-5` / `claude-opus-5` / `claude-haiku-4-5-20251001` |
| 图像模型 | `gemini-2.5-flash-image` |
| 视觉模型 | `gemini-3.7-flash`（卡通化第一段，不单独对外） |

### 已知未验证项（交接时必须一起说清楚）

1. **真实上游链路没跑通过**。APK 里的凭证 2026-11-12 实测 401，仓库是 BYOK，谁配 key 谁验证。
2. **`gemini-3.7-flash` 模型 ID 未验证**。字符串抄自 APK，接上有效 key 后应先打 `/v1beta/models` 对一遍。
3. **`claude-sonnet-5` / `claude-opus-5` 未验证**。同上，可能已被上游改名或下线。
4. **卡通化两段式的端到端未实测**。只有 mock 路径跑过。

这四条不是"大概可能有问题"，是**确定没测**。对接方接手后第一件事就是补这四项。