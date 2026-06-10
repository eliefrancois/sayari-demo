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
- `lib/canvas-layout.ts` — canvas layout engine: node-size constants, the
  push-right sibling collision cascade, position-aware edge handle selection,
  zoom-corrected DOM height measurement, and the child placement heuristic
  (follow-ups below the parent, fork siblings in a right lane)
  (donor: `src/renderer/src/lib/canvasConstants.ts`,
  `collisionResolution.ts`, `edgeHandles.ts`, `nodeDom.ts`,
  `src/renderer/src/hooks/useBranchFromNode.ts`)
- `components/canvas/InvestigationCanvas.tsx` — React Flow host
  configuration (pan/zoom behavior, 32px line grid, local node-state drag
  pattern, drag-aware edge handle recomputation) and the draft-child fork
  flow
  (donor: `src/renderer/src/components/Canvas/Canvas.tsx`,
  `src/renderer/src/hooks/useBranchFromNode.ts`)
- `components/canvas/TurnNode.tsx` — node chrome: four-side connection
  handles, hover-revealed circular `+` branch button, selection ring, and
  the draft-card textarea pattern (Enter submits, Esc cancels)
  (donor: `src/renderer/src/components/Canvas/CustomNode.tsx`)
- `components/manager/ConversationManager.tsx` — history side-panel shell:
  fixed left slide-over with spring motion, scrim dismissal, New button,
  search + scrolling list layout
  (donor: `src/renderer/src/components/CanvasManager/CanvasManager.tsx`)
- `components/manager/ConversationItem.tsx` — list row pattern: active-row
  highlight, hover-revealed `…` options button, click-outside menu dismissal
  (donor: `src/renderer/src/components/CanvasManager/CanvasItem.tsx`)
- `components/manager/ConversationSearch.tsx` — collapsed-label-to-inline-
  input search toggle with imperative close ref
  (donor: `src/renderer/src/components/CanvasManager/CanvasSearch.tsx`)
- `components/manager/DeleteConversationModal.tsx` — delete-confirm modal:
  scrim, focus-the-confirm-button pattern, Esc-to-close
  (donor: `src/renderer/src/components/CanvasManager/DeleteCanvasModal.tsx`)

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
