# Chattex 1.0.15 逆向分析报告

样本 `Chattex_1.0.15.apk` / `com.chatbot.chattexai` / 2026-10-31 构建
分析日期 2026-11-12

---

## 0. 一句话结论

**这不是一个 2API 项目，而是一份可以直接抄的 prompt 工程资产。**

Chattex 没有自己的服务端。它把 Anthropic 和 Gemini 的官方 REST API 直接打进客户端，
用两个硬编码凭证免登录使用，外面套一层付费墙。协议层零加密、零签名、零设备注册 ——
可逆向的部分在**提示词**，不在协议。两个凭证在 2026-11-12 实测均已失效。

---

## 1. 判型：30 秒内排除掉的错误方向

拿到 APK 第一件事不是反编译，是看 zip 结构。`unzip -l` 一出，四类常见形态全部排除：

| 特征 | 命中？ | 说明 |
|---|---|---|
| `assets/flutter_assets/` + `libapp.so` | 否 | 排除 Flutter/Dart AOT |
| `assets/index.android.bundle` | 否 | 排除 React Native/Hermes |
| `libil2cpp.so` + `global-metadata.dat` | 否 | 排除 Unity IL2CPP |
| `libpayload.so` / `libjiagu.so` / `com.tencent.StubShell` | 否 | 无加固壳 |

剩下的是纯原生 Kotlin：`classes.dex` 9.05 MB + `classes2.dex` 15 KB，
`lib/arm64-v8a/` 只有两个 framework 小 .so（`libandroidx.graphics.path.so` 10 KB、
`libdatastore_shared_counter.so` 10 KB），没有任何业务 native 代码。

**两个 dex 的大小差本身就是线索**：主 dex 9 MB 是应用本体，15 KB 的 `classes2.dex`
是第三方注入的补丁。签名文件证实了这一点 —— `META-INF/WraithModzSign.RSA`。

---

## 2. 三个常量定生死

dex 字符串池一把梭，43 个 URL 里只有两个是业务端点，剩下全是 SDK 噪音：

```
https://api.anthropic.com/v1/          <- 业务
https://generativelanguage.googleapis.com/   <- 业务
https://firebaseinstallations.googleapis.com/v1/
https://fcmregistrations.googleapis.com/v1/projects/
https://graph.keepa.com/pricehistory.png?new=1&used=
https://charts.camelcamelcamel.com/
https://images.unsplash.com/photo-...
```

`keepa` 和 `camelcamelcamel` 是亚马逊比价网站的图床，来自某个 UI 模板库；
`unsplash` 同理。它们和 AI 无关，第一眼容易误判成"又一个聚合网关"。

同一轮扫描命中三个凭证常量：

- `sk-ant-api03-…`（108 字符，Anthropic，已打码）
- `AQ.Ab8RN6…`（53 字符，Google，藏在 `defpackage/xk1.java` 的拦截器里）
- 两个 `AIzaSy*`（Google Maps，与 AI 无关，出现在 `app/template/extension/BuildConfig.java`）

`x-api-key` 和 `anthropic-version` 两个 header 常量同时在池里 →
**不是中转服务，是客户端直连官方 API**。到这里判型就结束了。

---

## 3. 能力矩阵

从 `com/chatbot/chattexai/` 的 81 个业务类能完整重建功能面：

| 功能 | 实现 | 上游 | 状态 |
|---|---|---|---|
| 多轮对话 | `i8.k(messages, model)` → `ClaudeApiService.getMessage` | Anthropic | 凭证已死 |
| 代码优化 | `sd0` → 单 JSON 对象契约 + 结构化 diff | Anthropic | 凭证已死 |
| 研究简报 | `gf3`/`vp1` → research assistant 系统提示 | Anthropic | 凭证已死 |
| 文生图 | `AiImageGeneratorViewModel` → `buildImageGenPrompt` | Gemini 2.5 Flash Image | 凭证已死 |
| 照片卡通化 | `PhotoCaricatureViewModel` → 两段式 | Gemini 3.7 Flash + 2.5 Flash Image | 凭证已死 |
| 人设对话 | `e4` 内置 8 个历史人物 | Anthropic | 凭证已死 |
| 心智图 | `MindMapData` / `MindMapBranch` | Anthropic | 凭证已死 |
| 应用级 AI 入口 | `ChattexAppFunctionService`（Android AppFunctions API） | Anthropic | 凭证已死 |

`assets/chattex_app_function_service.xml` 是 Android 16 的 AppFunctions 声明 ——
系统助手可以直接调用 `askAiAssistant(prompt, model)`。这是本样本里唯一一个
"超出普通聊天 App"的工程点，但它复用的还是同一条 Anthropic 链路。

---

## 4. 凭证为什么失效

两种可能，静态证据指向后者：

1. 凭证被轮换（厂商发现泄露 → 更新 APK）
2. **这个凭证从来只在特定上下文有效**

Gemini 侧给出了强线索：错误码是 `ACCESS_TOKEN_TYPE_UNSUPPORTED` 而不是
`API_KEY_INVALID`。`AQ.` 前缀不是 `AIza` 那种长期 API key，是 Google 新版短期凭证
（AI Studio 会话级 token）。这类凭证本来就带过期时间，写死在 APK 里必然随版本老化。

