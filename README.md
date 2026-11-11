# Chattex 2API

从 `Chattex_1.0.15.apk`（`com.chatbot.chattexai`）还原的 OpenAI 兼容网关。

**先说清楚这是什么**：Chattex 没有自有后端，它直连 Anthropic Messages 和 Gemini
`generateContent` 两个公开 API，凭证硬编码在客户端。本仓库复刻的是它的**协议形状 +
提示词工程**，凭证一律 BYOK。APK 内那两个凭证 2026-11-12 实测已失效（401），
详见 `docs/PROTOCOL.md` §10。

---

## 30 秒跑起来

```bash
cd server
pip install fastapi uvicorn httpx pillow      # 已有则跳过
python gateway.py                              # 默认 127.0.0.1:18611
```

打开 `http://127.0.0.1:18611/`。没有配凭证时网关自动进 **mock** 模式，
页面顶部会有黄色提示条，所有产物都是本地确定性生成的。

接真实上游：

```bash
export CHATTEX_ANTHROPIC_KEY=sk-ant-...        # https://console.anthropic.com
export CHATTEX_GEMINI_KEY=AIza...              # https://aistudio.google.com/apikey
export CHATTEX_MODE=auto                       # auto | real | mock
python gateway.py
```

`/health` 会同时报告 `mode`（配置值）和 `effective`（按凭证可用性解析后的实际模式）。

---

## 能力

| 模型名 | 上游 | 说明 |
|---|---|---|
| `claude-sonnet-5` | Anthropic Messages | 默认对话模型 |
| `claude-opus-5` | Anthropic Messages | 高配档 |
| `claude-haiku-4-5-20251001` | Anthropic Messages | 低延迟档 |
| `gemini-2.5-flash-image` | Gemini generateContent | 文生图，纯 text→image |
| `gemini-3.7-flash` | Gemini generateContent | 视觉预处理（卡通化第一段） |

模型名沿用 Chattex 客户端里写死的字符串，未做改名。

---

## 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 模式、凭证状态、计数器 |
| GET | `/v1/models` | 模型表 |
| GET | `/v1/personas` | 内置 8 个人设 |
| POST | `/v1/chat/completions` | OpenAI 形状，支持 `stream:true` |
| POST | `/v1/images/generations` | OpenAI 形状，`response_format` 支持 `url` / `b64_json` |
| POST | `/v1/images/caricature` | Chattex 专有：照片 → 主体描述 → 卡通化（multipart） |
| GET | `/files/{id}` | 内存 blob 存储（URL 形式的产物，128 条 LRU） |

所有响应带 `X-Chattex-Source: real|mock`，永不混用。

### 对话

```bash
curl http://127.0.0.1:18611/v1/chat/completions \
  -H 'content-type: application/json' \
  -d '{"model":"claude-sonnet-5","messages":[{"role":"user","content":"hi"}],"stream":false}'
```

### 文生图

```bash
curl http://127.0.0.1:18611/v1/images/generations \
  -H 'content-type: application/json' \
  -d '{"prompt":"a cube on a dark table","style":"pop_art","aspect_ratio":"16:9","response_format":"b64_json"}'
```

`style` 接受 Chattex 的 8 个画风 id；`aspect_ratio` 接受 `1:1/3:4/4:3/16:9/9:16`；
也可用 OpenAI 的 `size` 字段（映射表见 `gateway.py: SIZE_TO_RATIO`），未知值折叠为 `1:1`。

### 卡通化

```bash
curl http://127.0.0.1:18611/v1/images/caricature \
  -F photo=@face.jpg \
  -F style_id=anime -F exaggeration=High -F color_style=Pastel
```

返回 PNG 字节流；`X-Chattex-Subject`（base64）带视觉得到的主体描述，
`X-Chattex-Prompt`（base64）带最终提示词，方便对账。
留空 `subject` 字段时才会调用 `gemini-3.7-flash` 做视觉预处理 —— 传入 subject 可省一跳。

---

## 实测数据

环境：Windows 10 / Python 3.14 / 本机 loopback / mock 模式 / `rounds=20`
测量时间 2026-11-12。`CHATTEX_MAX_CONCURRENCY=4`（默认）：

```
chat  c=1   20/20 100%   rps 294.4   p50  44.0ms  p95  62.7ms
chat  c=4   80/80 100%   rps 190.8   p50 260.4ms  p95 359.2ms
chat  c=8  160/160 100%  rps 132.6   p50 844.6ms  p95 965.8ms
image c=1   20/20 100%   rps 135.2   p50  90.1ms  p95 132.8ms
image c=4   80/80 100%   rps 145.1   p50 329.9ms  p95 490.0ms
image c=8  160/160 100%  rps 168.5   p50 489.9ms  p95 779.6ms
```

