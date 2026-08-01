---
name: volcengine-seedance-commercial
description: Commercial real-person and product video workflow adapted from the official Volcengine Agent Plan Seedance Skill
---

# Volcengine Seedance Commercial

Use this Skill for real-person advertisements, product demonstrations, social
shorts, image-to-video work, and campaign motion creatives. It is an
Astra-managed adaptation of the official Volcengine Agent Plan Seedance Skill
v4.0.0.

## Capability and security boundary

- This Skill guides work; it does not grant a Tool or a Seedance entitlement.
- Use only `generate_video_minimax`, `check_video_minimax`,
  `generate_speech_minimax`, `generate_music_minimax`, and
  `compose_video_audio`. Astra chooses the provider and keeps an accepted task
  pinned to that provider.
- Never pass, display, or request an API key. Never run the upstream
  `seedance-wrapper.js`; it bypasses Astra credential, tenant, durable task,
  Credits, approval, and storage controls.
- Do not select a provider model name in the prompt. Astra routes by the
  operator-verified account capability. If Agent Plan video is unavailable, it
  may fail over to MiniMax before submission.
- `require_audio=false` is the default product contract. Set it to `true` only
  when the requested result needs a provider-generated audio stream; Astra
  preserves that exact intent in the Agent Plan request instead of enabling
  audio implicitly.
- If no provider accepts the task, return the stable unavailable result. Do not
  fabricate a video, silently substitute a slideshow, or repeatedly resubmit.
- One user request permits at most one `generate_video_minimax` invocation
  unless the user explicitly asks for another attempt. A local argument or
  media-contract failure still consumes that invocation budget: report it and
  stop instead of changing arguments and calling the Tool again.

## Choose the production mode

1. **Text-to-video** — use for exploratory motion where exact product identity
   is not critical.
2. **First-frame image-to-video** — preferred for a real-person or product ad.
   First generate/obtain an approved keyframe, then pass it as
   `first_frame_image`.
3. **First-and-last-frame video** — use only when both endpoints are approved
   and the selected route supports them.
4. **Protected product layer** — use `brand_asset` when the supplied
   product/logo must remain unchanged. It cannot be combined with frame
   references.

For a commercial real-person ad, prefer an approved first frame. Text-only
generation is acceptable for discovery but is not sufficient evidence of
identity or product consistency.

## Current reviewed model policy

Agents never choose a model name. The server-side policy reviewed on
2026-07-24 maps an eligible Medium plan to Seedance 2.0 Mini and Large / Max
to Seedance 2.0. Small has no Agent Plan video entitlement. If that policy or
the verified credential capability does not admit the request, Astra uses the
provider-neutral pre-submission fallback instead of asking the Agent to change
its prompt.

Seedance 2.0 Mini is the current Medium migration target. Keep new requests
inside its server-enforced envelope: up to 15 seconds, `480p` or `720p`, and a
supported fixed aspect ratio. Do not copy model IDs into the prompt or assume
that a future plan name guarantees the same model.

## Seedance 1.5 Pro legacy-task compatibility

Only an already accepted and provider-pinned legacy task may continue on
Seedance 1.5 Pro. Compile such a task inside the reviewed official v4.0.0
capability envelope:

- text-to-video, first-frame image-to-video, or first-and-last-frame
  image-to-video;
- 4–12 seconds, `480p` / `720p` / `1080p`, and one of `21:9`, `16:9`,
  `4:3`, `1:1`, `3:4`, or `9:16`;
- optional provider-generated audio;
- no web search, video/audio reference, three-or-more-image reference,
  video editing, or video extension.

The official API also describes 1.5-only `draft` preview and `flex`
low-cost/offline modes. Astra validates those parameters in its server adapter
but does not expose them as Agent-facing fields in the provider-neutral video
Tool: silently losing them during MiniMax fallback would change cost and
latency semantics. The normal commercial path therefore requests the final
quality mode.

Seedance 1.5 Pro stopped admitting new Agent Plan users on 2026-07-10 and is
scheduled to stop service on 2026-09-21. Never select it for a new task. Keep
the public model name and dated Provider ID only in the server compatibility
table so an accepted legacy task can still be reconciled without changing this
Skill or the Agent prompt.

Before the single Tool invocation, use the exact canonical `workspace/...`
path from the image Tool receipt. Do not pass the shortened attachment label
such as `images/...`. If the receipt is unavailable, confirm the path with a
read-only workspace listing first. Never discover a bad path by repeatedly
calling the paid video Tool.

## Write a timed shot plan

Fit one coherent action into the requested duration. For a 6-10 second ad,
write three beats rather than unrelated scenes:

- **0-2s — hook:** establish the person, location, and product in one readable
  action.
- **2-6s — proof:** show the product interaction or benefit with plausible hand
  contact and a controlled camera move.
- **final 1-2s — resolve:** finish on a stable product/face composition with
  negative space for deterministic copy.

Then compile the Tool prompt in this order:

`format and genre + persistent person/product identity + timed beats + exact
physical actions + camera path + lighting/material continuity + audio intent +
commercial finish + exclusions`

Explicitly require continuous wardrobe, face, product geometry, dominant hand,
screen/display state, lighting direction, and location. Use one camera move per
short clip. Avoid scene jumps, impossible object transformations, floating
hands, extra fingers, lip movement without speech, baked-in captions, logos,
watermarks, and pseudo-text.

## Audio and copy decisions

- For synchronized on-camera dialogue, set `require_audio=true` and include the
  exact short spoken line plus speaker/action timing in the prompt. Only claim
  lip sync after reviewing the actual result.
- For narration, set `require_audio=false`, generate the clean visual first,
  call `generate_speech_minimax`, then use `compose_video_audio`.
- Use `generate_music_minimax` only when music materially improves the brief;
  keep the bed below narration.
- Put exact on-screen wording in `overlay_text`; never ask the video model to
  render it.

## Scheduling and durable task rules

- Normal video work is asynchronous. Treat the returned task record as the
  identity of the accepted job.
- Use `check_video_minimax` for a submitted task; never create a duplicate just
  because generation is slow.
- `wait_for_completion=true` is for a bounded interactive test, not a guarantee
  that a long render finishes in the chat request.
- The final MP4 must pass Astra's duration, aspect-ratio, codec, audio (when
  required), and browser-safety checks before delivery.

## Commercial quality gate

Review the actual first, middle, and last frames plus audio:

- same person, wardrobe, product, hand, and display state across the clip;
- action reads without narration and the product benefit is actually visible;
- no scene discontinuity, duplicate object, body/hand distortion, unintended
  text, watermark, or last-second identity drift;
- camera motion is stable and the final frame can hold an end card;
- dialogue/narration is intelligible and matches the visual timing;
- MP4 duration, aspect ratio, codecs, and audio contract match the request.

A completed provider task is not automatically commercial-ready. Revise the
shot/action that failed, or switch to approved first-frame generation, instead
of merely adding more adjectives.

## Provenance and current entitlement

The official model matrix, reviewed version/hash, endpoint, and Astra
adaptation boundary are recorded in
`references/official-agent-plan-contract.md`. Runtime provider availability,
not this static file, decides whether Seedance is callable for the active
platform account.
