/**
 * Where a creative run wrote its outputs, derived from session state: the bucket
 * (`gcs_bucket_name`, else `gcs_bucket` without its `gs://`) and the run's
 * `<gcs_folder>/<agent_output_dir>` prefix. One place for the derivation the
 * results page needs for its eval report, gallery, images and artifacts.
 */

type State = Record<string, unknown>;

const text = (v: unknown): string => (typeof v === "string" ? v : "");

/** The run's output bucket name, or "" when state names none. */
export function bucketFromState(state: State): string {
  return (
    text(state.gcs_bucket_name) ||
    text(state.gcs_bucket)
      .replace(/^gs:\/\//, "")
      .replace(/\/+$/, "")
  );
}

/** The run's output folder (bucket + object prefix), or null when any part is missing. */
export function outputDir(state: State): { bucket: string; prefix: string } | null {
  const bucket = bucketFromState(state);
  const folder = text(state.gcs_folder);
  const subdir = text(state.agent_output_dir);
  return bucket && folder && subdir ? { bucket, prefix: `${folder}/${subdir}` } : null;
}
