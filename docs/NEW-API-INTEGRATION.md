# Chattex 网关 → new-api 对接契约

这份文档给**另一个会话**执行，不依赖任何会话上下文。
读完应该能独立判断走哪条路、填哪些字段、验收什么。

---

## 1. 任务契约

| 项 | 内容 |
|---|---|
| 目标 | 把 `Chattex 2API` 网关接入 new-api，对外暴露 5 个模型 |
| 不可改动区 | new-api 仓库代码（除非选路线 B）、网关的 prompt 层 |
| 交付标准 | `/v1/chat/completions` 与 `/v1/images/generations` 在 new-api 侧真实可用，有验收截图或 curl 输出 |
| 前置条件 | 网关已跑起来（`/health` 返回 200）；已配置 `CHATTEX_ANTHROPIC_KEY` / `CHATTEX_GEMINI_KEY` |

---

## 2. 协议速查（5 条硬事实）

1. 网关是 **OpenAI 兼容**的：`/v1/chat/completions`（含 SSE）与 `/v1/images/generations`。
2. **Base URL 不带 `/v1`**。new-api 的 OpenAI 类型渠道用 `baseURL + 原始请求路径` 拼接，
   原始路径本身已带 `/v1`，写成 `http://host:18611/v1` 会变成 `/v1/v1/...` → 404。
3. 图像端点直接返回 OpenAI 形状的 `{"created":...,"data":[{"url"|"b64_json":...}]}`，
   不需要二次转换。
4. 所有响应带 `X-Chattex-Source: real|mock`。**用途：监控里区分真假产物**，
   new-api 不会转发这个头，需要在网关侧单独看 `/health` 的计数器。
5. 卡通化端点 `/v1/images/caricature` 是 **multipart + 直接回 PNG 字节流**，
   OpenAI 图片协议里没有对应形状 —— **不要接成渠道**，只能由自有前端直接调。

---

## 3. 路线 A：零代码（推荐）

在 new-api 后台新建渠道，不需要改一行 Go 代码。

### 渠道 1：对话

| 字段 | 值 |
|---|---|
| 类型 | OpenAI |
| 名称 | `Chattex Claude` |
| 分组 | 自定 |
| Base URL | `http://127.0.0.1:18611` |
| 密钥 | 网关的 `CHATTEX_GATEWAY_KEY`；网关未设该值时随便填（new-api 要求非空） |
| 模型 | `claude-sonnet-5`<br>`claude-opus-5`<br>`claude-haiku-4-5-20251001` |
| 模型重定向 | 不需要，网关认原始名 |
| 代理 | 留空（网关在本机） |

### 渠道 2：生图

| 字段 | 值 |
|---|---|
| 类型 | OpenAI |
| 名称 | `Chattex Gemini Image` |
| Base URL | `http://127.0.0.1:18611` |
| 密钥 | 同上 |
| 模型 | `gemini-2.5-flash-image` |

**渠道「测试」按钮会失败，属正常**。new-api 的测试按钮打的是 `/v1/chat/completions`，
而 `gemini-2.5-flash-image` 只服务图像端点。用生图 curl 验证，不要因为测试按钮红叉就以为接错了。

### 生图参数怎么传

new-api 的 OpenAI 图像适配器会透传已定义字段，`style` 是已定义字段之一
（原本语义是 `vivid`/`natural`）。网关把 `style` 复用来承载画风 id：

| new-api 请求 | 网关行为 |
|---|---|
| `"style": "pop_art"` | 命中 Chattex 画风表 |
| `"style": "vivid"` / `"natural"` / 其它 | 按 `none` 处理（写实） |
| `"size": "1024x1024"` | → `1:1` |
| `"size": "1024x1536"` | → `3:4` |
| `"size": "1792x1024"` | → `16:9` |
| `"size": "1024x1792"` | → `9:16` |
| 其它 `size` | → `1:1` |
| `"response_format": "url"` | 回 `http://<网关>/files/<id>.png` |
| `"response_format": "b64_json"` | 回 base64 |

完整映射表见 `gateway.py` 的 `SIZE_TO_RATIO`。

**自定义字段会被 new-api 丢弃**（`relaykit/dto/openai_image.go` 的 `ImageRequest`
在 `MarshalJSON` 里刻意不合并 `Extra`）。所以不要指望透传 `aspect_ratio`，
必须走 `size` + `style` 这两个已定义字段。

---

## 4. 路线 B：原生 Go 渠道（只有需要计费控制时才做）

