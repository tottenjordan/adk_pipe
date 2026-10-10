// @vitest-environment jsdom
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const listPersonRefs = vi.fn();
vi.mock("@/lib/api", () => ({
  listPersonRefs: () => listPersonRefs(),
}));

import { PersonSelect } from "@/components/person-select";

const REFS = [
  {
    consent_id: "consent-1234",
    photo_uri: "gs://b/person-refs/a-1/me.jpg",
    label: "Me",
    subject: "self",
    adult_attested: true,
    allow_public_share: false,
    consent_text_version: "2026-10-09",
    created_at: "2026-10-09T00:00:00Z",
  },
  {
    consent_id: "consent-5678",
    photo_uri: "gs://b/person-refs/a-1/sam.png",
    label: "Sam",
    subject: "third_party_with_consent",
    adult_attested: true,
    allow_public_share: true,
    consent_text_version: "2026-10-09",
    created_at: "2026-10-09T00:00:00Z",
  },
];

beforeEach(() => {
  listPersonRefs.mockReset();
});

describe("PersonSelect", () => {
  it("lists active consents and reports the chosen person", async () => {
    listPersonRefs.mockResolvedValue({ personRefs: REFS, prefix: null });
    const onChange = vi.fn();
    render(<PersonSelect value={null} onChange={onChange} />);
    const select = await screen.findByRole("combobox", { name: /Person \(optional\)/ });
    await waitFor(() => expect(screen.getByRole("option", { name: "Sam" })).toBeInTheDocument());
    expect(screen.getByRole("option", { name: "No person" })).toBeInTheDocument();
    fireEvent.change(select, { target: { value: "consent-5678" } });
    expect(onChange).toHaveBeenLastCalledWith({
      uri: "gs://b/person-refs/a-1/sam.png",
      consentId: "consent-5678",
    });
    fireEvent.change(select, { target: { value: "" } });
    expect(onChange).toHaveBeenLastCalledWith(null);
    expect(screen.getByRole("link", { name: /Manage people/ })).toHaveAttribute("href", "/people");
  });

  it("shows the chosen person as selected", async () => {
    listPersonRefs.mockResolvedValue({ personRefs: REFS, prefix: null });
    render(
      <PersonSelect
        value={{ uri: "gs://b/person-refs/a-1/me.jpg", consentId: "consent-1234" }}
        onChange={() => {}}
      />,
    );
    await waitFor(() =>
      expect(screen.getByRole("combobox")).toHaveValue("consent-1234"),
    );
  });

  it("drops a duplicated person whose consent is no longer active", async () => {
    listPersonRefs.mockResolvedValue({ personRefs: REFS, prefix: null });
    const onChange = vi.fn();
    render(
      <PersonSelect
        value={{ uri: "gs://b/person-refs/a-1/gone.jpg", consentId: "consent-gone" }}
        onChange={onChange}
      />,
    );
    await waitFor(() => expect(onChange).toHaveBeenCalledWith(null));
  });

  it("points to /people when nobody is registered", async () => {
    listPersonRefs.mockResolvedValue({ personRefs: [], prefix: null });
    render(<PersonSelect value={null} onChange={() => {}} />);
    expect(await screen.findByText(/No people registered yet/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Register a person/ })).toHaveAttribute(
      "href",
      "/people",
    );
  });

  it("shows a carried-over person with Remove when the list fails", async () => {
    listPersonRefs.mockImplementation(() => Promise.reject(new Error("nope")));
    const onChange = vi.fn();
    render(
      <PersonSelect
        value={{ uri: "gs://b/person-refs/a-1/me.jpg", consentId: "consent-1234" }}
        onChange={onChange}
      />,
    );
    expect(
      await screen.findByText("Person: saved person (couldn't load your people)"),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Remove" }));
    expect(onChange).toHaveBeenCalledWith(null);
  });

  it("degrades quietly when the list fails", async () => {
    listPersonRefs.mockImplementation(() => Promise.reject(new Error("nope")));
    render(<PersonSelect value={null} onChange={() => {}} />);
    expect(await screen.findByText(/People couldn't be loaded/)).toBeInTheDocument();
  });
});
