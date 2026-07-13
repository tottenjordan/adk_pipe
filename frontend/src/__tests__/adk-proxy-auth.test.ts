import { describe, it, expect } from "vitest";
import {
  backendNeedsAuth,
  stripInboundCredentials,
} from "@/app/api/adk/[...path]/route";

describe("backendNeedsAuth", () => {
  it("is true for a remote https backend (private Cloud Run)", () => {
    expect(backendNeedsAuth("https://trend-trawler-api-abc.run.app")).toBe(true);
  });
  it("is false for localhost dev backend", () => {
    expect(backendNeedsAuth("http://localhost:8000")).toBe(false);
  });
});

describe("stripInboundCredentials", () => {
  it("removes IAP's edge-injected credentials so they never reach the backend", () => {
    // Simulates the request IAP forwards to the container.
    const headers = new Headers({
      authorization: "Bearer iap-jwt-wrong-audience",
      "x-serverless-authorization": "Bearer iap-serverless-token",
      cookie: "GCP_IAP_XSRF_NONCE=abc; GCP_IAPAuth=xyz",
      "x-goog-iap-jwt-assertion": "assertion",
      "x-goog-authenticated-user-email": "accounts.google.com:user@example.com",
      "x-goog-authenticated-user-id": "accounts.google.com:12345",
      "content-type": "application/json",
    });

    stripInboundCredentials(headers);

    // X-Serverless-Authorization is the critical one: Cloud Run ingress checks it in
    // preference to Authorization, so a leaked IAP token here causes a 401.
    expect(headers.get("x-serverless-authorization")).toBeNull();
    expect(headers.get("authorization")).toBeNull();
    expect(headers.get("cookie")).toBeNull();
    expect(headers.get("x-goog-iap-jwt-assertion")).toBeNull();
    expect(headers.get("x-goog-authenticated-user-email")).toBeNull();
    expect(headers.get("x-goog-authenticated-user-id")).toBeNull();
    // Non-credential headers are preserved.
    expect(headers.get("content-type")).toBe("application/json");
  });
});
