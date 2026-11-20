import type { ChatEvent, ChatMessage, NeutralCard, Output, Pin } from "../types"

/** One message as a preview shows it, whether it came from a case's history or from a run's recorded events. */
export type Msg = {
  id: string; author: string; at?: string; text?: string; markdown: boolean; replyTo?: string
  reactions: { emoji: string; by: string[] }[]; cards: NeutralCard[]; files: string[]; deleted?: boolean; edited?: boolean; notice?: boolean; scripted?: boolean
}
export type { Pin }

export const fromMessages = (ms: ChatMessage[]): Msg[] =>
  ms.map((m) => ({
    id: m.id, author: m.author, at: m.at, text: m.text, markdown: m.format !== "plain", replyTo: m.reply_to, reactions: m.reactions ?? [],
    cards: m.cards ?? [], files: (m.attachments ?? []).map((a) => a.name), deleted: m.deleted, edited: !!m.edited_at, notice: m.kind !== undefined && m.kind !== "message",
  }))

/** Replay recorded chat events into the messages a person would have seen at the end. */
export function fromEvents(events: ChatEvent[], outputs: Output[] = []): Msg[] {
  const out: Msg[] = []
  const byId = new Map<string, Msg>()
  const add = (m: Msg) => { out.push(m); byId.set(m.id, m); return m }
  const blank = (id: string, author: string, over: Partial<Msg> = {}): Msg => ({ id, author, markdown: true, reactions: [], cards: [], files: [], ...over })
  for (const e of [...events].sort((a, b) => a.seq - b.seq)) {
    const id = e.message_id ?? `e${e.seq}`
    const m = e.message_id ? byId.get(e.message_id) : undefined
    switch (e.kind) {
      case "message": add(blank(id, e.actor, { text: e.text, markdown: e.format !== "plain", replyTo: e.reply_to, scripted: !!e.scripted })); break
      case "notice": add(blank(id, e.actor, { text: e.text, notice: true })); break
      case "edit": if (m) { m.text = e.text; m.edited = true } break
      case "delete": if (m) m.deleted = true; break
      case "reaction_add": if (m && e.emoji) {
        const r = m.reactions.find((x) => x.emoji === e.emoji)
        if (r) r.by.push(e.actor); else m.reactions.push({ emoji: e.emoji, by: [e.actor] })
      } break
      case "reaction_remove": if (m) m.reactions = m.reactions.map((r) => r.emoji === e.emoji ? { ...r, by: r.by.filter((a) => a !== e.actor) } : r).filter((r) => r.by.length) ; break
      case "card": if (e.card) (m ?? add(blank(id, e.actor, { scripted: !!e.scripted }))).cards.push(e.card); break
      case "card_update": if (e.card) for (const x of out) x.cards = x.cards.map((c) => c.id === e.card!.id ? e.card! : c); break
      case "file": (m ?? add(blank(id, e.actor))).files.push(...(e.files ?? []).map((f) => outputs.find((o) => o.id === f)?.name ?? f)); break
    }
  }
  return out
}
