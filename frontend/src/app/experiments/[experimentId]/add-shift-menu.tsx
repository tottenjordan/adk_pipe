"use client";

import { Menu } from "@base-ui/react/menu";
import { PlusIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import type { ShiftKind } from "@/lib/experiments";
import { KIND_LABELS, SHIFT_KINDS, SHIFT_PRESETS, type ShiftPresetId } from "@/lib/shifts";

// Items keep the global :focus-visible outline (globals.css), pulled inside the
// item, so keyboard focus stays visible in forced-colors mode too.
const ITEM =
  "block w-full cursor-default rounded-sm px-2.5 py-1.5 text-left text-foreground select-none focus-visible:outline-offset-[-2px] data-highlighted:bg-muted";
const GROUP_LABEL = "px-2.5 pt-1.5 pb-1 text-xs text-muted-foreground";

/**
 * The "Add shift" menu: four kinds and three ready-made presets. Base UI Menu
 * supplies the menu semantics: arrow keys, typeahead, focus moved into the menu
 * and back to the trigger on Escape.
 */
export function AddShiftMenu({
  onAdd,
  disabled,
}: {
  onAdd: (kind: ShiftKind | null, preset: ShiftPresetId | null) => void;
  disabled: boolean;
}) {
  return (
    <Menu.Root modal={false}>
      <Menu.Trigger disabled={disabled} render={<Button variant="outline" size="sm" />}>
        <PlusIcon /> Add shift
      </Menu.Trigger>
      <Menu.Portal>
        <Menu.Positioner side="bottom" align="end" sideOffset={4} className="z-50">
          <Menu.Popup className="w-72 rounded-md border border-border bg-popover p-1 text-sm text-popover-foreground shadow-md outline-hidden">
            <Menu.Group>
              <Menu.GroupLabel className={GROUP_LABEL}>What changes</Menu.GroupLabel>
              {SHIFT_KINDS.map((k) => (
                <Menu.Item key={k} className={ITEM} onClick={() => onAdd(k, null)}>
                  {KIND_LABELS[k]}
                </Menu.Item>
              ))}
            </Menu.Group>
            <Menu.Separator className="my-1 border-t border-border" />
            <Menu.Group>
              <Menu.GroupLabel className={GROUP_LABEL}>Ready-made</Menu.GroupLabel>
              {SHIFT_PRESETS.map((p) => (
                <Menu.Item key={p.id} label={p.label} className={ITEM} onClick={() => onAdd(null, p.id)}>
                  <span className="block">{p.label}</span>
                  <span className="block text-xs text-muted-foreground">{p.description}</span>
                </Menu.Item>
              ))}
            </Menu.Group>
          </Menu.Popup>
        </Menu.Positioner>
      </Menu.Portal>
    </Menu.Root>
  );
}
