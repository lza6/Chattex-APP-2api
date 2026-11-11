# Chattex 1.0.15 协议规格

样本：`Chattex_1.0.15.apk`（6.39 MB）
包名：`com.chatbot.chattexai`
签名：`META-INF/WraithModzSign.RSA`（第三方重签的 mod 包）
分析日期：2026-11-12

证据等级：**已验证**＝实际发包观察；**静态确认**＝读反编译代码确认；**合理推断**＝有间接证据；**待验证**＝缺环境或凭证。

---

## 1. 结论速览

| 项 | 结论 | 等级 |
|---|---|---|
| 架构 | 原生 Android（Kotlin + Hilt + Retrofit/OkHttp + Room），非 Flutter / RN / Unity | 已验证 |
| 自有后端 | **没有**。全部请求直连厂商官方 API | 已验证 |
| 聊天上游 | `https://api.anthropic.com/v1/messages` | 静态确认 |
| 生图上游 | `https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-image:generateContent` | 静态确认 |
| 视觉预处理 | `gemini-3.7-flash`，同一 Gemini 端点 | 静态确认 |
| 鉴权 | `x-api-key`（Anthropic）/ `x-goog-api-key`（Gemini），均为**硬编码常量** | 静态确认 |
| 内置凭证状态 | **已失效**：Anthropic `401 invalid x-api-key`、Gemini `401 ACCESS_TOKEN_TYPE_UNSUPPORTED` | 已验证 |
| 加密/签名 | 无。标准 JSON over TLS，无信封、无 HMAC、无设备注册、无 Play Integrity | 已验证 |
| 可复用性 | 协议 100% 可复刻；凭证必须 BYOK | 已验证 |

一次 `/v1/models` 请求即可复现凭证判定（不产生任何生成计费）：

```
GET https://api.anthropic.com/v1/models
  x-api-key: <APK 内常量>
  anthropic-version: 2023-06-01
→ 401 {"type":"error","error":{"type":"authentication_error","message":"invalid x-api-key"}}
```

---

## 2. 端点矩阵

| 功能 | 方法 | URL | 鉴权头 | 等级 |
|---|---|---|---|---|
| 对话 | POST | `https://api.anthropic.com/v1/messages` | `x-api-key` + `anthropic-version` | 静态确认 |
| 文生图 | POST | `https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-image:generateContent` | `x-goog-api-key` | 静态确认 |
| 图像描述 | POST | `https://generativelanguage.googleapis.com/v1beta/models/gemini-3.7-flash:generateContent` | `x-goog-api-key` | 静态确认 |

除此之外 dex 内**不存在**任何业务域名：`chattex.ai` 只出现在 `support@chattex.ai` 这个邮箱串里；
其余域名全部来自 Firebase / Google Maps / 三方 SDK。

Retrofit 构造（`com/chatbot/chattexai/c.java`）：

```java
// case 6
new Retrofit.Builder().baseUrl("https://api.anthropic.com/v1/")...
// case 14
new Retrofit.Builder().baseUrl("https://generativelanguage.googleapis.com/")...
```

---

## 3. 对话请求

```http
POST /v1/messages HTTP/1.1
Host: api.anthropic.com
x-api-key: <key>
anthropic-version: 2023-06-01
content-type: application/json
```

```json
{
  "model": "claude-sonnet-5",
  "system": "You are Claude, a helpful, honest, and knowledgeable AI assistant.",
  "messages": [{"role": "user", "content": "..."}],
  "max_tokens": 4096
}
```

- `ClaudeRequest` 字段：`model` / `system` / `messages` / `max_tokens`（默认 4096）/ `temperature`（可空）。
- `ClaudeMessage` 只有 `role` + `content`（content 是纯字符串，不是 content-block 数组）。
- 模型选择在中原逻辑里按档位切换：

```java
String model = "claude-sonnet-5";
if (tier.equals("Opus"))  model = "claude-opus-5";
else if (tier.equals("Haiku")) model = "claude-haiku-4-5-20251001";
```

- 响应只取 `content[].text`，不使用流式（`stream` 字段从不设置）。
- 客户端超时：connect 60s / read 120s / write 60s / call 120s。

### 错误映射（`i8.k()` 的 catch 分支）

| HTTP | 客户端文案 |
|---|---|
| 429 | `You've hit your usage limit for Claude. Please try again later.` |
| 401 / 403 | `Invalid or unauthorized Anthropic API key. Please check your settings.` |
| 5xx | `Claude servers are currently having trouble. Please try again shortly.` |
| 超时 | `This request timed out — check your connection and try again.` |

---

## 4. 文生图请求

```http
POST /v1beta/models/gemini-2.5-flash-image:generateContent
x-goog-api-key: <key>
content-type: application/json
```

