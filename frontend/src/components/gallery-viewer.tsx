"use client";

import { useState } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";

interface GalleryViewerProps {
  bucket: string;
  objectPath: string;
}

export function GalleryViewer({ bucket, objectPath }: GalleryViewerProps) {
  const [expanded, setExpanded] = useState(true);
  const src = `/api/gcs?bucket=${encodeURIComponent(bucket)}&path=${encodeURIComponent(objectPath)}`;

  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-base flex items-center justify-between">
          Creative Portfolio Gallery
          <div className="flex gap-2">
            <Button
              size="sm"
              variant="outline"
              onClick={() => setExpanded(!expanded)}
            >
              {expanded ? "Collapse" : "Expand"}
            </Button>
            <Button
              size="sm"
              variant="outline"
              className="border-red-500 text-red-500 hover:bg-red-50 hover:text-red-600"
              onClick={() => window.open(src, "_blank")}
            >
              Open in new tab
            </Button>
          </div>
        </CardTitle>
      </CardHeader>
      <CardContent className="p-0 overflow-hidden rounded-b-lg">
        <iframe
          src={src}
          title="Creative Portfolio Gallery"
          className={`w-full border-0 transition-all duration-300 ${expanded ? "h-[900px]" : "h-[500px]"}`}
          sandbox="allow-scripts allow-same-origin"
        />
      </CardContent>
    </Card>
  );
}
