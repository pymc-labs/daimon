import { useState } from "react"
import { Prose } from "@/components/common"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import { Locked } from "../bits"
import { Section, useLocal, type Props } from "./parts"

export function InstructionsEditor({ agent, config, ro, update }: Props) {
  const [tab, setTab] = useState("write")
  const [text, setText] = useLocal(config.system ?? "", (v) => update((c) => { c.system = v }, 500))
  if (config.system == null) return <Section><Locked who={agent.owner.name} /></Section>
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex h-11 shrink-0 items-center gap-2 border-b px-4">
        <ToggleGroup type="single" value={tab} onValueChange={(v) => v && setTab(v)} variant="segmented" size="sm">
          <ToggleGroupItem value="write" className="px-3 text-xs">{ro ? "Source" : "Write"}</ToggleGroupItem>
          <ToggleGroupItem value="preview" className="px-3 text-xs">Preview</ToggleGroupItem>
        </ToggleGroup>
        <span className="ml-auto text-xs text-faint tabular">{text.split(/\s+/).filter(Boolean).length} words</span>
      </div>
      {tab === "write" ? (
        <textarea
          value={text}
          readOnly={ro}
          onChange={(e) => setText(e.target.value)}
          spellCheck={false}
          className="min-h-0 flex-1 resize-none bg-transparent px-5 py-4 font-mono text-[12.5px] leading-[1.7] outline-none"
        />
      ) : <div className="px-5 py-4"><Prose text={text} /></div>}
    </div>
  )
}
