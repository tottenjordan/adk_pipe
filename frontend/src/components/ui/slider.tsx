"use client";

import { Slider as SliderPrimitive } from "@base-ui/react/slider";
import { cn } from "@/lib/utils";

/**
 * Single-value slider over Base UI's Slider (keyboard: arrows step, Page Up/Down
 * take `largeStep`, Home/End jump to the ends). The thumb wraps a native range
 * input, so `aria-label`/`aria-labelledby` and `valueText` (aria-valuetext) reach
 * assistive tech. `fill` draws the track from `min` to the value; turn it off for
 * scales with no natural zero (e.g. right ↔ backwards).
 */
export function Slider({
  value,
  onValueChange,
  min,
  max,
  step = 0.01,
  largeStep,
  valueText,
  fill = true,
  marks,
  disabled,
  className,
  "aria-label": ariaLabel,
  "aria-labelledby": ariaLabelledBy,
  id,
}: {
  value: number;
  onValueChange: (value: number) => void;
  min: number;
  max: number;
  step?: number;
  largeStep?: number;
  /** Spoken value, e.g. "40% of readers". */
  valueText?: (value: number) => string;
  fill?: boolean;
  /** Values to tick on the track (e.g. the preset value). */
  marks?: number[];
  disabled?: boolean;
  className?: string;
  "aria-label"?: string;
  "aria-labelledby"?: string;
  id?: string;
}) {
  const at = (v: number) => `${((v - min) / (max - min)) * 100}%`;
  return (
    <SliderPrimitive.Root
      value={value}
      onValueChange={(v) => onValueChange(Array.isArray(v) ? v[0] : (v as number))}
      min={min}
      max={max}
      step={step}
      largeStep={largeStep}
      disabled={disabled}
      thumbAlignment="edge"
      className={cn("relative w-full touch-none select-none data-[disabled]:opacity-50", className)}
      data-slot="slider"
    >
      <SliderPrimitive.Control className="flex h-6 w-full cursor-pointer items-center">
        <SliderPrimitive.Track className="relative h-1 w-full rounded-full bg-foreground/12">
          {fill && <SliderPrimitive.Indicator className="rounded-full bg-primary" />}
          {marks?.map((m) => (
            <span
              key={m}
              aria-hidden
              className="absolute top-1/2 h-2.5 w-px -translate-x-1/2 -translate-y-1/2 bg-foreground/35"
              style={{ left: at(m) }}
            />
          ))}
          <SliderPrimitive.Thumb
            id={id}
            aria-label={ariaLabel}
            aria-labelledby={ariaLabelledBy}
            getAriaValueText={valueText ? (_formatted, v) => valueText(v) : undefined}
            className="size-4 rounded-full border-2 border-primary bg-card shadow-[0_1px_2px_rgb(26_29_33/0.2)] outline-none has-[:focus-visible]:ring-3 has-[:focus-visible]:ring-ring/50 data-[dragging]:scale-110 motion-reduce:transition-none"
          />
        </SliderPrimitive.Track>
      </SliderPrimitive.Control>
    </SliderPrimitive.Root>
  );
}
