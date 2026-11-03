import { SettingRow } from "@/components/panel"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"

/** The value a setting falls back to, and which tier supplies it. `label` is the value as people read it; empty when it is not known. */
export type Inherited<T> = { value: T; label: string; from: "Workspace" | "Built-in" }

/** `bare` leaves out the label row, for a control that sits in a list which names it already. */
const Row = ({ label, overridden, bare, children }: { label: string; overridden: boolean; bare?: boolean; children: React.ReactNode }) =>
  bare ? <>{children}</> : <SettingRow label={label} overridden={overridden}>{children}</SettingRow>

const DEFAULT = "__default"
const defaultText = (i: { label: string; from: string }) => (i.label && i.label.toLowerCase() !== "default" ? `${i.from} default (${i.label})` : `${i.from} default`)

/**
 * One control for a setting that may be inherited. The first item is the default and sets null;
 * with no value of its own the trigger shows that item, so the applied value is always in view.
 */
export function InheritedSelect<T extends string>({ label, value, inherited, options, onChange, disabled, className, bare }: {
  label: string
  value: T | null
  inherited: Inherited<T>
  options: { value: T; label: string; icon?: React.ReactNode }[]
  onChange: (v: T | null) => void
  disabled?: boolean
  className?: string
  bare?: boolean
}) {
  const listed = value && !options.some((o) => o.value === value) ? [{ value, label: value }, ...options] : options
  return (
    <Row label={label} overridden={value !== null} bare={bare}>
      <Select value={value ?? DEFAULT} disabled={disabled} onValueChange={(v) => onChange(v === DEFAULT ? null : (v as T))}>
        <SelectTrigger size="sm" className={className ?? "w-full max-w-72"} aria-label={label}><SelectValue /></SelectTrigger>
        <SelectContent>
          <SelectItem value={DEFAULT}>{defaultText(inherited)}</SelectItem>
          {listed.map((o) => <SelectItem key={o.value} value={o.value}>{o.icon}{o.label}</SelectItem>)}
        </SelectContent>
      </Select>
    </Row>
  )
}

/** On, off or the inherited value, in one segmented control: `Workspace default (On) | On | Off`. */
export function InheritedSegmented({ label, value, inherited, onChange, disabled, bare }: {
  label: string
  value: boolean | null
  /** `value` is null when the inherited value is not known. */
  inherited: { value: boolean | null; from: Inherited<boolean>["from"] }
  onChange: (v: boolean | null) => void
  disabled?: boolean
  bare?: boolean
}) {
  const current = value === null ? DEFAULT : value ? "on" : "off"
  return (
    <Row label={label} overridden={value !== null} bare={bare}>
      <ToggleGroup
        type="single" variant="segmented" size="sm" value={current} disabled={disabled} aria-label={label}
        onValueChange={(v) => { if (v && v !== current) onChange(v === DEFAULT ? null : v === "on") }}
      >
        <ToggleGroupItem value={DEFAULT} className="px-2.5 text-xs">{defaultText({ from: inherited.from, label: inherited.value === null ? "" : inherited.value ? "On" : "Off" })}</ToggleGroupItem>
        <ToggleGroupItem value="on" className="px-2.5 text-xs">On</ToggleGroupItem>
        <ToggleGroupItem value="off" className="px-2.5 text-xs">Off</ToggleGroupItem>
      </ToggleGroup>
    </Row>
  )
}
