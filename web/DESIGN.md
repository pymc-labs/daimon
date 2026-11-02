# Daimon web: design rules

Shared design system: tokens, components and density live in `src/index.css` and `src/components/ui`.

## Look

- Tokens in `src/index.css` only: `bg-muted` sidebar, `bg-background` content, `border`, `text-muted-foreground`, `text-faint`, `text-link`, `bg-teal`/`aqua` accents, `chart-1..5`. No raw hex in components.
- Density: rows and controls `h-8`, page header `h-12`, `text-sm` body, `text-xs` labels. Radius `rounded-md` (controls), `rounded-lg`/`rounded-xl` (cards).
- Pages use `components/shell/Page` (crumbs, actions, tabs). Detail opens in a right `Sheet` drawer, not a new page, unless it is a workspace of its own (agent canvas, chat).
- Reuse `components/ui/*` (shadcn) and `components/common.tsx` (`Ago`, `Num`, `Money`, `Avatar`, `AgentAvatar`, `CopyButton`, `Kbd`, `Prose`, `StatStrip`). `CreditMeter` for credit.
- Light and dark both work; check both.

## Words

- Labels and essential facts only. No helper sentences, no empty-state essays, no em dashes.
- A refusal names who can help ("Ask an admin").
- Everyday words: "Instructions" not "system prompt" in UI chrome, "Tools", "Skills", "Connections".

## Code

- A module owns `src/modules/<key>/` and `src/mock/<key>.ts`; shared shapes go in `src/lib/types.ts`, module shapes in `src/modules/<key>/types.ts`. Do not edit another module's files.
- The mock API (MSW, `src/mock/`) is the future server contract: real paths under `/api/<module>/...`, JSON shapes we would ship.
- Brief code.

## Run

`npm run dev` (port 5174). Sign in with any provider (Maya, owner); switch user from the user menu (owner, admin, member, visitor). `/invite/inv_demo` is the visitor flow.
