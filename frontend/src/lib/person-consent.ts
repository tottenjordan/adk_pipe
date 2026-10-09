/**
 * The consent wording a user agrees to when registering a person's photo.
 *
 * Mirrored verbatim in runserver/person_refs.py (`CONSENT_TEXT`,
 * `CONSENT_TEXT_VERSION`; drift-tested by tests/test_person_consent_drift.py). The
 * api refuses a registration whose `consent_text_version` isn't the current one, so
 * change both sides together and bump the version to the change's date.
 */

export const CONSENT_TEXT_VERSION = "2026-10-09";

export const CONSENT_TEXT = "By registering this photo you confirm that the person shown is an adult (18 or over) and has agreed to appear in AI-generated ad previews made with Trend Trawler. The photo is used only as a reference for generating images in your runs and is visible only to you. Images made with it appear in public share links only if the person also agreed to that. You can revoke at any time: revoking deletes the photo and every image made with it.";
