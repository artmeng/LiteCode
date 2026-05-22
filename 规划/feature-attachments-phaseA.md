# 对话附件支持 + Model 配置增强（Phase A 完整计划 · v2）

> 项目：emperor-agent · 状态：✅ 计划已敲定，待实施
> 版本：2026-05-08（**v2：vision UI 简化为"测试驱动 + 👁 徽章"**）
> 关联 commits：`2cad8c8`（Week 1 上下文治理+错误恢复）、`9d2d2f3`（Week 2 Checkpoint）

---

## v2 与 v1 的差异

| 维度 | v1（弃） | v2（采纳） |
|---|---|---|
| 视觉能力字段 | `supports_vision: bool \| None` 三态 | `supports_vision: bool`（默认 false） |
| 用户意图层 | UI 三个 pill：自动 / 强开 / 强关 | **不暴露手动开关**——只能通过"测试视觉"激活 |
| ModelEntry 列表渲染 | 无视觉标记 | 已激活的 entry 名称右侧出现 **👁 徽章** |
| 配置 payload | `supportsVision` + `supportsVisionOverride` 两字段 | 仅 `supportsVision: boolean` |
| 测试视觉成功后行为 | 仅显示 ✓ 结果 | **自动把 `supports_vision=true` 持久化到 `model_config.json`** |
| 编辑表单空间 | `.vision-row`（30 行模板 + pill 按钮 + 实际生效提示） | **删除整块**；只保留底部"连通测试"区 |
| 心智模型 | 用户要懂"spec 默认 vs 用户覆写 vs 实际生效" | 一个状态：测过→👁；没测过→无图标 |

**节省**：约 -120 行代码、UI 减一行、零继承逻辑。

---

## 目录

