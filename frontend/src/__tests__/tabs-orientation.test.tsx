// @vitest-environment jsdom
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";

describe("Tabs", () => {
  it("passes orientation to Base UI (aria-orientation + arrow keys)", () => {
    render(
      <Tabs orientation="vertical" defaultValue="a">
        <TabsList>
          <TabsTrigger value="a">A</TabsTrigger>
          <TabsTrigger value="b">B</TabsTrigger>
        </TabsList>
      </Tabs>
    );
    expect(screen.getByRole("tablist")).toHaveAttribute("aria-orientation", "vertical");
  });
});
