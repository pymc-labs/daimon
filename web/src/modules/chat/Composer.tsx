import { ArrowUp, Paperclip, Square, X } from "lucide-react"
import { useEffect, useRef, useState } from "react"

import { bytes, MAX_FILE_BYTES } from "./format"
import { cn } from "@/lib/utils"

export function Composer({ onSend, onStop, busy, placeholder, disabled, picker, autoFocus }: {
  onSend: (text: string, files: File[]) => void; onStop: () => void; busy: boolean
  placeholder: string; disabled?: boolean; picker: React.ReactNode; autoFocus?: boolean
}) {
  const [text, setText] = useState("")
  const [files, setFiles] = useState<File[]>([])
  const [tooBig, setTooBig] = useState<string[]>([])
  const ref = useRef<HTMLTextAreaElement>(null)
  const pick = useRef<HTMLInputElement>(null)
  useEffect(() => {
    const t = ref.current
    if (!t) return
    t.style.height = "auto"
    t.style.height = `${Math.min(t.scrollHeight, 240)}px`
  }, [text])
  const canSend = (!!text.trim() || files.length > 0) && !busy && !disabled
  const submit = () => {
    if (!canSend) return
    onSend(text.trim(), files)
    setText("")
    setFiles([])
    setTooBig([])
  }
  const add = (list: File[]) => {
    if (!list.length || disabled) return
    setTooBig(list.filter((f) => f.size > MAX_FILE_BYTES).map((f) => f.name))
    setFiles((cur) => [...cur, ...list.filter((f) => f.size <= MAX_FILE_BYTES)])
  }
  return (
    <form
      onSubmit={(e) => { e.preventDefault(); submit() }}
      onDragOver={(e) => { if (e.dataTransfer.types.includes("Files")) e.preventDefault() }}
      onDrop={(e) => { if (e.dataTransfer.files.length) { e.preventDefault(); add(Array.from(e.dataTransfer.files)) } }}
      className={cn("rounded-xl border bg-background shadow-[0_1px_2px_rgb(0_0_0/0.04)] focus-within:border-border-strong", disabled && "bg-muted/60 shadow-none")}
    >
      {files.length > 0 && (
        <div className="flex flex-wrap gap-1.5 px-3 pt-3">
          {files.map((f, i) => (
            <span key={`${i}-${f.name}`} className="inline-flex h-7 items-center gap-1.5 rounded-md border bg-muted px-2 text-xs">
              <Paperclip className="size-3.5 text-muted-foreground" />
              <span className="max-w-40 truncate">{f.name}</span>
              <span className="text-faint tabular">{bytes(f.size)}</span>
              <button type="button" aria-label={`Remove ${f.name}`} onClick={() => setFiles(files.filter((x) => x !== f))} className="text-muted-foreground hover:text-foreground">
                <X className="size-3.5" />
              </button>
            </span>
          ))}
        </div>
      )}
      {tooBig.length > 0 && <div role="alert" className="px-4 pt-3 text-sm text-destructive">{tooBig.join(", ")} {tooBig.length === 1 ? "is" : "are"} over 24 MB</div>}
      <textarea
        ref={ref}
        rows={1}
        value={text}
        autoFocus={autoFocus}
        disabled={disabled}
        onChange={(e) => setText(e.target.value)}
        onPaste={(e) => { if (e.clipboardData.files.length) { e.preventDefault(); add(Array.from(e.clipboardData.files)) } }}
        onKeyDown={(e) => {
          if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); submit() }
          if (e.key === "Escape" && busy) onStop()
        }}
        placeholder={placeholder}
        id="composer"
        aria-label="Message"
        className="block max-h-60 w-full resize-none bg-transparent px-4 pt-3.5 pb-1 text-[15px] leading-6 outline-none placeholder:text-faint disabled:cursor-not-allowed"
      />
      <div className="flex items-center gap-1 px-2 pb-2">
        {picker}
        <input ref={pick} type="file" multiple hidden onChange={(e) => {
          add(Array.from(e.target.files ?? []))
          e.target.value = ""
        }} />
        <button
          type="button" aria-label="Attach files" disabled={disabled} onClick={() => pick.current?.click()}
          className="inline-flex size-7 items-center justify-center rounded-md text-muted-foreground hover:bg-accent hover:text-foreground disabled:pointer-events-none disabled:opacity-50"
        >
          <Paperclip className="size-4" />
        </button>
        {busy ? (
          <button type="button" onClick={onStop} aria-label="Stop" className="ml-auto inline-flex size-8 items-center justify-center rounded-lg bg-primary text-primary-foreground">
            <Square className="size-3 fill-current" />
          </button>
        ) : (
          <button
            type="submit"
            aria-label="Send"
            disabled={!canSend}
            className="ml-auto inline-flex size-8 items-center justify-center rounded-lg bg-primary text-primary-foreground transition-colors disabled:bg-muted disabled:text-faint"
          >
            <ArrowUp className="size-4" />
          </button>
        )}
      </div>
    </form>
  )
}