不推荐。理由是：网关已经把协议差异吸收完了，原生渠道唯一的额外收益是
在 new-api 内部直接计费，而这个收益用「模型倍率」也能达到。

如果仍要做，需要动的文件（按 new-api v1.3.x 结构）：

| 文件 | 改动 |
|---|---|
| `constant/channel.go` | 末尾追加 `ChannelTypeChattex`，注意 iota 顺序，Dummy 必须是最后有效类型 |
| `constant/api_type.go` | 对应 `APIType` 值 |
| `common/api_type.go` | `ChannelType2APIType()` 加分支 |
| `relay/adaptor.go` | `GetAdaptor()` 加 case |
| `relay/channel/chattex/adaptor.go` | `ConvertOpenAIRequest` / `DoRequest` / `DoResponse` |
| `web/src/features/channels/constants.ts` | 前端渠道表两处 |
| `controller/twoapi_panel.go` | 若要在 2API 面板露出，加进 `TwoAPIChannelTypes` |

`DoRequest` 里如果自建 `http.Client`，就**不要**用 `channel.DoApiRequest`
（那条路径会加框架级的首字节超时与 body 重读）。

验收前跑一遍框架自带的 `TestChannelBaseURLsAligned`，`ChannelBaseURLs` 漏一个占位就整体错位。

---

## 5. 计费锚点

| 模型 | 上游真实成本 | 建议倍率 |
|---|---|---|
| `claude-sonnet-5` | 按 token（Anthropic 计费） | 按官方 × 1.2 |
| `claude-opus-5` | 按 token | 按官方 × 1.2 |
| `claude-haiku-4-5-20251001` | 按 token | 按官方 × 1.2 |
| `gemini-2.5-flash-image` | 按张（Gemini 图像输出 token） | 按次固定价 |

**注意**：`gemini-2.5-flash-image` 不在 new-api 内置的 `ImageGenerationModels` 表里
（该表只有 `dall-e-2/3`、`gpt-image-1`、`imagen-`、`flux-` 前缀）。
不在表内 → `IsImageGenerationModel` 判 false → 端点类型判定可能走文本分支。
**必须手工在「模型倍率」里配置该模型**，否则命中兜底倍率 37.5，扣费会离谱。

---

## 6. 故障排查表

| 现象 | 真实原因 | 修法 |
|---|---|---|
| 404 且路径里出现 `/v1/v1/` | Base URL 带了 `/v1` | 改成 `http://host:port` |
| 401 `invalid_api_key` | 网关设了 `CHATTEX_GATEWAY_KEY` 而 new-api 密钥填错 | 两边对齐 |
| 502 `upstream_auth_error` | 网关的 BYOK 凭证无效 | 检查 `/health` 的 `anthropic_key`/`gemini_key` |
| 502 `configuration_error` | 网关没配凭证但被强制 `CHATTEX_MODE=real` | 配 key 或改 `auto` |
| 429 `upstream_rate_limited` | 上游限流，不是网关 | 网关会原样透传 429，客户端应退避 |
| 503 `gateway_busy` | 并发打满且 `SLOT_WAIT` 超时 | 调大 `CHATTEX_MAX_CONCURRENCY` 或 `SLOT_WAIT` |
| 生图渠道测试按钮红叉 | 测试打的是 chat 端点 | 正常，用生图 curl 验 |
| 图片 URL 打不开 | `CHATTEX_PUBLIC_BASE` 为空，返回了相对路径 | 设成外部可达地址 |

---

## 7. 验收清单

- [ ] `GET /health` 返回 200，`effective` 两个字段都不是 `mock`（真实模式）
- [ ] `POST /v1/chat/completions`（stream=false）返回 `choices[0].message.content` 非空
- [ ] `POST /v1/chat/completions`（stream=true）能逐块收到 `data:` 并以 `[DONE]` 结束
- [ ] `POST /v1/images/generations` 返回的 `url` 能下载到真实 PNG（文件头 `89 50 4E 47`）
- [ ] new-api 侧用 5 个模型名各打一次，`/api/log` 有对应记录且扣费正常
- [ ] 断网重试：拔掉出口后请求返回 502 `upstream_server_error`，不是挂死
- [ ] 并发 8 打 30 秒，无 500，`/health` 的 `errors` 计数为 0

---

## 8. 回滚

网关是无状态进程，停掉即回滚。
new-api 侧只需把两个渠道 `status` 置 0（**不要**在 `PUT /api/channel/` 里带 `status` 字段，
它会回「无效的参数」；状态走单独的状态接口）。