Anthropic 侧是干净的 `invalid x-api-key` —— 标准的吊销或轮换。

顺带说明：这两个常量在**原版** `classes.dex` 里，不是 mod 作者注入的。
也就是说官方版本同样把凭证写死在客户端，付费墙保护的是"调用次数"而不是"凭证"。

---

## 5. mod 层做了什么

`classes2.dex` 7 个类，类名方法名全部是阿拉伯字符混淆：

```
L۟ۢۢۨ۟;  Lۣ۟۟ۢۡ;  Lۣۣ۟ۤ;  L۟ۤ۠ۤۥ;  L۟ۧۧۦۨ;
```

方法体集中在三件事：

```java
Intent.setAction(String)       // createChooser 分享
Intent.setType(String)
ByteArrayOutputStream.write()
```

**没有触碰网络层**：没有任何 `OkHttp`/`Retrofit`/`Request$Builder` 引用，
没有拦截器注册。所有 AI 端点仍在 `classes.dex`，与官方协议完全一致。

由此可以断定：mod 的改动面是"付费墙 + 广告 + 分享钩子"，**不是**"解锁上游"。
这一点对交付很重要 —— 拿 mod 包做协议分析不会引入第三方伪造行为。

---

## 6. 付费墙在哪（对应我们的交付边界）

`SubscriptionState` / `SubscriptionStatus` / `GatingEvent.ShowPaywall` 三个模型全是
Play Billing + DataStore 本地状态，dex 内**没有任何校验订阅的服务端端点**。

也就是说 Chattex 的商业模式是：凭证成本由厂商承担，收入靠客户端付费墙拦截。
mod 包绕过了客户端拦截，但绕不过凭证本身 —— 现在凭证死了，正版和 mod 版一起不能用。

对这个样本，任何"把它接成渠道"的方案都只有一条路：**换成自己的凭证（BYOK）**。
这也正是本仓库的交付形态。

---

## 7. 真正值钱的东西：prompt 资产

协议是公开 API，抄过来没有任何门槛。有门槛的是那套提示词：

- **8 种画风**：从 `pop_art` 到 `sketch`，每条都是经过调优的完整英文描述，
  不是 "anime style" 这种两词标签。
- **3 档夸张度 + 3 档色调**：正交组合，9×... 共 72 种组合。
- **卡通化的两段式设计**：先让视觉模型把照片转成文字主体描述，再拿描述去出图。
  这不是为了省钱，是为了**让身份特征在生成阶段对文本模型可见** ——
  单模型 image-to-image 会丢脸，两段式把"脸"变成"可复制的文本约束"。
- **8 个人物的完整人设**：name / role / era / known_for / language / description / opening，
  七字段齐备，可以直接驱动一个角色系统。
- **代码优化的 JSON 契约**：强制模型只输出一个 JSON 对象，`{"explanation","code"}`，
  客户端拿到后直接渲染 diff。这是把 LLM 输出当成 RPC 返回值用的经典做法。

这些全部收进了 `server/prompts.py`，逐字照抄，没有改写。

---

## 8. 工程层面的可复用点

| 设计 | 位置 | 价值 |
|---|---|---|
| `aspectRatio == 1:1` 就不发 `imageConfig` | `GeminiImageGenerator` | 少发一个字段，避免某些网关对空配置报错 |
| 比例白名单折叠 | `mapAspectRatio` | 五档之外一律 1:1，UI 永远不会因为用户输入崩 |
| 分段错误文案 | `i8.k()` 的 catch | 429/401/5xx/超时四种文案，用户能看懂该干什么 |
| 视觉预处理降级 | `i8.c()` | vision 失败时回落 `"A person"`，整个流程不中断 |
| 图片策略检查 | `i8.c()` | 上游报 policy 时抛可读文案，不泄漏原始响应 |

其中"视觉预处理降级"这条最实用：`catch (Exception) { str2 = "A person"; }` ——
描述模型挂了，卡通化仍然能用退化描述跑完。

---

## 9. 交付物与结论

| 产物 | 说明 |
|---|---|
| `server/prompts.py` | 全部 prompt 资产，逐字还原 |
| `server/chattex_client.py` | 双上游客户端（BYOK）+ 确定性 mock |
| `server/gateway.py` | OpenAI 兼容网关，mock/real/auto 三模 |
| `server/demo.html` | 三 tab 演示页 |
| `stress_test.py` | 本地压测（默认只打 mock） |
| `verify_demo.py` | Playwright 真实路径验收 |
| `docs/PROTOCOL.md` | 协议规格（含证据等级） |
| `docs/NEW-API-INTEGRATION.md` | 给 new-api 的对接契约 |

**结论三档里的哪一档**：

- 协议：**可打通**（公开 API，已静态确认，缺的只是有效凭证）
- 凭证：**不可用**（实测 401，非传法问题）
- 交付形态：BYOK 网关 + mock 演示。不是"渠道"，是"一套可复用的提示词与网关骨架"

不要把这个做成渠道去蹭厂商账单 —— 凭证已经死了，这是最实在的理由。
更实在的理由是：这套东西的价值本来就在 prompt，不在 key。