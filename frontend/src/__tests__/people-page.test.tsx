// @vitest-environment jsdom
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { CONSENT_TEXT, CONSENT_TEXT_VERSION } from "@/lib/person-consent";
import { PersonRefError, PREFIX_RULE, type PersonRef } from "@/lib/person-refs";

const listPersonRefs = vi.fn();
const createPersonRef = vi.fn();
const revokePersonRef = vi.fn();
vi.mock("@/lib/api", () => ({
  listPersonRefs: (...a: unknown[]) => listPersonRefs(...a),
  createPersonRef: (...a: unknown[]) => createPersonRef(...a),
  revokePersonRef: (...a: unknown[]) => revokePersonRef(...a),
}));

import PeoplePage from "@/app/people/page";

const PREFIX = "gs://tt-bucket/person-refs/alice_x_com/";
const person = (over: Partial<PersonRef> = {}): PersonRef => ({
  consent_id: "cid_AAAAAAAAAAAA",
  photo_uri: `${PREFIX}me.jpg`,
  label: "Alice",
  subject: "self",
  adult_attested: true,
  allow_public_share: false,
  consent_text_version: CONSENT_TEXT_VERSION,
  created_at: "2026-10-09T10:00:00Z",
  ...over,
});

const writeText = vi.fn();
beforeEach(() => {
  listPersonRefs.mockReset();
  createPersonRef.mockReset();
  revokePersonRef.mockReset();
  writeText.mockReset().mockResolvedValue(undefined);
  Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
});


async function rows() {
  const list = await screen.findByRole("list", { name: "Your people" });
  return within(list).getAllByRole("listitem");
}

describe("PeoplePage", () => {
  it("shows the consent text and the copyable upload prefix", async () => {
    listPersonRefs.mockResolvedValue({ personRefs: [], prefix: PREFIX });
    render(<PeoplePage />);
    expect(screen.getByTestId("consent-text")).toHaveTextContent(CONSENT_TEXT);
    await waitFor(() => expect(screen.getByTestId("person-prefix")).toHaveTextContent(PREFIX));
    expect(screen.getByLabelText("Photo")).toHaveValue(PREFIX);
    fireEvent.click(screen.getByRole("button", { name: "Copy" }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(PREFIX));
    expect(await screen.findByText("No one yet. Add a person above.")).toBeInTheDocument();
  });

  it("explains the folder rule when the api has no prefix", async () => {
    listPersonRefs.mockResolvedValue({ personRefs: [], prefix: null });
    render(<PeoplePage />);
    await screen.findByText("No one yet. Add a person above.");
    expect(screen.getByTestId("person-prefix")).toHaveTextContent(PREFIX_RULE);
    expect(screen.queryByRole("button", { name: "Copy" })).not.toBeInTheDocument();
  });

  it("lists people with a proxied thumbnail and public-share status", async () => {
    listPersonRefs.mockResolvedValue({
      personRefs: [person(), person({ consent_id: "cid_BBBBBBBBBBBB", label: "Bob",
        subject: "third_party_with_consent", allow_public_share: true, photo_uri: `${PREFIX}bob.png` })],
      prefix: PREFIX,
    });
    render(<PeoplePage />);
    const items = await rows();
    expect(items).toHaveLength(2);
    const img = within(items[0]).getByRole("img", { name: "Photo of Alice" });
    expect(img).toHaveAttribute(
      "src", "/api/gcs?bucket=tt-bucket&path=person-refs%2Falice_x_com%2Fme.jpg");
    expect(within(items[0]).getByText("Public links: no")).toBeInTheDocument();
    expect(within(items[1]).getByText("Someone who agreed")).toBeInTheDocument();
    expect(within(items[1]).getByText("Public links: yes")).toBeInTheDocument();
  });

  it("requires the adult attestation and registers with the current consent version", async () => {
    listPersonRefs.mockResolvedValue({ personRefs: [], prefix: PREFIX });
    createPersonRef.mockResolvedValue(person({ label: "Carol", consent_id: "cid_CCCCCCCCCCCC" }));
    render(<PeoplePage />);
    await screen.findByText("No one yet. Add a person above.");
    fireEvent.change(screen.getByLabelText("Photo"), { target: { value: `${PREFIX}carol.jpg` } });
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: " Carol " } });
    fireEvent.click(screen.getByLabelText("Someone who agreed"));
    const submit = screen.getByRole("button", { name: "Register photo" });
    expect(submit).toBeDisabled();
    fireEvent.click(screen.getByLabelText(/is an adult and agreed to appear/));
    fireEvent.click(screen.getByLabelText(/public share links/));
    expect(submit).toBeEnabled();
    await act(async () => {
      fireEvent.click(submit);
    });
    expect(createPersonRef).toHaveBeenCalledWith({
      photo_uri: `${PREFIX}carol.jpg`,
      label: "Carol",
      subject: "third_party_with_consent",
      adult_attested: true,
      allow_public_share: true,
      consent_text_version: CONSENT_TEXT_VERSION,
    });
    const items = await rows();
    expect(within(items[0]).getByText("Carol")).toBeInTheDocument();
    // the form resets, attestation unticked
    expect(screen.getByLabelText(/is an adult and agreed to appear/)).not.toBeChecked();
    expect(screen.getByLabelText("Photo")).toHaveValue(PREFIX);
  });

  it("shows a friendly error when registration is refused", async () => {
    listPersonRefs.mockResolvedValue({ personRefs: [], prefix: PREFIX });
    createPersonRef.mockRejectedValue(new PersonRefError("invalid_photo_uri", 400));
    render(<PeoplePage />);
    await screen.findByText("No one yet. Add a person above.");
    fireEvent.change(screen.getByLabelText("Photo"), { target: { value: "gs://x/y.jpg" } });
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "X" } });
    fireEvent.click(screen.getByLabelText(/is an adult and agreed to appear/));
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Register photo" }));
    });
    expect(await screen.findByRole("alert")).toHaveTextContent(/directly inside your folder/);
  });

  it("revokes after confirming that the photo and its images are deleted", async () => {
    listPersonRefs.mockResolvedValue({ personRefs: [person()], prefix: PREFIX });
    revokePersonRef.mockResolvedValue(undefined);
    render(<PeoplePage />);
    const [item] = await rows();
    fireEvent.click(within(item).getByRole("button", { name: "Revoke Alice" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("Revoke Alice?")).toBeInTheDocument();
    expect(within(dialog).getByText(/Deletes the photo and every image made with it/)).toBeInTheDocument();
    await act(async () => {
      fireEvent.click(within(dialog).getByRole("button", { name: "Revoke and delete" }));
    });
    expect(revokePersonRef).toHaveBeenCalledWith("cid_AAAAAAAAAAAA");
    expect(await screen.findByText("No one yet. Add a person above.")).toBeInTheDocument();
  });

  it("keeps the row with an error when the revoke is incomplete", async () => {
    listPersonRefs.mockResolvedValue({ personRefs: [person()], prefix: PREFIX });
    revokePersonRef.mockRejectedValue(new PersonRefError("revoke_incomplete", 502));
    render(<PeoplePage />);
    const [item] = await rows();
    fireEvent.click(within(item).getByRole("button", { name: "Revoke Alice" }));
    const dialog = await screen.findByRole("dialog");
    await act(async () => {
      fireEvent.click(within(dialog).getByRole("button", { name: "Revoke and delete" }));
    });
    const [still] = await rows();
    expect(within(still).getByRole("alert")).toHaveTextContent(/Revoke again to finish/);
  });

  it("reports a load failure", async () => {
    listPersonRefs.mockRejectedValue(new PersonRefError(null, 500));
    render(<PeoplePage />);
    expect(await screen.findByRole("alert")).toHaveTextContent(/People could not load/);
  });
});

