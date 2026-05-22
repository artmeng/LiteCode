# 附件类型图标 Prompts

> 规格：1024×1024 PNG，透明背景。实际 UI 中按 32-48px 使用。
> 统一遵循 `assets/PROMPT_PREFIX.md` 的故宫像素风、16 色调色板和透明 PNG 规范。

本组由 `$imagegen` 生成 4×2 contact sheet 后拆分落盘，主要用于 Chat 附件卡片。

## 通用生成 Prompt

```
Use case: logo-brand
Asset type: UI icon set contact sheet for Emperor Agent WebUI
Primary request: Create a clean contact sheet containing 8 separate pixel-art UI icons for a local AI agent interface: image attachment, PDF document, Markdown/text document, generic file, vision-capable model, text-capable model, test success, test failure.
Style/medium: 16-bit pixel art, crisp pixel-perfect rendering, no anti-aliasing, no blur, transparent or perfectly flat #00ff00 chroma-key background for removal.
Composition/framing: 4 columns x 2 rows, each icon centered in its own clearly separated 128x128 square cell with generous padding; icons must not touch each other.
Color palette: strict limited palette only from #1A1612 #2A2622 #5A5048 #6B6058 #F5EFE0 #EBE3D0 #FBF7E8 #7A1F1F #B53A3A #D9776E #2D6B4F #3F8C6E #6FB39A #9C6925 #C68A3A #E5B568.
Subject details: ancient imperial Chinese theme, Stardew Valley x Eastward x Octopath style. Image icon: small framed landscape/photo scroll. PDF icon: folded paper with seal-red corner mark but no letters. Markdown/text icon: bamboo slip or paper sheet with ink lines and tiny # symbol motif, no readable text. Generic file icon: cream document with folded corner. Vision model icon: jade eye over a tiny neural-node brain, no text. Text model icon: scroll with connected ink nodes, no text. Success icon: jade check mark inside seal token. Failure icon: amber warning seal token with cross mark.
Constraints: no readable letters, no words, no signature, no watermark, no shadows, no glow, no gradients, crisp integer-pixel edges, transparent background preferred.
```

## 输出文件

- `attachment-image.png`：图片附件类型图标，备用于无预览图片或类型标记。
- `attachment-pdf.png`：PDF / document 图标。
- `attachment-markdown.png`：Markdown 图标。
- `attachment-text.png`：纯文本 / JSON / CSV 图标，目前复用 Markdown/Text 视觉。
- `attachment-file.png`：未知或通用文件图标。