同一脚本在 `CHATTEX_MAX_CONCURRENCY=32` 下的 chat 对照：

```
chat  c=1   rps 355.9   p50  33.6ms
chat  c=4   rps 194.1   p50 276.5ms
chat  c=8   rps 214.0   p50 490.7ms
```

读法：p50 随并发单调上升、c=8 时放宽闸门使吞吐 +61% 且 p50 降 42%
→ **第一瓶颈是信号量，第二瓶颈是 Python 单进程**。这不是上游的表现，
mock 模式下没有上游。真实上游吞吐**未测**（无有效凭证），标记待验证。

闸门契约（`MAX_CONCURRENCY=2` + `SLOT_WAIT=0` + 12 并发，实测）：

```
codes: {200: 2, 503: 10}     -> 恰好 2 个进闸、10 个立即 503
```

确定性断言（mock 必过）：

```
chat determinism: PASS (1 distinct replies)
image determinism: PASS (1 distinct payloads)
```

```bash
python stress_test.py --allow-real --kinds chat --rounds 2 --concurrency 1,2
```

`--allow-real` 是硬性开关，不加时检测到 real 模式会直接退出码 2 —— 防止误烧额度。

浏览器验收（Playwright 真实 Chromium，12 项）：

```
python verify_demo.py
  PASS  mode badge resolved
  PASS  mock banner visible
  PASS  health panel filled
  PASS  persona picker populated
  PASS  style picker populated
  PASS  chat reply rendered
  PASS  chat reply came from gateway
  PASS  streaming reply rendered
  PASS  generated image decoded
  PASS  image source labelled
  PASS  caricature image decoded
  PASS  no console errors
```

---

## 配置

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `CHATTEX_MODE` | `auto` | `auto` 按凭证可用性解析；`real`/`mock` 强制 |
| `CHATTEX_ANTHROPIC_KEY` | — | BYOK |
| `CHATTEX_GEMINI_KEY` | — | BYOK |
| `CHATTEX_GATEWAY_KEY` | 空 | 非空时 `/v1/*` 要求 `Authorization: Bearer <key>` |
| `CHATTEX_MAX_CONCURRENCY` | `4` | 上游并发闸门 |
| `CHATTEX_SLOT_WAIT` | `30` | 等槽位超时（秒）；`0` 表示立即 503 |
| `CHATTEX_MAX_BODY` | 24 MB | 请求体上限 |
| `CHATTEX_MAX_UPLOAD` | 12 MB | 上传图片上限 |
| `CHATTEX_PUBLIC_BASE` | 空 | 图片 URL 的前缀，空则返回相对路径 |
| `CHATTEX_PORT` | `18611` | 监听端口 |

---

## 文件清单

```
Chattex_1.0.15.apk          原始样本
server/prompts.py           全部 prompt 资产（逐字还原）
server/chattex_client.py    双上游客户端 + 确定性 mock
server/gateway.py           OpenAI 兼容网关
server/demo.html            演示页（三 tab）
stress_test.py              压测（默认只打 mock）
test_edges.py               29 项边界契约（畸形输入必须 4xx，不能 500）
verify_demo.py              Playwright 浏览器验收
docs/PROTOCOL.md            协议规格（含证据等级与实测凭证判定）
docs/REVERSE-REPORT.md      逆向分析报告
docs/NEW-API-INTEGRATION.md 对接 new-api 的契约（渠道字段表 + 故障排查）
docs/HANDOFF-PROMPT.md      可直接粘贴给新会话的交接 Prompt
jadx-out/ jadx-bad/         反编译产物（jadx 1.5.6，含 --show-bad-code 回退）
work/ evidence/             原始证据（dex 字符串、模型清单、截图）
```

---

## 风险与限制

1. **凭证已死**。APK 内 Anthropic 与 Gemini 凭证 2026-11-12 实测 401，本仓库不包含任何厂商凭证。
2. **真实链路未实测**。没有有效 key，`real` 分支的请求/响应只做过静态确认，标记待验证。
3. **`gemini-3.7-flash` 模型 ID 待验证**。该字符串来自 APK，是否仍是 Google 公开模型未经实测。
4. **人设 system prompt 是重建的**。APK 只存七个字段，拼接串在 Compose lambda 里未还原完整
   （见 `docs/PROTOCOL.md` §9）。重建版在 `prompts.py: build_persona_system`。
5. **mock 产物不是真实模型输出**。响应头与页面横幅双重标注，不可用于效果评估。
6. **`/files/{id}` 是内存存储**，进程重启即失效；生产环境换成对象存储并把 `CHATTEX_PUBLIC_BASE` 指向它。
7. **模型名沿用客户端写死的字符串**，Anthropic / Google 侧是否接受需实测；被拒时按上游错误处理。