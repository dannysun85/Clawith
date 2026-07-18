---
name: Brand-safe Media
description: Required workflow for customer-facing images or videos containing exact copy, logos, packaging, or reference products
---

# Brand-safe Media

Use this skill whenever a user asks for an image, poster, short video, Douyin creative, advertisement, title card,
logo placement, packaging display, or reference-product creative.

## Choose the delivery contract first

### Brand-safe delivery

Use brand-safe delivery when the user supplies exact Chinese/English copy or requires a product, logo, packaging,
or brand asset to remain unchanged.

1. Preserve user-supplied copy exactly. Do not translate, rewrite, summarize, correct, or place it inside `prompt`.
2. Put the exact visible copy in `overlay_text` and describe only the text-free visual background in `prompt`.
3. Put an uploaded product/logo path in `brand_asset`. Do not combine `brand_asset` with `reference_image`,
   `first_frame_image`, or `last_frame_image`.
4. Prefer a transparent PNG for `brand_asset`. JPG/WebP are accepted but their original rectangular background is
   intentionally preserved; never silently remove or redraw it.
5. Use `brand_position` and `brand_scale` to reserve a stable product area. For video, the protected product layer is
   composited over every frame and remains static while the background moves.
6. Astra softens model-generated backgrounds in exact-copy and protected-product mode so provider-created pseudo-text
   cannot remain legible. Creative reference-image/frame mode keeps scene detail and therefore cannot make this promise.
7. Only report success after the tool returns a real saved workspace path and a brand-safe receipt. The receipt must
   record `background_sanitized=true` whenever background sanitization was required.

### Creative delivery

Use `reference_image`, `first_frame_image`, or `last_frame_image` only when the user explicitly accepts generative
redrawing, new angles, or model-created motion. Explain that spelling, packaging, logos, and product geometry may vary.
Do not describe creative delivery as exact or brand-safe.

## Copy rules

- User supplied exact copy: pass the same Unicode characters and line breaks to `overlay_text`.
- User asked you to write copy: create the final copy first, then pass that final text unchanged to `overlay_text`.
- If the exact copy is too long for the canvas, shorten it only with user approval; never rely on silent truncation.
- Never ask an image/video model to draw words when `overlay_text` is available.

## Product limits

- One flat image cannot prove unseen sides or a physically accurate rotation.
- For new angles, request approved multi-view images or a 3D asset, or use creative delivery with an explicit warning.
- Skills guide the workflow; the native media tools enforce fonts, layout, frozen asset hashes, video decoding, storage,
  and Credits settlement.
