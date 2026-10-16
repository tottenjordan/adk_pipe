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

/** The Base UI select's trigger (a combobox showing the chosen label). */
const trigger = () => screen.getByRole("combobox", { name: /Person \(optional\)/ });

/** Open the select (once the list loaded) and pick the option named `name`. */
async function choose(name: string) {
  await waitFor(() => expect(trigger()).not.toHaveAttribute("data-disabled"));
  fireEvent.click(trigger());
  const option = await screen.findByRole("option", { name });
  // Base UI commits a mouse click only when the press started on the item.
  fireEvent.pointerDown(option);
  fireEvent.click(option);
}

describe("PersonSelect", () => {
  it("lists active consents and reports the chosen person", async () => {
    listPersonRefs.mockResolvedValue({ personRefs: REFS, prefix: null });
    const onChange = vi.fn();
    render(<PersonSelect value={null} onChange={onChange} />);
    await screen.findByRole("combobox", { name: /Person \(optional\)/ });
    expect(trigger()).toHaveTextContent("No person");
    // The shared ui/select (like every other picker), not a native <select>.
    expect(trigger().tagName).not.toBe("SELECT");
    expect(trigger()).toHaveAttribute("data-slot", "select-trigger");
    expect(trigger()).toHaveAccessibleDescription(/The agents decide per concept/);
    await choose("Sam");
    expect(onChange).toHaveBeenLastCalledWith({
      uri: "gs://b/person-refs/a-1/sam.png",
      consentId: "consent-5678",
    });
    await choose("No person");
    expect(onChange).toHaveBeenLastCalledWith(null);
    expect(screen.getByRole("link", { name: /Manage people/ })).toHaveAttribute("href", "/people");
  });

  it("describes the person-safe styles, not photographic only", async () => {
    listPersonRefs.mockResolvedValue({ personRefs: REFS, prefix: null });
    render(<PersonSelect value={null} onChange={vi.fn()} />);
    expect(
      await screen.findByText(
        /The agents decide per concept whether to feature this person \(person-safe styles only: photographic, 3D, comic, flat illustration, collage\)\./
      )
    ).toBeInTheDocument();
    expect(screen.queryByText(/photographic concepts only/)).not.toBeInTheDocument();
  });

  it("shows the chosen person as selected", async () => {
    listPersonRefs.mockResolvedValue({ personRefs: REFS, prefix: null });
    render(
      <PersonSelect
        value={{ uri: "gs://b/person-refs/a-1/me.jpg", consentId: "consent-1234" }}
        onChange={() => {}}
      />,
    );
    await waitFor(() => expect(trigger()).toHaveTextContent("Me"));
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
    expect(onChange).toHaveBeenCalledTimes(1);
  });

  it("drops a stale person exactly once, even if the parent never clears it", async () => {
    listPersonRefs.mockResolvedValue({ personRefs: REFS, prefix: null });
    const gone = { uri: "gs://b/person-refs/a-1/gone.jpg", consentId: "consent-gone" };
    const onChange = vi.fn();
    const { rerender } = render(<PersonSelect value={gone} onChange={(v) => onChange(v)} />);
    await waitFor(() => expect(onChange).toHaveBeenCalledWith(null));
    // A parent that ignores the call and passes a fresh inline onChange.
    rerender(<PersonSelect value={gone} onChange={(v) => onChange(v)} />);
    rerender(<PersonSelect value={gone} onChange={(v) => onChange(v)} />);
    expect(onChange).toHaveBeenCalledTimes(1);
    expect(trigger()).toHaveTextContent("No person");
  });

  it("drops a stale person set before the list loads, once it loads", async () => {
    let resolve!: (v: unknown) => void;
    listPersonRefs.mockReturnValue(new Promise((r) => (resolve = r)));
    const onChange = vi.fn();
    const { rerender } = render(<PersonSelect value={null} onChange={onChange} />);
    // Duplicate brief applied while the list is still loading.
    rerender(
      <PersonSelect
        value={{ uri: "gs://b/person-refs/a-1/gone.jpg", consentId: "consent-gone" }}
        onChange={onChange}
      />,
    );
    expect(onChange).not.toHaveBeenCalled();
    resolve({ personRefs: REFS, prefix: null });
    await waitFor(() => expect(onChange).toHaveBeenCalledWith(null));
    expect(onChange).toHaveBeenCalledTimes(1);
  });

  it("drops a stale person passed in after the list loaded", async () => {
    listPersonRefs.mockResolvedValue({ personRefs: REFS, prefix: null });
    const onChange = vi.fn();
    const { rerender } = render(<PersonSelect value={null} onChange={onChange} />);
    await waitFor(() => expect(trigger()).not.toHaveAttribute("data-disabled"));
    // "Duplicate here" on the runs list: the brief's person was since revoked.
    rerender(
      <PersonSelect
        value={{ uri: "gs://b/person-refs/a-1/gone.jpg", consentId: "consent-gone" }}
        onChange={onChange}
      />,
    );
    await waitFor(() => expect(onChange).toHaveBeenCalledWith(null));
    expect(onChange).toHaveBeenCalledTimes(1);
  });

  it("never drops an active person, before or after the list loads", async () => {
    listPersonRefs.mockResolvedValue({ personRefs: REFS, prefix: null });
    const onChange = vi.fn();
    const me = { uri: "gs://b/person-refs/a-1/me.jpg", consentId: "consent-1234" };
    const { rerender } = render(<PersonSelect value={me} onChange={onChange} />);
    await waitFor(() => expect(trigger()).toHaveTextContent("Me"));
    rerender(
      <PersonSelect
        value={{ uri: "gs://b/person-refs/a-1/sam.png", consentId: "consent-5678" }}
        onChange={onChange}
      />,
    );
    expect(trigger()).toHaveTextContent("Sam");
    expect(onChange).not.toHaveBeenCalled();
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
