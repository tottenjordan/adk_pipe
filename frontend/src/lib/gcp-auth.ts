const METADATA_BASE =
  "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default";
const METADATA_HEADERS = { "Metadata-Flavor": "Google" };

/** OAuth access token for calling Google APIs (e.g. Cloud Storage). ADC on Cloud Run;
 *  falls back to the gcloud CLI for local dev. */
export async function getAccessToken(): Promise<string> {
  try {
    const res = await fetch(`${METADATA_BASE}/token`, {
      headers: METADATA_HEADERS,
      signal: AbortSignal.timeout(1000),
    });
    if (!res.ok) throw new Error("metadata token non-ok");
    return (await res.json()).access_token as string;
  } catch {
    const { execSync } = await import("child_process");
    return execSync("gcloud auth application-default print-access-token", {
      encoding: "utf-8",
    }).trim();
  }
}

/** Google-signed ID token whose `aud` is `audience` — used to call a private Cloud Run
 *  service. The metadata `identity` endpoint returns the raw JWT as text. */
export async function getIdentityToken(audience: string): Promise<string> {
  try {
    const res = await fetch(
      `${METADATA_BASE}/identity?audience=${encodeURIComponent(audience)}`,
      { headers: METADATA_HEADERS, signal: AbortSignal.timeout(1000) },
    );
    if (!res.ok) throw new Error("metadata identity non-ok");
    return (await res.text()).trim();
  } catch {
    const { execSync } = await import("child_process");
    return execSync(`gcloud auth print-identity-token`, { encoding: "utf-8" }).trim();
  }
}