1. [Context 与目标](#context-与目标)
2. [已敲定决策](#已敲定决策)
3. [关键探查结论](#关键探查结论)
4. [数据模型](#数据模型)
5. [后端改造](#后端改造)
6. [前端改造](#前端改造)
7. [旧路径兼容](#旧路径兼容)
8. [文件清单 & 行数估计](#文件清单--行数估计)
9. [验证 Checklist](#验证-checklist)
10. [实施顺序](#实施顺序)
11. [不在本次范围](#不在本次范围)

---

## Context 与目标

Week 1+2 的稳定性骨架已上线。本计划范围：

1. **图片附件直传** —— vision-capable 模型直接看图
2. **文档附件抽文本** —— 任何模型都能基于服务端抽出的文本回答
3. **ChatUI 完整支持** —— Composer 拖拽/选择 + 草稿条 + 气泡缩略图 + 历史持久化
4. **ModelPanel 视觉徽章** —— entry 列表用 👁 标记"测试通过"的视觉条目，激活由"测试视觉"按钮单一触发
5. **连通测试** —— ModelPanel 内置"测试文本""测试视觉"两个按钮一键验证 entry 是否能跑

不做 Anthropic 原生 PDF block、office 文档、远程 URL 图片、音视频。

---

## 已敲定决策

| 项 | 选择 | 备注 |
|---|---|---|
| 内部消息格式 | OpenAI 多模态：`content: list[block]`（text / image_url） | 转 Anthropic 在 provider 内部翻译 |
| 文件存储 | 落盘 `memory/attachments/YYYY-MM/{hash8}-{name}.{ext}` | `memory/` 已 gitignore |
| 文档处理 | 上传时同步抽文本写 sidecar `<path>.txt`；发消息时拼进 user content | 同时保留磁盘原件供 `read_file` 兜底 |
| 上传通道 | HTTP `POST /api/attachments`（multipart） → 返回引用 ID | WS 发消息时只带 ID |
| 不支持 vision 的 provider 选了图片 | 后端兜底替换为占位文字 | 前端按钮仍可点，提示模型不支持 |
| **Vision 标记机制** | **`ModelEntry.supports_vision: bool`（默认 false），仅由"测试视觉"成功时自动写入 true** | **v2 核心** |
| **ModelPanel UI** | 编辑表单底部"连通测试"区只有两个按钮；entry 列表项 👁 徽章渲染 | **v2 简化** |
| 单文件上限 | 图片 10MB / 文档 25MB | |
| 单条消息附件数 | ≤5 | |
| 支持的 mime | `image/png` `image/jpeg` `image/webp` `image/gif`；`application/pdf`；`text/*`；`application/json` `text/csv` | |

---

## 关键探查结论

| 已有能力 | 文件:行 | 含义 |
|---|---|---|
| `actionAssets.attach` 已注册 + `assets/actions/action-attach.png` 存在 | `webui/src/assets.ts:62`、Composer.vue:78-80 | UI 按钮位置已就位、只缺 handler |
| `memory.py:_json_safe` 已能序列化任意 dict/list | `agent/memory.py:137-150` | `history.jsonl` 持久化 multimodal 零改 |
| `openai_compat.py:_sanitize_messages` 透传 `content` | `agent/providers/openai_compat.py:95-120` | OpenAI 兼容侧的 vision 走原生格式即可 |
| `anthropic_provider._convert_messages` 把 user content 当字符串 | `agent/providers/anthropic_provider.py:99-123`（line 121 `content or "(empty)"`） | **必须**加 block 转换器 |
| `ReadFileTool` workspace=root、`memory/` 在内 | `agent/tools/filesystem.py:23-36` | 主代理可用 `read_file` 读 sidecar 文本 |
| `webui.py` 的 `import_skills` 已用 multipart | `agent/webui.py:180-212` | 上传路由可照抄此模式 |
| `_handle_ws_text` 仅认 `{type:"message", content:str}` | `agent/webui.py:87-118` | 发消息流要扩 `attachments: string[]` |
| `ProviderSpec` 无 `supports_vision` | `agent/providers/registry.py:23-57` | `supports_prompt_caching` 是先例（**spec 不必新增字段；v2 把视觉能力收敛到 entry 级**） |
| `ModelEntry` 字段已支持 entry 级覆写 | `agent/model_config.py:81-96` | 加 `supports_vision: bool` 无破坏 |
| `build_provider_snapshot` 已能按 `model_override` 临时构造 snapshot | `agent/model_config.py:168-208` | 连通测试路由直接复用 |
| `save_model_config` 已是入口 | `agent/model_config.py:157` | "测试视觉成功后自动持久化"调它即可 |

> **v2 重要决策**：`ProviderSpec` 不加 `supports_vision`。理由：单用户场景下 spec 默认值反而让"为何 entry 已勾视觉但模型不识图"这种 bug 难以排查。**唯一真理来源 = 测试结果**。`provider_options()` 不输出任何视觉相关字段。

---

## 数据模型

### `AttachmentRef`（前后端共享）

```ts
// frontend (webui/src/types.ts)
export interface AttachmentRef {
  id: string             // "att_2026-05_abc12345"
  name: string
  mime: string
  size: number
  kind: 'image' | 'document' | 'text'
  hasText: boolean
  hasImage: boolean
  path: string           // 相对仓库根
  textPath?: string
}
```

```python
# backend (agent/attachments.py)
@dataclass(frozen=True)
class AttachmentRef:
    id: str
    name: str
    mime: str
    size: int
    kind: str
    has_text: bool
    has_image: bool
    rel_path: str
    text_rel_path: str | None
```

### `ModelEntry` 新增字段（v2 简化）

```python
# agent/model_config.py
@dataclass(frozen=True)
class ModelEntry:
    # ... 原有字段
    supports_vision: bool = False    # ← v2：仅由"测试视觉"成功时自动置 true
```

```ts
// webui/src/types.ts
export interface ModelEntry {
  // ... 原有字段
  supportsVision?: boolean             // 默认 false
}
```

### `ProviderSnapshot` 新增字段

```python
@dataclass
class ProviderSnapshot:
    # ... 原有
    supports_vision: bool = False        # 直接读 entry.supports_vision
    entry_name: str = ""
```

### user message content（内部统一格式）

```jsonc
// 普通文本
{"role":"user","content":"hello"}

// 带附件（OpenAI 多模态格式）
{
  "role":"user",
  "content":[
    {"type":"text","text":"看图\n\n[附件 report.pdf 提取文本]\n第一章...\n[/附件]\n\n[已落盘: memory/attachments/2026-05/abc12345-report.pdf]"},
    {"type":"image_url","image_url":{"url":"data:image/png;base64,iVBORw0..."}}
  ],
  "attachments":["att_2026-05_abc12345"]   // 内部追溯，发给 LLM 前剥离
}
```

### `ModelTestResult`（API 返回）

```ts
export interface ModelTestResult {
  ok: boolean
  kind: 'text' | 'vision'
  latencyMs?: number
  model?: string
  provider?: string
  sample?: string             // 模型回复前 200 字符
  finishReason?: string
  error?: string
  visionMarked?: boolean      // v2 新增：vision 测试成功时后端是否已把 supports_vision 写入 true
}
```

---

## 后端改造

### 1. `agent/attachments.py`（新建，~180 行）

```python
"""附件落盘 + mime 校验 + PDF / 文本抽取 + 引用读写。"""
ALLOWED_IMAGE_MIMES = {"image/png","image/jpeg","image/webp","image/gif"}
ALLOWED_DOC_MIMES   = {"application/pdf","application/json","text/csv","text/plain","text/markdown"}
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_DOC_BYTES   = 25 * 1024 * 1024
TEXT_INLINE_LIMIT = 50_000

@dataclass(frozen=True)
class AttachmentRef:
    id: str; name: str; mime: str; size: int
    kind: str; has_text: bool; has_image: bool
    rel_path: str; text_rel_path: str | None

class AttachmentStore:
    def __init__(self, root: Path):
        self.root = root
        self.base = root / "memory" / "attachments"
        self._cache: dict[str, AttachmentRef] = {}     # LRU(64)

    def save(self, *, raw: bytes, name: str, mime: str) -> AttachmentRef:
        # 1) 校验 mime / size
        # 2) hash8 = sha256(raw)[:8]; month = "YYYY-MM"
        # 3) 写 self.base/{month}/{hash8}-{safe_name}.{ext}
        # 4) image/* → kind="image"；application/pdf → 抽 sidecar；text/* → 拷 sidecar
        # 5) 返回 AttachmentRef + 缓存

    def get(self, att_id: str) -> AttachmentRef | None:
        # 缓存 hit / 走 glob 找 month/hash8-*.{ext}

    def read_bytes(self, ref: AttachmentRef) -> bytes: ...
    def read_text(self, ref: AttachmentRef, limit: int = TEXT_INLINE_LIMIT) -> str: ...

def _extract_pdf_text(raw: bytes) -> str | None:
    try:
        from pypdf import PdfReader
        from io import BytesIO
        reader = PdfReader(BytesIO(raw))
        return "\n\n".join((p.extract_text() or "") for p in reader.pages).strip() or None
    except Exception as exc:
        logger.warning(f"pdf extract failed: {exc}")
        return None

def _safe_name(name: str) -> str:
    """去路径分隔符与 ascii 控制字符；保 unicode（中文文件名合法）。"""

def encode_for_openai_block(ref: AttachmentRef, store: AttachmentStore) -> dict:
    raw = store.read_bytes(ref)
    b64 = base64.b64encode(raw).decode("ascii")
    return {"type":"image_url","image_url":{"url": f"data:{ref.mime};base64,{b64}"}}
```

**索引方案**：不持久化索引，每次 `get(att_id)` 用 `att_id`（含 month + hash8）走 glob 反查；LRU(64) 防热路径反复 stat。

### 2. `agent/providers/registry.py`（v2：**不动**）

> v2 决定不在 spec 上加 `supports_vision`。registry 完全保持现状。`provider_options()` 也不输出任何视觉字段。

### 3. `agent/model_config.py`

**(a) `ModelEntry` 加字段**：
```python
supports_vision: bool = False
```

**(b) `_parse_entry` 一行解析**：
```python
return ModelEntry(
    # ...
    supports_vision=bool(item.get("supportsVision", False)),
)
```

**(c) `build_provider_snapshot` 直接透传**：
```python
return ProviderSnapshot(
    # ...
    supports_vision=entry.supports_vision,
    entry_name=entry.name,
)
```

**(d) `model_config_payload.current` 输出**：
```python
"current": {
    # ...
    "supportsVision": snapshot.supports_vision,
}
```

**(e) `model_config_payload.config.models[]` 持久化字段**：保存时 entry dict 携带 `supportsVision: bool` 即可（既有 `_normalized_raw` 自动透传字段）。

**(f) 新增辅助函数 `mark_entry_vision(root, entry_name, value=True)`**（供测试路由调用）：
```python
def mark_entry_vision(root: Path, entry_name: str, value: bool = True) -> ModelConfig:
    """把指定 entry 的 supportsVision 字段写入 model_config.json，原子保存。"""
    config = load_model_config(root)
    raw = copy.deepcopy(config.raw)
    found = False
    for m in raw.get("models", []):
        if m.get("name") == entry_name:
            m["supportsVision"] = value
            found = True
            break
    if not found:
        raise ValueError(f"entry {entry_name!r} not found")
    return save_model_config(root, raw)
```

### 4. `agent/providers/factory.py`

```python
@dataclass
class ProviderSnapshot:
    # ... 原有
    supports_vision: bool = False
    entry_name: str = ""
```

### 5. `agent/loop.py`

`refresh_model_config` 末尾：
```python
self.supports_vision = snapshot.supports_vision
```

### 6. `agent/webui.py`

#### (a) `__init__` 注入 store
```python
from .attachments import AttachmentStore
self.attachments = AttachmentStore(self.root)
```

#### (b) `POST /api/attachments` 上传
```python
async def upload_attachment(self, request):
    reader = await request.multipart()
    field = await reader.next()
    if field is None or field.name != "file":
        return self._json({"error":"missing file field"}, status=400)
    raw = await field.read(decode=False)
    name = field.filename or "unnamed"
    mime = field.headers.get("Content-Type") or "application/octet-stream"
    try:
        ref = self.attachments.save(raw=raw, name=name, mime=mime)
    except ValueError as exc:
        return self._json({"error": str(exc)}, status=400)
    return self._json(_ref_to_json(ref))
```

#### (c) `GET /api/attachments/{id}/raw`
```python
async def attachment_raw(self, request):
    ref = self.attachments.get(request.match_info["id"])
    if ref is None:
        return web.Response(status=404)
    return web.FileResponse(self.attachments.root / ref.rel_path,
                            headers={"Content-Type": ref.mime})
```

#### (d) `_handle_ws_text` 扩 attachments
```python
text = payload.get("content") or ""
attachment_ids = payload.get("attachments") or []
content = self._build_user_content(text, attachment_ids)
self.history.append({"role":"user", "content": content, "attachments": attachment_ids})
self.loop.memory.append_history("user", {"content": content, "attachments": attachment_ids})
```

#### (e) `_build_user_content` helper
```python
def _build_user_content(self, text, attachment_ids):
    if not attachment_ids:
        return text
    refs = [r for r in (self.attachments.get(a) for a in attachment_ids) if r]
    image_blocks, text_pieces = [], [text]
    supports_vision = self.loop.supports_vision
    for ref in refs:
        if ref.kind == "image":
            if supports_vision:
                image_blocks.append(encode_for_openai_block(ref, self.attachments))
            else:
                text_pieces.append(f"\n[图片附件 {ref.name}（当前模型未标记视觉，已忽略；可在 /model 测试视觉激活）]")
        elif ref.has_text:
            txt = self.attachments.read_text(ref)
            text_pieces.append(f"\n\n[附件 {ref.name} 提取文本]\n{txt}\n[/附件]")
        else:
            text_pieces.append(f"\n[附件 {ref.name} 已落盘: {ref.rel_path}（用 read_file 读取）]")
        text_pieces.append(f"\n[已落盘: {ref.rel_path}]")
    full_text = "".join(text_pieces).strip()
    if image_blocks:
        return [{"type":"text","text": full_text}] + image_blocks
    return full_text
```

#### (f) `POST /api/model-test` —— v2 关键路由

```python
import time
import base64
from .model_config import build_provider_snapshot, mark_entry_vision

# 2x2 纯红 PNG（约 70 bytes base64），视觉探测样本
_PROBE_PNG_RED_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAFklEQVR4AWPwl3jzn4HhPwM2D"
    "JVAaQAJ7QgRZD2vqQAAAABJRU5ErkJggg=="
)

async def model_test(self, request):
    body = await request.json()
    entry_name = (body.get("entryName") or "").strip()
    kind = body.get("kind") or "text"
    if kind not in ("text", "vision"):
        return self._json({"ok": False, "error": "kind must be text|vision"}, status=400)
    if not entry_name:
        return self._json({"ok": False, "error": "entryName required"}, status=400)

    try:
        snap = build_provider_snapshot(self.root, model_override=entry_name)
    except Exception as exc:
        return self._json({"ok": False, "kind": kind, "error": f"snapshot failed: {exc}"})

    if kind == "vision":
        messages = [{
            "role": "user",
            "content": [
                {"type": "text",
                 "text": "Reply with ONE English word only: what is the dominant color of this image?"},
                {"type": "image_url",
                 "image_url": {"url": f"data:image/png;base64,{_PROBE_PNG_RED_BASE64}"}},
            ],
        }]
    else:
        messages = [{"role": "user", "content": "Reply with exactly one word: pong"}]

    started = time.monotonic()
    try:
        resp = await snap.provider.chat(
            messages=messages, tools=None, model=snap.model,
            max_tokens=64, temperature=0.0, reasoning_effort=None,
        )
    except Exception as exc:
        return self._json({
            "ok": False, "kind": kind, "error": str(exc),
            "latencyMs": int((time.monotonic() - started) * 1000),
            "model": snap.model, "provider": snap.provider_name,
        })

    latency = int((time.monotonic() - started) * 1000)
    sample = (resp.content or "").strip()[:200]
    if kind == "vision":
        ok = bool(sample) and any(k in sample.lower() for k in ("red", "红"))
    else:
        ok = bool(sample) and "pong" in sample.lower()

    payload = {
        "ok": ok,
        "kind": kind,
        "latencyMs": latency,
        "model": snap.model,
        "provider": snap.provider_name,
        "sample": sample,
        "finishReason": getattr(resp, "finish_reason", "stop"),
    }

    # ★ v2 关键：vision 测试成功 → 把 entry.supports_vision 持久化为 true
    if kind == "vision" and ok:
        try:
            mark_entry_vision(self.root, entry_name, value=True)
            self.loop.refresh_model_config()        # 同步 in-memory 状态
            payload["visionMarked"] = True
        except Exception as exc:
            logger.warning(f"failed to mark entry vision: {exc}")
            payload["visionMarked"] = False

    return self._json(payload)
```

注册路由：
```python
web.post("/api/attachments", state.upload_attachment),
web.get("/api/attachments/{id}/raw", state.attachment_raw),
web.post("/api/model-test", state.model_test),
```

#### (g) `bootstrap` 历史回填 attachment refs

扫 `unarchivedHistory` 中 role=user 条目，含 `attachments` 字段时逐个 `AttachmentStore.get(id)` 转 ref，附在条目返回；找不到的（手删）忽略。

### 7. `agent/providers/anthropic_provider.py` 加 block 转换

```python
def _content_to_anthropic(self, content):
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content)
    out = []
    for block in content:
        btype = block.get("type")
        if btype == "text":
            out.append({"type":"text", "text": block.get("text", "")})
        elif btype == "image_url":
            url = (block.get("image_url") or {}).get("url", "")
            if url.startswith("data:"):
                meta, data = url.split(",", 1)
                media_type = meta.split(";")[0].split(":", 1)[1]
                out.append({
                    "type": "image",
                    "source": {"type":"base64","media_type":media_type,"data":data},
                })
    return out
```

把 `line 121` 的 `content or "(empty)"` 改为 `self._content_to_anthropic(content) or "(empty)"`。

### 8. `agent/providers/openai_compat.py`

确认 `_sanitize_messages` 白名单已剔除 `attachments` key。

### 9. `agent/runner.py` cap/shrink 兼容多模态

```python
@staticmethod
def _content_text_size(content) -> int:
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        return sum(len(b.get("text", "")) for b in content if b.get("type") == "text")
    return len(str(content))
```

cap/shrink 的 size 判断走此 helper，**只对 role=tool 生效**（user 多模态原样保留）。

### 10. `requirements.txt`

```
pypdf>=4.0
python-magic>=0.4   # 可选
```

---

## 前端改造

### 1. `webui/src/types.ts`

```ts
export interface AttachmentRef {
  id: string; name: string; mime: string; size: number
  kind: 'image' | 'document' | 'text'
  hasText: boolean; hasImage: boolean
  path: string; textPath?: string
}

export interface UserMessage {
  id: string; role: 'user'; content: string
  attachments?: AttachmentRef[]
  local?: boolean
}

export interface ModelEntry {
  // 原有字段
  supportsVision?: boolean
}

export interface CurrentModelConfig {
  // 原有字段
  supportsVision?: boolean
}

export interface ModelTestResult {
  ok: boolean
  kind: 'text' | 'vision'
  latencyMs?: number
  model?: string
  provider?: string
  sample?: string
  finishReason?: string
  error?: string
  visionMarked?: boolean
}
```

> v2 删除 `ProviderOption.supportsVision`、`CurrentModelConfig.supportsVisionOverride`、`ModelEntry.supportsVision: boolean | null`。

### 2. `webui/src/api/attachments.ts`（新建）

```ts
import type { AttachmentRef } from '../types'

export async function uploadAttachment(file: File): Promise<AttachmentRef> {
  const fd = new FormData()
  fd.append('file', file)
  const r = await fetch('/api/attachments', { method: 'POST', body: fd })
  if (!r.ok) {
    const data = await r.json().catch(() => ({}))
    throw new Error(data.error || `HTTP ${r.status}`)
  }
  return r.json()
}
```

### 3. `webui/src/api/model.ts`（新建）

```ts
import type { ModelTestResult } from '../types'

export async function testModelEntry(
  entryName: string,
  kind: 'text' | 'vision',
): Promise<ModelTestResult> {
  const r = await fetch('/api/model-test', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ entryName, kind }),
  })
  return r.json()
}
```

### 4. `webui/src/components/chat/AttachmentChip.vue`（新建）

```vue
<script setup lang="ts">
import { computed } from 'vue'
import type { AttachmentRef } from '../../types'
import { formatBytes } from '../../utils/format'

const props = defineProps<{ data: AttachmentRef; removable?: boolean }>()
const emit = defineEmits<{ (e: 'remove'): void }>()
const isImage = computed(() => props.data.kind === 'image')
const previewUrl = computed(() =>
  isImage.value ? `/api/attachments/${props.data.id}/raw` : null,
)
</script>

<template>
  <div class="attach-chip" :class="{ 'is-image': isImage }">
    <img v-if="previewUrl" :src="previewUrl" :alt="data.name" />
    <span v-else class="doc-icon">📄</span>
    <div class="meta">
      <div class="name" :title="data.name">{{ data.name }}</div>
      <div class="sub">{{ formatBytes(data.size) }} · {{ data.kind }}</div>
    </div>
    <button v-if="removable" class="remove" @click="emit('remove')">×</button>
  </div>
</template>
```

### 5. `webui/src/components/chat/Composer.vue`（重点改造）

```ts
const drafts = ref<AttachmentRef[]>([])
const uploading = ref<Set<string>>(new Set())
const fileInput = ref<HTMLInputElement | null>(null)
const dragActive = ref(false)
```

Handlers：
```ts
async function handleFiles(files: FileList | File[]) {
  const slots = 5 - drafts.value.length
  const list = Array.from(files).slice(0, slots)
  for (const f of list) {
    uploading.value.add(f.name)
    try {
      drafts.value.push(await uploadAttachment(f))
    } catch (err) {
      ctx.showToast(String(err))
    } finally {
      uploading.value.delete(f.name)
    }
  }
}
function pickFiles() { fileInput.value?.click() }
function onFileChange(e: Event) {
  const input = e.target as HTMLInputElement
  if (input.files) handleFiles(input.files)
  input.value = ''
}
function onDrop(e: DragEvent) {
  e.preventDefault(); dragActive.value = false
  if (e.dataTransfer?.files) handleFiles(e.dataTransfer.files)
}
function removeDraft(idx: number) { drafts.value.splice(idx, 1) }

function submit() {
  const text = composer.value.trim()
  if (!text && drafts.value.length === 0) return
  emit('send', { content: text, attachments: drafts.value })
  composer.value = ''
  drafts.value = []
}
```

模板新增：
- 隐藏 `<input ref="fileInput" type="file" multiple :accept="ACCEPT_LIST">`
- attach 按钮 `@click="pickFiles"`，tooltip：`current.supportsVision ? '当前模型 ✓ 视觉' : '当前模型未标记视觉，可去 /model 测试激活'`
- 外层 `@dragover.prevent="dragActive=true" @dragleave="dragActive=false" @drop="onDrop"`
- textarea 上方草稿条：`<AttachmentChip v-for="(r, i) in drafts" :data="r" removable @remove="removeDraft(i)"/>`
- 上传中：spinner + 文件名

### 6. `webui/src/components/chat/MessageList.vue`

用户气泡渲染（line 40-48）：
```vue
<div v-if="message.role === 'user'" class="bubble user">
  <div v-if="message.attachments?.length" class="user-attach-row">
    <AttachmentChip v-for="a in message.attachments" :key="a.id" :data="a" />
  </div>
  <p v-if="message.content" class="user-text">{{ message.content }}</p>
</div>
```

### 7. `webui/src/composables/useRuntime.ts`

```ts
async function sendMessage(payload: { content: string; attachments?: AttachmentRef[] }) {
  const text = payload.content
  const attachments = payload.attachments || []
  messages.value.push({
    id: localId(), role: 'user', content: text, attachments, local: true,
  })
  messages.value.push({
    id: localId(), role: 'assistant', content: '', segments: [], streaming: true, local: true,
  })
  ws.send(JSON.stringify({
    type: 'message',
    content: text,
    attachments: attachments.map(a => a.id),
  }))
}
```

`useAppContext` 转发处同步改签名。

### 8. `webui/src/components/panels/ModelPanel.vue` —— v2 简化版

**(a) entry 列表项渲染加 👁 徽章**：

```vue
<div class="entry-list">
  <div
    v-for="(e, idx) in entries"
    :key="idx"
    class="entry-item"
    :class="{ active: idx === editingIndex, default: e.name === defaultName }"
    @click="pickEditing(idx)"
  >
    <div class="entry-meta">
      <div class="entry-title">
        <span>{{ e.label || e.name }}</span>
        <span
          v-if="e.supportsVision"
          class="entry-vision-eye"
          title="此条目已通过视觉测试，可接收图片附件"
        >👁</span>
      </div>
      <div class="entry-sub">
        <code>{{ e.provider }}</code> · <code>{{ e.id || '(no id)' }}</code>
      </div>
    </div>
    <span v-if="e.name === defaultName" class="entry-active-badge">✓ 激活中</span>
    <button v-else class="entry-activate-btn" @click.stop="setActive(idx)">设为激活</button>
  </div>
</div>
```

**(b) 编辑表单底部"连通测试"区**（替代 v1 的 `.vision-row` + `.test-row`）：

```vue
<div class="test-row">
  <div class="test-label">
    <span>连通测试</span>
    <small class="hint">
      用一次最小请求验证 entry 是否能跑；视觉测试通过会自动给本条目打 👁 视觉标记
    </small>
  </div>
  <div class="test-actions">
    <button
      class="tool-button"
      :disabled="hasChanges || testing.text"
      :title="hasChanges ? '请先保存配置再测试' : '发一次 ping（约消耗几十 token）'"
      @click="runTest('text')"
    >
      <span v-if="testing.text">…测试中</span>
      <span v-else>测试文本</span>
    </button>
    <button
      class="tool-button"
      :disabled="hasChanges || testing.vision"
      :title="hasChanges ? '请先保存配置再测试' : '发一张红色测试图（约几十 token）；通过即标 👁'"
      @click="runTest('vision')"
    >
      <span v-if="testing.vision">…测试中</span>
      <span v-else>测试视觉</span>
    </button>
  </div>
  <div v-if="lastResult" class="test-result" :class="{ ok: lastResult.ok, fail: !lastResult.ok }">
    <template v-if="lastResult.ok">
      <span class="badge">✓ {{ lastResult.kind === 'vision' ? '视觉通' : '文本通' }}</span>
      <span class="meta">{{ lastResult.latencyMs }}ms · {{ lastResult.model }}</span>
      <code class="sample">{{ lastResult.sample }}</code>
      <span v-if="lastResult.kind === 'vision' && lastResult.visionMarked" class="meta jade">
        已自动写入 👁 视觉标记
      </span>
    </template>
    <template v-else>
      <span class="badge">✗ 失败</span>
      <span class="meta" :title="lastResult.error">{{ truncate(lastResult.error, 80) }}</span>
    </template>
  </div>
</div>
```

```ts
import { testModelEntry } from '../../api/model'
const testing = reactive({ text: false, vision: false })
const lastResult = ref<ModelTestResult | null>(null)

async function runTest(kind: 'text' | 'vision') {
  if (!editing.value?.name) return
  testing[kind] = true
  try {
    const r = await testModelEntry(editing.value.name, kind)
    lastResult.value = r
    // 视觉测试成功 → 后端已持久化，刷新 bootstrap 让 entry 列表的 👁 立刻显示
    if (r.ok && kind === 'vision' && r.visionMarked) {
      await ctx.reloadBootstrap()
    }
  } catch (err) {
    lastResult.value = { ok: false, kind, error: String(err) }
  } finally {
    testing[kind] = false
  }
}
```

**(c) `entryToWire`** —— 透传 supportsVision 字段（保存时不丢失）：
```ts
return {
  // ...
  supportsVision: !!e.supportsVision,
}
```

> **删除 v1 的 vision-row 整块（pill 三态 + effective 提示）**。

### 9. `webui/src/styles.css`

```css
/* 草稿条 + 气泡通用 chip */
.attach-chip { @apply inline-flex items-center gap-2 rounded-xl border border-line/70 bg-paper px-2 py-1.5; }
.attach-chip.is-image img { @apply h-12 w-12 rounded-lg object-cover; }
.attach-chip .meta { @apply min-w-0 max-w-[10rem]; }
.attach-chip .meta .name { @apply truncate font-mono text-xs font-bold; }
.attach-chip .meta .sub { @apply truncate font-mono text-[10px] text-muted; }
.attach-chip .remove { @apply ml-1 grid h-5 w-5 place-items-center rounded-full bg-paper2 text-muted hover:bg-seal/20 hover:text-seal; }
.attach-chip .doc-icon { @apply grid h-12 w-12 place-items-center rounded-lg bg-paper2 text-2xl; }

/* 用户气泡 attach 行 */
.user-attach-row { @apply mb-2 flex flex-wrap gap-2; }

/* Composer 拖拽高亮 */
.composer-drag-active { @apply ring-2 ring-seal/40 ring-offset-2 ring-offset-paper; }

/* ModelPanel 视觉徽章（v2 标志性元素） */
.entry-title { @apply flex items-center gap-2; }
.entry-vision-eye {
  @apply inline-flex h-5 w-5 items-center justify-center rounded-full
         border border-jade/40 bg-jade/10 text-xs leading-none text-jade;
  cursor: help;
}

/* ModelPanel 测试行 */
.test-row { @apply flex flex-col gap-2 rounded-xl border border-line/60 bg-paper2/50 p-3; }
.test-label .hint { @apply block text-xs text-muted; }
.test-actions { @apply flex flex-wrap gap-2; }
.test-result { @apply flex flex-wrap items-center gap-2 rounded-lg border px-2 py-1.5 font-mono text-xs; }
.test-result.ok { @apply border-jade/40 bg-jade/10; }
.test-result.fail { @apply border-amber/40 bg-amber/10; }
.test-result .badge { @apply font-bold; }
.test-result.ok .badge { @apply text-jade; }
.test-result.fail .badge { @apply text-amber; }
.test-result .meta.jade { @apply text-jade; }
.test-result .sample { @apply max-w-[20rem] truncate rounded bg-paper px-1.5 py-0.5; }
```

### 10. `webui/src/composables/useBootstrap.ts`

新增 `reloadBootstrap()` 方法（如果不存在）—— 简单调一次 `fetchBootstrap()` 把 `boot.value` 替换。这样测试视觉成功后 entry 列表会即时更新 👁。

```ts
async function reloadBootstrap() {
  boot.value = await fetchBootstrap()
}
return { ..., reloadBootstrap }
```

`useAppContext` 转发：`reloadBootstrap: bootstrap.reloadBootstrap`。

---

## 旧路径兼容

| 兼容点 | 处理 |
|---|---|
| 旧 `history.jsonl` 全是 `content: str` | runner / `_handle_ws_text` 都 fall-through string |
| 现有 `compactor._messages_to_text` | line 52-70 已处理 list-of-blocks，image_url block 取不到 text 自动忽略 |
| `_pair_tool_calls` | 不看 content shape，零影响 |
| `_json_safe` | 已支持嵌套 dict/list |
| `model_config.json` 旧文件无 `supportsVision` | `bool(item.get(..., False))` 默认 false → 无 👁 → 用户去测试一次激活 |
| WebSocket replay | bootstrap 把 `unarchivedHistory` 中 attachment ID 转 ref 返回，刷新仍可见缩略图 |

---

## 文件清单 & 行数估计

### 后端

| 文件 | 改动 | 行数 |
|---|---|---|
| `agent/attachments.py` | **新建** | +180 |
| `agent/providers/registry.py` | （v2：不动） | 0 |
| `agent/providers/factory.py` | `ProviderSnapshot.supports_vision` / `entry_name` | +3 |
| `agent/model_config.py` | `ModelEntry.supports_vision`、`_parse_entry`、snapshot 字段、payload `supportsVision`、`mark_entry_vision` | +28 |
| `agent/loop.py` | 暴露 `self.supports_vision` | +2 |
| `agent/webui.py` | upload + raw + `_handle_ws_text` 扩 + `_build_user_content` + `/api/model-test` 含**自动持久化** + bootstrap 回填 | +160 |
| `agent/providers/anthropic_provider.py` | `_content_to_anthropic` block 转换 | +28 |
| `agent/providers/openai_compat.py` | 白名单确认 | +1 |
| `agent/runner.py` | `_content_text_size` helper；cap/shrink 用之；只对 tool | +18 |
| `requirements.txt` | `pypdf` | +2 |
| **小计** | | **+422** |

### 前端

| 文件 | 改动 | 行数 |
|---|---|---|
| `webui/src/types.ts` | `AttachmentRef`、`UserMessage.attachments`、`ModelEntry.supportsVision`、`CurrentModelConfig.supportsVision`、`ModelTestResult` | +20 |
| `webui/src/api/attachments.ts` | **新建** | +12 |
| `webui/src/api/model.ts` | **新建** | +14 |
| `webui/src/components/chat/AttachmentChip.vue` | **新建** | +60 |
| `webui/src/components/chat/Composer.vue` | drafts state + 拖拽 + handler + 草稿条 | +90 |
| `webui/src/components/chat/MessageList.vue` | 用户气泡 attach 行 | +6 |
| `webui/src/composables/useRuntime.ts` | sendMessage payload | +12 |
| `webui/src/composables/useAppContext.ts` | sendMessage 转发 + `reloadBootstrap` | +4 |
| `webui/src/composables/useBootstrap.ts` | `reloadBootstrap()` 方法 | +6 |
| `webui/src/components/panels/ModelPanel.vue` | entry 项 👁 徽章 + 连通测试区 + 测试视觉成功后 reload | +75 |
| `webui/src/styles.css` | chip / drag / vision-eye / test-row | +60 |
| **小计** | | **+359** |

**总增量 ~+781 行**（v1 是 ~854 行；**节省 73 行**）；零删改既有逻辑（`anthropic_provider.py:121` 一行替换、Composer 既有 attach 按钮挂 handler）。

---

## 验证 Checklist

### A. attachments.py 单元

```bash
.venv/bin/python -c "
from pathlib import Path
import tempfile
from agent.attachments import AttachmentStore

with tempfile.TemporaryDirectory() as d:
    store = AttachmentStore(Path(d))
    ref = store.save(raw=b'\\x89PNG\\r\\n\\x1a\\n'+b'A'*100, name='hi.png', mime='image/png')
    assert ref.kind == 'image' and ref.has_image and not ref.has_text
    ref2 = store.save(raw=b'hello', name='note.txt', mime='text/plain')
    assert ref2.has_text and store.read_text(ref2) == 'hello'
    try: store.save(raw=b'x', name='bad.exe', mime='application/x-msdownload')
    except ValueError: pass
    else: raise AssertionError('should have rejected exe')
    print('A OK')
"
```

### B. Anthropic block 转换离线

```python
from agent.providers.anthropic_provider import AnthropicProvider
ap = AnthropicProvider.__new__(AnthropicProvider)
out = ap._content_to_anthropic([
    {"type":"text","text":"hi"},
    {"type":"image_url","image_url":{"url":"data:image/png;base64,AAA"}},
])
assert out == [
    {"type":"text","text":"hi"},
    {"type":"image","source":{"type":"base64","media_type":"image/png","data":"AAA"}}
]
print('B OK')
```

### C. cap/shrink 不误伤多模态

```python
from agent.runner import AgentRunner
hist = [{"role":"user","content":[
    {"type":"text","text":"小文本"},
    {"type":"image_url","image_url":{"url":"data:image/png;base64," + "A"*200000}},
]}]
out = AgentRunner._cap_tool_result(hist)
assert out[0]["content"] == hist[0]["content"]
out2 = AgentRunner._shrink_old_tool_results(out)
assert out2[0]["content"] == hist[0]["content"]
print('C OK')
```

### D. mark_entry_vision 单元

```python
from agent.model_config import mark_entry_vision, load_model_config
# 假设 model_config.json 有一条 name="claude" 的 entry
mark_entry_vision(Path("."), "claude", value=True)
cfg = load_model_config(Path("."))
e = cfg.find_entry("claude")
assert e.supports_vision is True
print('D OK')
```

### E. 集成（webui）— 附件流

```bash
cd /Users/anhuike/Documents/workspace/emperor-agent
pkill -f 'python webui.py' || true
.venv/bin/python webui.py &
# 浏览器 http://127.0.0.1:8765
```

1. **图片直传**：先在 /model 给 anthropic entry 跑测试视觉激活 👁 → 切到该 entry → 拖 PNG 进 Composer → 缩略图 → 发"看图说话" → claude 描述图片
2. **未激活兜底**：deepseek entry 无 👁 → 上传图片仍能选 → 发出后端把图替换为 `[图片附件 hi.png（当前模型未标记视觉，已忽略；可在 /model 测试视觉激活）]`
3. **PDF 文本抽取**：上传 5 页中文 PDF → 文档卡片 → 发"总结" → 任何 model 基于抽出文本回答
4. **read_file 兜底**：上传 PDF 后问"用 read_file 看 memory/attachments/2026-05/xxx-foo.pdf.txt" → 主代理 read_file 读 sidecar
5. **历史持久化**：发完图刷新 → bootstrap 回填 → 用户气泡缩略图仍可见
6. **超限拒绝**：拖 12MB PNG → toast 报错
7. **Compactor 不崩**：连发 3 条带图凑阈值 → 触发压缩 → log 无异常
8. **Checkpoint 兼容**：发图后 Ctrl-C kill webui → 重启 → checkpoint 恢复带 attachments 的 user message

### F. ModelPanel 视觉测试 + 👁 徽章（**v2 重点**）

1. **初始无 👁**：所有 entry 默认无 👁 徽章（包括 anthropic）
2. **测试文本（dirty）**：编辑 entry 改个字段不保存 → "测试文本"按钮禁用 + tooltip "请先保存"
3. **测试文本（已保存）**：保存后点击 → 200ms-2s 内显示 ✓ 文本通 + latency + sample 含 pong
4. **测试视觉成功 → 自动 👁**：anthropic entry 点"测试视觉" → 1-3s 显示 ✓ 视觉通 + sample 含 red + "已自动写入 👁 视觉标记"提示 → entry 列表项**右侧立刻出现 👁** → 刷新页面 👁 仍在
5. **测试视觉失败**：deepseek-chat（不识图）entry 点"测试视觉" → 显示 ✗ 失败 + sample 不含 red → 列表项**不出现 👁**
6. **测试连通错误**：把 entry 的 apiKey 故意清空 → 保存 → 测试 → 显示 ✗ 失败 + 错误信息（如 401）
7. **多 entry 互不干扰**：只有跑过测试视觉的那条 entry 有 👁，其他保持原状
8. **激活 entry 与视觉徽章独立**：entry 可以是 ✓ 激活 + 👁 视觉，也可以仅一种状态；NavRail 头部不显示 👁（保持简洁，仅 ModelPanel 列表显示）
9. **重测覆盖**：先在 anthropic 测视觉成功标了 👁 → 用户改坏 apiKey 保存 → 再测试视觉失败：**当前实现不会清除 👁**（保守设计，避免临时网络抖动误清）；用户若需要去除 👁 须直接编辑 model_config.json。注：可在 v3 加 "清除视觉标记" 按钮。

### G. 回归

- 不带附件的纯文本对话与之前完全一致
- `/clear` `/status` 工作
- Token 统计正确
- /model 页面其他字段（apiKey / temperature / maxTokens / extras）保存仍正常

---

## 实施顺序

```
Day 1  attachments.py + model_config.py (ModelEntry.supports_vision + mark_entry_vision) + factory/loop 透传
Day 2  webui.py: upload + raw + _build_user_content + _handle_ws_text 扩展
Day 3  webui.py: /api/model-test（含自动持久化）+ 探测 PNG + bootstrap attachment 回填
Day 4  anthropic block 转换 + runner cap/shrink helper + 单元跑通 (A/B/C/D)
Day 5  前端：types + api + AttachmentChip + Composer 拖拽与草稿条
Day 6  前端：MessageList + useRuntime / useAppContext + useBootstrap reload + styles
Day 7  前端：ModelPanel 👁 徽章 + 测试按钮 + 测试视觉成功后 reloadBootstrap
Day 8  集成：E 8 项 + F 9 项 + G 回归
========= Phase A 发布 =========
```

---

## 不在本次范围

- **Anthropic 原生 PDF document block** —— 留 Phase B
- **Office docx / xlsx** —— 留 Phase B
- **远程 URL 图片**（非 data URL）—— v1 不支持
- **附件级权限/过期清理** —— 单用户本地工具不删
- **WebSocket 二进制传输** —— HTTP multipart 已够
- **音视频附件** —— 超出本期目标
- **手动 toggle 视觉标记**（清除 / 强开）—— v2 仅测试激活；如需手动可后续加"清除 👁"按钮，但优先级低
- **ProviderSpec 级 supports_vision 字段** —— v2 收敛到 entry 级，避免双层继承

---

## 风险与回退

- **风险 1**：`pypdf` 抽不出文本（扫描版 PDF）→ sidecar 为空，user message 仅留落盘提示。**缓解**：UI AttachmentChip 显示 ⚠ 标记。
- **风险 2**：base64 图片膨胀 history.jsonl → checkpoint 写盘变慢。**缓解**：multimodal user content 在 cap/shrink 跳过；`history.jsonl` 仅写 attachment ID（不写 base64）。
- **风险 3**：连通测试请求消耗真 token。**缓解**：max_tokens=64 + temperature=0；按钮 tooltip 提示"会消耗少量 token"；UI 显式显示 latency 与 sample。
- **风险 4**：视觉测试因网络抖动失败 → 用户被误判没视觉。**缓解**：失败不清除已有的 👁；用户可重测。失败时显示完整 sample 与 error，让用户自己判断。
- **风险 5**：openrouter / 网关 provider 测试视觉用 chat 模型 → 响应可能不识图。**缓解**：用户必须自己选支持视觉的 model id（如 `openai/gpt-4o`）；测试不通过即说明该 model 不行，行为符合预期。
- **回退**：所有改动集中在前后端 ~10 个文件；`git revert <hash>` 一键回退。`memory/attachments/` 保留磁盘副本不影响其他功能。
