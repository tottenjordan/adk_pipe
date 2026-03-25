"use client";

import { useState } from "react";
import { Button } from "@/components/ui/button";

interface GalleryViewerProps {
  bucket: string;
  objectPath: string;
}

export function GalleryViewer({ bucket, objectPath }: GalleryViewerProps) {
  const [expanded, setExpanded] = useState(false);
  const src = `/api/gcs?bucket=${encodeURIComponent(bucket)}&path=${encodeURIComponent(objectPath)}`;

  return (
    <div className="glass rounded-2xl overflow-hidden">
      <div className="px-5 py-3 flex items-center justify-between border-b border-border">
        <h3 className="text-sm font-semibold text-foreground">
          Creative Portfolio Gallery
        </h3>
        <div className="flex gap-2">
          <Button
            size="sm"
            variant="outline"
            className="text-xs border-border bg-muted/50 hover:bg-muted"
            onClick={() => setExpanded(!expanded)}
          >
            {expanded ? "Collapse" : "Expand"}
          </Button>
          <Button
            size="sm"
            variant="outline"
            className="text-xs border-red-300 text-red-600 hover:bg-red-50 hover:text-red-700"
            onClick={() => window.open(src, "_blank")}
          >
            Open in new tab
          </Button>
        </div>
      </div>
      <iframe
        src={src}
        title="Creative Portfolio Gallery"
        className={`w-full border-0 transition-all duration-300 ${expanded ? "h-[900px]" : "h-[200px]"}`}
        sandbox="allow-scripts allow-same-origin"
      />
    </div>
  );
}