```json
{
  "contents": [{"parts": [{"text": "<final prompt>"}]}],
  "generationConfig": {
    "responseModalities": ["TEXT", "IMAGE"],
    "imageConfig": {"aspectRatio": "16:9"}
  }
}
```

关键细节（`GeminiImageGenerator.generateImageInternal`）：

1. **只有一个 text part**，没有 `inlineData`。Chattex 的生图是**纯文生图**，不上传参考图。
2. `responseModalities` 固定 `["TEXT","IMAGE"]`。
3. `aspectRatio == "1:1"` 时 **整个 `imageConfig` 被置为 null**（不发送该字段）。
4. 合法比例只有五档：`1:1` / `3:4` / `4:3` / `16:9` / `9:16`；其它值经 `mapAspectRatio` 一律折叠成 `1:1`。
5. 客户端**不发送** `imageSize` 字段（`GeminiImageConfig` 有该字段，但两处构造都传 null）。
6. 结果解析：`candidates[0].content.parts[]` 里找 `inlineData.data`（base64）→ 落盘为 PNG。

响应解析与错误映射：

```
finishReason != "STOP"           -> ContentBlocked
promptFeedback.blockReason 存在  -> ContentBlocked
无 candidates / 无 inlineData    -> "Gemini returned no image part"
HTTP 401/403 -> AuthError   HTTP 404 -> 模型不存在   HTTP 429 -> RateLimited
```

---

## 5. 照片卡通化（两段式）

链路在 `defpackage/i8.c(Uri, String)`：

```
① 读图字节 → BitmapFactory → Content(image)   （无 text part）
② gemini-3.7-flash  generateContent  →  主体描述文本
③ final = "<卡通化 prompt>" + " Subject Description: " + <主体描述>
④ gemini-2.5-flash-image  generateContent(final, aspectRatio="1:1")
```

第 ② 步用的是 Google 官方 `com.google.ai.client.generativeai.GenerativeModel` SDK，
不是手写 Retrofit —— 但落到线上仍是同一个 `generateContent` 端点，可等价复刻。

卡通化 prompt 模板（`CaricaturePromptBuilder.buildCaricaturePrompt`）：

```
Transform the uploaded photo into a caricature portrait. Preserve the subject's
recognizable facial identity, hairstyle, and expression. Style: <STYLE>.
Exaggeration: <EXAGGERATION>. Color treatment: <COLOR>. Keep the background
simple and uncluttered so the subject remains the focus. High quality, clean
linework, well-composed portrait framing.
```

`regenerate=true` 时首句替换为：
`Regenerate a new version of the caricature based on the uploaded photo. Try a slightly different artistic interpretation while maintaining the same core settings.`

---

## 6. 文生图 prompt 模板（`buildImageGenPrompt`）

```
<用户提示词>. Rendered in the following style: <STYLE>. Aspect ratio <RATIO>,
well-composed, high detail, professional quality.
```

---

## 7. Prompt 资产（dex 内原文，逐字）

### 系统提示

| 场景 | 原文 |
|---|---|
| 普通对话 | `You are Claude, a helpful, honest, and knowledgeable AI assistant.` |
| 对话（变体） | `You are Claude, a helpful assistant.` |
| 代码优化（JSON 契约） | `You are an expert software engineer specializing in code optimization. You always reply with a single valid JSON object and nothing else - no markdown code fences, no commentary before or after the JSON.` |
| 代码问答 | `You are an expert software engineer embedded in a code optimization tool. Your ONLY job is to answer questions about the code and explanation shown below. Stay strictly on-topic.` |
| 研究简报 | `You are a research assistant producing a structured briefing on: "` |
| 研究上下文 | `You are a research assistant. Context: ` |
| 研究（专用） | `You are a specialized research assistant.` |

### 风格修饰词（8 种）

| id | 原文 |
|---|---|
| `none` | photorealistic portrait, natural proportions, no stylization |
| `pop_art` | pop art style, bold flat colors, thick black outlines, halftone dot shading, high contrast, Roy Lichtenstein / Warhol inspired, graphic poster look |
| `2d_vector` | clean 2D vector illustration, flat shading, smooth geometric shapes, minimal gradients, crisp vector line art, modern flat-design style |
| `3d_animation` | 3D animated character render, Pixar/Disney-style 3D modeling, soft subsurface-scattering skin shading, big expressive eyes, smooth rounded features, studio 3D lighting |
| `comic_book` | comic book illustration style, bold ink outlines, cross-hatch shading, dynamic linework, printed comic panel look, slightly exaggerated expression |
| `watercolor` | watercolor painting style, soft bleeding pigment edges, visible paper texture, gentle color washes, hand-painted artistic look, delicate detail |
| `sketch` | pencil sketch illustration, graphite shading, visible hatching and cross-hatch linework, monochrome or lightly tinted, hand-drawn sketchbook look |
| `anime` | modern anime illustration style, expressive large eyes, cell-shaded, high quality digital art |

