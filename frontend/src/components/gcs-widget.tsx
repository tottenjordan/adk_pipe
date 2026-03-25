"use client";

export function GcsWidget({ uri }: { uri: string }) {
  if (!uri) return null;

  // Convert gs:// URI to console URL
  const consolePath = uri.replace("gs://", "").split("/");
  const bucket = consolePath[0];
  const objectPath = consolePath.slice(1).join("/");
  const consoleUrl = `https://console.cloud.google.com/storage/browser/${bucket}/${objectPath}`;

  return (
    <div className="glass rounded-xl px-4 py-3 flex items-start gap-3 animate-fadeInUpSmooth">
      <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-primary/10 text-primary mt-0.5">
        <svg
          xmlns="http://www.w3.org/2000/svg"
          width="16"
          height="16"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
        >
          <path d="M4 20h16a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.93a2 2 0 0 1-1.66-.9l-.82-1.2A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13c0 1.1.9 2 2 2Z" />
        </svg>
      </div>
      <div className="min-w-0 flex-1">
        <p className="text-[10px] font-bold uppercase tracking-wider text-emerald-600 mb-1">
          Cloud Storage Output
        </p>
        <a
          href={consoleUrl}
          target="_blank"
          rel="noopener noreferrer"
          className="text-xs font-mono text-primary hover:text-primary/80 hover:underline break-all transition-colors"
        >
          {uri}
        </a>
      </div>
    </div>
  );
}
