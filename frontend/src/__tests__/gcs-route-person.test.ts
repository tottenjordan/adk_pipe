import { NextRequest } from "next/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ResolvedUser } from "@/lib/iap-identity";

vi.mock("@/lib/gcp-auth", () => ({ getAccessToken: vi.fn(async () => "ya29.token") }));
const resolveUser = vi.fn<() => Promise<ResolvedUser>>();
vi.mock("@/lib/iap-identity", () => ({ resolveUser: () => resolveUser() }));

import { GET } from "@/app/api/gcs/route";
import {
  emailSlug,
  isPersonPath,
  personOwnerSlug,
  personPathAccess,
  personRefsPrefix,
} from "@/lib/person-paths";

const ALICE = "alice.smith@example.com";
const ALICE_SLUG = "alice_smith_example_com";

const call = (path: string, bucket = "tt-bucket") =>
  GET(
    new NextRequest(
      `https://app.example/api/gcs?bucket=${bucket}&path=${encodeURIComponent(path)}`
    )
  );

describe("person paths", () => {
  it("slugs an email like runserver slug_for", () => {
    expect(emailSlug("admin@jordantotten.altostrat.com")).toBe(
      "admin_jordantotten_altostrat_com"
    );
    expect(emailSlug(" Alice.Smith@Example.com ")).toBe(ALICE_SLUG);
    expect(personRefsPrefix("b", ALICE)).toBe(`gs://b/person-refs/${ALICE_SLUG}/`);
  });

  it.each([
    ["person-refs/a_x_com/me.jpg", true, "a_x_com"],
    ["/person-refs/a_x_com/me.jpg", true, "a_x_com"],
    ["person-refs/me.jpg", true, null],
    ["person-refs//me.jpg", true, null],
    ["2026/creative_output/variants/a_x_com/c/k.png", true, "a_x_com"],
    ["2026/creative_output/variants/k.png", true, null],
    ["2026/creative_output/variants.png", false, null],
    ["2026/creative_output/hero.png", false, null],
    ["shares/tok/0.png", false, null],
    ["x/person-refs/a_x_com/me.jpg", false, null],
  ])("%s → person=%s owner=%s", (path, person, owner) => {
    expect(isPersonPath(path)).toBe(person);
    if (person) expect(personOwnerSlug(path)).toBe(owner);
  });

  it("allows only the owner (or local dev)", () => {
    const p = `person-refs/${ALICE_SLUG}/me.jpg`;
    expect(personPathAccess(p, { kind: "user", userId: ALICE })).toBe("allow");
    expect(personPathAccess(p, { kind: "user", userId: "bob@example.com" })).toBe(
      "not_found"
    );
    expect(personPathAccess("person-refs/me.jpg", { kind: "user", userId: ALICE })).toBe(
      "not_found"
    );
    expect(personPathAccess(p, { kind: "reject" })).toBe("unauthenticated");
    expect(personPathAccess(p, { kind: "local" })).toBe("allow");
  });
});

describe("/api/gcs owner scoping", () => {
  const fetchMock = vi.fn(
    async (url: string | URL | Request) =>
      new Response(new Uint8Array([1, 2, 3]), {
        status: 200,
        headers: { "content-type": String(url).includes(".png") ? "image/png" : "image/jpeg" },
      })
  );

  beforeEach(() => {
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    fetchMock.mockClear();
    resolveUser.mockReset();
  });

  it("serves a person photo to its owner, privately and uncached", async () => {
    resolveUser.mockResolvedValue({ kind: "user", userId: ALICE });
    const res = await call(`person-refs/${ALICE_SLUG}/me.jpg`);
    expect(res.status).toBe(200);
    expect(res.headers.get("cache-control")).toBe("private, no-store");
    expect(String(fetchMock.mock.calls[0][0])).toBe(
      `https://storage.googleapis.com/storage/v1/b/tt-bucket/o/person-refs%2F${ALICE_SLUG}%2Fme.jpg?alt=media`
    );
  });

  it("answers 404 to another user without fetching", async () => {
    resolveUser.mockResolvedValue({ kind: "user", userId: "bob@example.com" });
    for (const path of [
      `person-refs/${ALICE_SLUG}/me.jpg`,
      `2026/creative_output/variants/${ALICE_SLUG}/c/k.png`,
      "person-refs/me.jpg",
    ]) {
      expect((await call(path)).status).toBe(404);
    }
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("serves a variant to its owner", async () => {
    resolveUser.mockResolvedValue({ kind: "user", userId: ALICE });
    const res = await call(`2026/creative_output/variants/${ALICE_SLUG}/c/k.png`);
    expect(res.status).toBe(200);
  });

  it("answers 401 when the caller can't be identified", async () => {
    resolveUser.mockResolvedValue({ kind: "reject", status: 401 });
    expect((await call(`person-refs/${ALICE_SLUG}/me.jpg`)).status).toBe(401);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("passes person paths through in local dev", async () => {
    resolveUser.mockResolvedValue({ kind: "local" });
    expect((await call(`person-refs/${ALICE_SLUG}/me.jpg`)).status).toBe(200);
  });

  it("leaves other paths unchanged (no identity check, same caching)", async () => {
    const res = await call("2026/creative_output/hero.png");
    expect(res.status).toBe(200);
    expect(res.headers.get("cache-control")).toBe("private, max-age=300");
    expect(res.headers.get("content-type")).toBe("image/png");
    expect(resolveUser).not.toHaveBeenCalled();
  });
});