describe("person-refs api client", () => {
  it("is covered with real fetch calls", async () => {
    const api = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
    const json = (body: unknown, status = 200) =>
      new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
    const fetchMock = vi.fn(async (_url: string, _init?: RequestInit) =>
      json({ person_refs: [person()], prefix: PREFIX, consent_text_version: CONSENT_TEXT_VERSION }));
    vi.stubGlobal("fetch", fetchMock);
    expect(await api.listPersonRefs()).toEqual({ personRefs: [person()], prefix: PREFIX });
    expect(fetchMock.mock.calls[0][0]).toBe("/api/adk/person-refs/me");

    fetchMock.mockImplementationOnce(async () => json(person()));
    const payload = {
      photo_uri: `${PREFIX}me.jpg`, label: "Alice", subject: "self" as const,
      adult_attested: true, allow_public_share: false, consent_text_version: CONSENT_TEXT_VERSION,
    };
    await api.createPersonRef(payload);
    expect(fetchMock.mock.calls[1][0]).toBe("/api/adk/person-refs/me");
    expect(fetchMock.mock.calls[1][1]?.method).toBe("POST");
    expect(JSON.parse(fetchMock.mock.calls[1][1]?.body as string)).toEqual(payload);

    fetchMock.mockImplementationOnce(async () => new Response(null, { status: 204 }));
    await api.revokePersonRef("cid_AAAAAAAAAAAA");
    expect(fetchMock.mock.calls[2][0]).toBe("/api/adk/person-refs/me/cid_AAAAAAAAAAAA");
    expect(fetchMock.mock.calls[2][1]?.method).toBe("DELETE");

    fetchMock.mockImplementationOnce(async () =>
      json({ detail: { reason: "stale_consent_text", message: "x" } }, 400));
    const err = await api.createPersonRef(payload).catch((e) => e);
    expect(err).toBeInstanceOf(PersonRefError);
    expect(err.reason).toBe("stale_consent_text");
    expect(err.message).toMatch(/consent text has changed/);

    fetchMock.mockImplementationOnce(async () => new Response("Not found", { status: 404 }));
    const err2 = await api.listPersonRefs().catch((e) => e);
    expect(err2.reason).toBeNull();
    expect(err2.status).toBe(404);

    fetchMock.mockImplementationOnce(async () => json({ person_refs: "nope", prefix: "" }));
    expect(await api.listPersonRefs()).toEqual({ personRefs: [], prefix: null });
    vi.unstubAllGlobals();
  });
});
