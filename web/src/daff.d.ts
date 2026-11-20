// daff ships no types; the table diff viewer uses the few calls below.
declare module "daff" {
  export class TableView { constructor(rows: string[][]); getCell(x: number, y: number): unknown; get_width(): number; get_height(): number }
  export class CompareFlags {}
  export class TableDiff { constructor(alignment: unknown, flags: CompareFlags); hilite(out: TableView): boolean }
  export function compareTables(a: TableView, b: TableView): { align(): unknown }
}