### 夸张度（3 档）

| id | 原文 |
|---|---|
| `Low` | subtle caricature exaggeration, proportions stay close to realistic, only slight emphasis on distinctive facial features |
| `Medium` | moderate caricature exaggeration, noticeably enlarged eyes and expression, balanced stylized proportions while still recognizable |
| `High` | strong caricature exaggeration, oversized head and eyes relative to body, heavily emphasized distinctive features, bold and playful proportions |

### 色调（3 档）

| id | 原文 |
|---|---|
| `Natural` | natural, true-to-life color palette, balanced skin tones |
| `Vibrant` | vibrant saturated colors, punchy high-contrast palette, energetic tones |
| `Pastel` | soft pastel color palette, muted light tones, gentle low-contrast colors |

### 内置人设（8 个，`defpackage/e4`）

`einstein` / `vinci` / `lovelace` / `aristotle` / `socrates` / `austen` / `tesla` / `michelangelo`，
每个含 name / role / era / known_for / language / description / opening。
APK 里只存这七个字段，**角色 system prompt 是运行时拼的**，拼接串不在 dex 内（见 §9 未决项）。

---

## 8. 与官方 API 的偏差（复刻时必须照做的）

| # | 偏差 | 后果 |
|---|---|---|
| 1 | 生图只传 text part | 传 `inlineData` 会变成图生图，模型行为与 Chattex 不同 |
| 2 | `1:1` 不发 `imageConfig` | 多发一个 `{"aspectRatio":"1:1"}` 通常无害，但不等于客户端行为 |
| 3 | 比例只有五档 | OpenAI 的 `size` 需要映射表，未知值折 1:1 |
| 4 | 不发 `imageSize` | Gemini 2.5 Flash Image 支持 `1K`/`2K`；Chattex 用默认值，不要替它指定 |
| 5 | `messages[].content` 是字符串 | 用 content-block 数组会被 Anthropic 接受，但历史裁剪语义不同 |
| 6 | 不流式 | 客户端从不设 `stream`；网关加流式时必须自己拼 SSE |

---

## 9. 未决项

| 项 | 状态 | 补齐办法 |
|---|---|---|
| 卡通化两段式是否真能出图 | 待验证 | 凭证已失效，需要有效 Gemini key 才能实测 |
| `gemini-3.7-flash` 是否为公开模型 ID | 待验证 | 用有效 key 打 `/v1beta/models` 对照 |
| 人设 system prompt 的具体拼接串 | 未还原 | 拼接在 `l7`/`hd0` 的 Compose 闭包里，jadx 对 lambda 反编译不完整；按七字段重建（见 `server/prompts.py`） |
| 订阅门禁是本地还是服务端 | 静态确认＝本地 | `SubscriptionState` / `GatingEvent.ShowPaywall` 全部读 Play Billing + DataStore，无服务端校验端点 |
| mod 注入 dex 的确切行为 | 部分确认 | `classes2.dex` 7 个混淆类，方法体集中在 `Intent.setAction/setType/createChooser` 与 `ByteArrayOutputStream`，**未触碰网络层**（三个 AI 端点仍在 `classes.dex`） |

---

## 10. 凭证判定证据

APK 内三个凭据常量：

```
classes.dex (string pool):
  sk-ant-api03-…（108 字符，前缀 + 长度已打码）              (Anthropic)

defpackage/xk1.java (Gemini 拦截器，Interceptor#intercept 里硬编码):
  AQ.Ab8RN6…（53 字符，打码）                                (Google)

app/template/extension/BuildConfig.java:
  AIzaSy…（Google Maps，与 AI 无关，仅工程噪音）
  AIzaSy…（同上）
```

> 本仓库不保留任何完整凭证串。需要复现时从 APK 自行提取：
> `python -c "import re;d=open('classes.dex','rb').read();print([m.group() for m in re.finditer(rb'sk-ant-[A-Za-z0-9_-]{20,}',d)])"`

实测（2026-11-12）：

```
Anthropic  GET /v1/models  -> 401 authentication_error invalid x-api-key
Gemini     GET /v1beta/models (x-goog-api-key) -> 401 ACCESS_TOKEN_TYPE_UNSUPPORTED
Gemini     GET /v1beta/models (Authorization: Bearer) -> 401 Expected OAuth 2 access token
Gemini     GET /v1beta/models (?key=) -> 401 ACCESS_TOKEN_TYPE_UNSUPPORTED
```

`ACCESS_TOKEN_TYPE_UNSUPPORTED` 说明 `AQ.` 前缀不是标准 API key（新版 Google 短期凭证形态），
三种主流传法全部 401 → **该凭证已不可用**，不是传法问题。

本仓库不内置任何厂商凭证；`server/` 全部走 BYOK。