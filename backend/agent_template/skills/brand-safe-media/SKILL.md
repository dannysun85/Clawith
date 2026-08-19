---
name: Brand-safe Media
description: Required workflow for customer-facing images or videos containing exact copy, logos, packaging, or reference products
---

# Brand-safe Media

Use this skill whenever a user asks for an image, poster, short video, Douyin creative, advertisement, title card,
logo placement, packaging display, or reference-product creative.

## Infer the outcome before choosing a tool mode

Do not make the user complete a production form before work begins. Read the request and attachments, infer the
likely deliverable, and ask only for information whose absence would materially change the result (for example aspect
ratio, exact copy, or whether product redraw is acceptable). Present the inferred brief in one short confirmation when
the request is ambiguous.

Choose between these three outcomes:

1. **Poster / packshot / end card** — exact product and exact copy matter; a static product layer is appropriate.
2. **Product-in-motion advertisement** — the supplied product must participate in the scene or camera movement.
3. **Creative reference** — the attachment is inspiration and generative redraw is acceptable.

Never use the static product-layer workaround for outcome 2 and describe it as a generated product advertisement.

## Choose the delivery contract

The Agent chooses the user outcome and calls the product-level media Tool. It must not choose a Provider, expose an
API key, invoke a downloaded vendor script, or promise a specific model. The legacy Tool identifier
`generate_image_minimax` is protocol compatibility only and is never evidence that MiniMax handled a request.
Astra resolves the tenant's eligible account, compatible model, health, quota and execution path at the server boundary.
Use `execution_strategy=commercial_quality` for formal customer delivery and
`execution_strategy=creative_exploration` only for ideation or intentionally broader visual exploration. These values
describe the work contract; they do not select or reveal a vendor. A provider may
only be retried or changed before acceptance, or after an explicit reviewed rejection; ambiguous timeouts enter
reconciliation so the same paid work is not submitted twice.

### Brand-safe delivery

Use brand-safe delivery when the user supplies exact Chinese/English copy or requires a product, logo, packaging,
or brand asset to remain unchanged.

1. Preserve user-supplied copy exactly. Do not translate, rewrite, summarize, correct, or place it inside `prompt`.
2. Put one exact text element in `overlay_text`, or multi-level poster copy in ordered `overlay_blocks`, and describe
   only the text-free visual background in `prompt`. The managed provider creates that background first; Astra's
   server then composes the exact copy with installed fonts, freezes the final canvas from the active tier plus
   `aspect_ratio`, and returns a `poster-v3` receipt in the same Tool call. This does not submit a second provider
   generation. There is intentionally no caller-controlled `delivery_size` argument; do not refuse a valid poster
   because that field is absent or because the image model itself cannot spell the requested copy.
3. Put an uploaded product/logo path in `brand_asset`. Do not combine `brand_asset` with `reference_image`,
   `first_frame_image`, or `last_frame_image`.
4. Prefer a transparent PNG for `brand_asset`. JPG/WebP are accepted but their original rectangular background is
   intentionally preserved; never silently remove or redraw it.
5. Use `brand_position` and `brand_scale` only for posters, packshots, and deliberate video end cards. In video this
   product layer is static; disclose that limitation and do not use it for a request that expects product motion.
6. Do not blur or soften the scene by default. Background sanitization is a fallback for provider-created pseudo-text,
   not a general quality enhancement. Prefer a clean text-free prompt and deterministic overlay copy first.
7. Treat screens, temperature displays, watch faces, labels, signs, and packaging panels as copy surfaces. When their
   exact content is not supplied, require an unlit/blank surface during generation and add approved content later with
   deterministic composition. Never combine a request for a visible display with "no letters or numbers" and then
   accept provider-created pseudo-glyphs as product detail.
8. Only report success after the tool returns a real saved workspace path and a brand-safe receipt. The receipt must
   record `background_sanitized=true` whenever the background transform ran; this flag is an execution receipt, not
   proof that every unintended glyph is unreadable.
9. `overlay_text` and `overlay_blocks` return a final composed image. Never use that image as an HTML background and
   overlay the same wording again. Create PDF, PPTX, HTML, or other formats only when the user or server-owned
   `output_contract` explicitly requests them; do not expand a PNG poster task into extra deliverables.

### Product-in-motion delivery

When the user provides a product photo and asks for a moving advertisement:

