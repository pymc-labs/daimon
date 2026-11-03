import { SettingRow } from "@/components/panel"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"

/** The value a setting falls back to, and which tier supplies it. `label` is the value as people read it. */
export type Inherited<T> = { value: T; label: string; from: "Workspace" | "Built-in" }

const DEFAULT = "__default"
const defaultText = (i: { label: string; from: string }) => `${i.from} default (${i.label})`

/**
 * One control for a setting that may be inherited. The first item is the default and sets null;
 * with no value of its own the trigger shows that item, so the applied value is always in view.
 */
export function InheritedSelect<T extends string>({ label, value, inherited, options, onChange, disabled, className }: {
  label: string
  value: T | null
  inherited: Inherited<T>
  options: { value: T; label: string; icon?: React.ReactNode }[]
  onChange: (v: T | null) => void
  disabled?: boolean
  className?: string
}) {
  const listed = value && !options.some((o) => o.value === value) ? [{ value, label: value }, ...options] : options
  return (
    <SettingRow label={label} overridden={value !== null}>
      <Select value={value ?? DEFAULT} disabled={disabled} onValueChange={(v) => onChange(v === DEFAULT ? null : (v as T))}>
        <SelectTrigger size="sm" className={className ?? "w-full max-w-72"} aria-label={label}><SelectValue /></SelectTrigger>
        <SelectContent>
          <SelectItem value={DEFAULT}>{defaultText(inherited)}</SelectItem>
          {listed.map((o) => <SelectItem key={o.value} value={o.value}>{o.icon}{o.label}</SelectItem>)}
        </SelectContent>
      </Select>
    </SettingRow>
  )
}

/** On, off or the inherited value, in one segmented control: `Workspace default (On) | On | Off`. */
export function InheritedSegmented({ label, value, inherited, onChange, disabled }: {
  label: string
  value: boolean | null
  inherited: { value: boolean; from: Inherited<boolean>["from"] }
  onChange: (v: boolean | null) => void
  disabled?: boolean
}) {
  const current = value === null ? DEFAULT : value ? "on" : "off"
  return (
    <SettingRow label={label} overridden={value !== null}>
      <ToggleGroup
        type="single" variant="segmented" size="sm" value={current} disabled={disabled} aria-label={label}
        onValueChange={(v) => { if (v && v !== current) onChange(v === DEFAULT ? null : v === "on") }}
      >
        <ToggleGroupItem value={DEFAULT} className="px-2.5 text-xs">{defaultText({ from: inherited.from, label: inherited.value ? "On" : "Off" })}</ToggleGroupItem>
        <ToggleGroupItem value="on" className="px-2.5 text-xs">On</ToggleGroupItem>
        <ToggleGroupItem value="off" className="px-2.5 text-xs">Off</ToggleGroupItem>
      </ToggleGroup>
    </SettingRow>
  )
}
