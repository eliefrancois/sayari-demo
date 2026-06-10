# Third-party notices

## local-lmcanvas (design-system donor)

Portions of this frontend's design system and components are ported from
**local-lmcanvas** (Electron + React canvas-based branching AI conversation
tool), MIT License, Copyright (c) 2026 Max Lee.

Ported material (adapted for Next.js and this app's SSE/conversation-store
data flow):

- `app/globals.css` — OKLCH design-token set (light + dark), Geist font
  stacks, 32px grid canvas pattern, React Flow control/edge theming,
  scrollbar styling, shimmer keyframes
  (donor: `src/renderer/src/styles/globals.css`)
- `public/fonts/GeistPixel-Square.woff2` — Geist Pixel Square accent font,
  bundled by the donor (font itself by Vercel)
- `components/canvas/TurnCard.tsx` — card language (hairline-border
  `rounded-[10px]` white cards, corner badge strip, drop-in motion with
  `[0.16, 1, 0.3, 1]` easing, divider, streaming/shimmer presentation,
  suggestion-chip styling)
  (donor: `src/renderer/src/components/Canvas/CustomNode.tsx`,
  `NodeResponse.tsx`)
- `components/canvas/ToolCallBlock.tsx` — collapsible tool-call row pattern
  (icon + name + mono summary + status, expandable mono input/result)
  (donor: `src/renderer/src/components/Canvas/blocks/ToolUseView.tsx`,
  `toolMeta.ts`)
- `components/GraphPanel.tsx` — grid background configuration
  (donor: `src/renderer/src/components/Canvas/Canvas.tsx`)

MIT License

Copyright (c) 2026 Max Lee

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