1. Use the real uploaded frame through `reference_image` or `first_frame_image` when the provider route supports it.
2. State that a single flat photo cannot guarantee unchanged packaging, typography, geometry, or unseen sides during
   generated motion. Do not call the result exact or brand-safe merely because the first frame was exact.
3. If exact identity is mandatory, offer a bounded composition instead: animate camera/background elements around a
   static packshot, produce a storyboard for approval, or request transparent/multi-view/3D brand assets.
4. If the selected provider cannot accept the reference frame, stop with a capability explanation. Do not silently
   fall back to text-to-video or regenerate a look-alike product.
5. Validate the returned artifact visually before presenting it: product identity, label readability, copy, duration,
   aspect ratio, and absence of corrupted frames.
6. When the brief prohibits third-party brands, use a controlled set with sparse unbranded props. Avoid retail shelves,
   stocked refrigerators, streets of storefront signs, app screens, or other logo-dense backgrounds unless every
   visible asset is supplied and cleared; a clean prompt does not make uncontrolled packaging brand-safe.

### Creative delivery

Use `reference_image`, `first_frame_image`, or `last_frame_image` when generative motion or redraw is part of the
request. Explain that spelling, packaging, logos, and product geometry may vary unless the request already makes that
trade-off clear. Do not describe creative delivery as exact or brand-safe.

For a coherent image set, the prompt must explicitly state the number of images, the content of each image, the
shared subject/product/style constraints, and the required continuity. A sequential-generation flag by itself is not
a continuity plan.

For video, compile capability requirements from the brief before submission: text/image/video/audio references,
first/last-frame control, duration, resolution, generated audio, web search, draft/flex mode and speed preference.
Do not select a model by matching marketing words in the prompt. If the chosen route cannot satisfy a required
capability, fail preflight or choose a compatible provider before any paid task is accepted.

### Commercial video audio modes

Choose the audio contract before submitting the visual task:

1. **On-camera synchronized dialogue** — set `require_audio=true`. This requires a provider route that returns a real
   audio stream and can attempt lip synchronization. If no eligible route exists, stop at capability preflight; do
   not relabel a silent video plus unrelated voiceover as lip-synced dialogue.
2. **Narrated advertisement / voiceover** — create the visual with `require_audio=false`, create the exact narration
   with `generate_speech_minimax`, optionally create or supply a music bed, then call `compose_video_audio`. The final
   MP4, not the silent intermediate, is the customer deliverable.
3. **Silent motion asset** — keep `require_audio=false` and disclose that the delivered MP4 is silent.

For portrait ads on a provider whose text-to-video default is landscape, generate or obtain an approved portrait key
visual first and pass it as `first_frame_image`. Never claim a requested 9:16 deliverable from prompt wording alone;
the completed artifact contract must verify its real dimensions.

`compose_video_audio` is deterministic local post-production. Pass workspace paths returned by the completed video,
speech and music tools. Keep the voiceover at normal gain and the music bed low enough for intelligibility; do not
claim lip sync unless it came from an audio-capable provider route and passed visual review. Its successful structured
receipt is the verification source for the final workspace path, H.264/AAC codecs, dimensions, duration and browser
safety. Do not call `read_file` or `read_document` on the resulting MP4; those tools intentionally reject binary media.

When the brief exceeds a single shot's duration, request the target duration directly (routing picks a route whose
model supports it and fails closed rather than silently shortening) or generate same-canvas shots and merge them with
`concat_videos` before any `compose_video_audio` mix.

## Copy rules

- User supplied exact copy: pass the same Unicode characters and line breaks to `overlay_text`.
- User asked you to write copy: create the final copy first, then pass that final text unchanged to `overlay_text`.
- If the exact copy is too long for the canvas, shorten it only with user approval; never rely on silent truncation.
- Never ask an image/video model to draw words when `overlay_text` is available.
- Never infer or announce provider/model from a compatibility Tool name. Only a server-owned admin/audit receipt may
  identify the actual provider and model; customer-facing delivery remains provider-neutral.

## Product limits

- One flat image cannot prove unseen sides or a physically accurate rotation.
- For new angles, request approved multi-view images or a 3D asset, or use creative delivery with an explicit warning.
- Skills guide the workflow; the native media tools enforce fonts, layout, frozen asset hashes, video decoding, storage,
  and Credits settlement.
