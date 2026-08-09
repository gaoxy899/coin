import { errorMessage, json, type ImageManagerEnv } from "../../_shared";

const MAX_FILE_SIZE = 10 * 1024 * 1024;
const PAGE_SIZE = 48;
const MANAGED_PREFIX = "image-";
const IMAGE_TYPES = new Map([
  ["image/jpeg", "jpg"],
  ["image/png", "png"],
  ["image/webp", "webp"],
  ["image/gif", "gif"],
  ["image/avif", "avif"],
]);

function safeOriginalName(name: string): string {
  const basename = name.split(/[\\/]/).at(-1)?.trim() || "image";
  return basename.replace(/[\u0000-\u001f\u007f]/g, "").slice(0, 180) || "image";
}

export const onRequestGet: PagesFunction<ImageManagerEnv> = async ({ request, env }) => {
  const cursor = new URL(request.url).searchParams.get("cursor") || undefined;
  const result = await env.IMAGES.list({
    cursor,
    limit: PAGE_SIZE,
    prefix: MANAGED_PREFIX,
    include: ["httpMetadata", "customMetadata"],
  });

  return json({
    images: result.objects.map((object) => ({
      key: object.key,
      name: object.customMetadata?.originalName || object.key,
      size: object.size,
      uploaded: object.uploaded.toISOString(),
      contentType: object.httpMetadata?.contentType || "application/octet-stream",
      url: `/api/images/${encodeURIComponent(object.key)}`,
    })),
    cursor: result.truncated ? result.cursor : null,
  });
};

export const onRequestPost: PagesFunction<ImageManagerEnv> = async ({ request, env }) => {
  const contentType = request.headers.get("Content-Type")?.split(";", 1)[0].toLowerCase();
  const extension = contentType ? IMAGE_TYPES.get(contentType) : undefined;
  if (!contentType || !extension) {
    return json({ error: "仅支持 JPEG、PNG、WebP、GIF 和 AVIF 图片" }, { status: 415 });
  }
  if (!request.body) {
    return json({ error: "没有收到图片内容" }, { status: 400 });
  }

  const declaredSize = Number(request.headers.get("X-File-Size") || request.headers.get("Content-Length"));
  if (!Number.isFinite(declaredSize) || declaredSize <= 0) {
    return json({ error: "缺少有效的图片大小" }, { status: 411 });
  }
  if (declaredSize > MAX_FILE_SIZE) {
    return json({ error: "图片不能超过 10 MiB" }, { status: 413 });
  }
  const contentLength = request.headers.get("Content-Length");
  if (contentLength && Number(contentLength) !== declaredSize) {
    return json({ error: "图片大小与请求内容不一致" }, { status: 400 });
  }

  const originalName = safeOriginalName(new URL(request.url).searchParams.get("filename") || "image");
  const key = `${MANAGED_PREFIX}${crypto.randomUUID()}.${extension}`;

  try {
    const stream = new FixedLengthStream(declaredSize);
    const [object] = await Promise.all([
      env.IMAGES.put(key, stream.readable, {
        httpMetadata: {
          contentType,
          cacheControl: "private, max-age=3600",
          contentDisposition: `inline; filename="${key}"`,
        },
        customMetadata: { originalName },
      }),
      request.body.pipeTo(stream.writable),
    ]);

    if (!object) return json({ error: "图片写入失败" }, { status: 500 });
    console.log(JSON.stringify({ event: "image.uploaded", key, size: object.size }));
    return json(
      {
        image: {
          key,
          name: originalName,
          size: object.size,
          uploaded: object.uploaded.toISOString(),
          contentType,
          url: `/api/images/${encodeURIComponent(key)}`,
        },
      },
      { status: 201 },
    );
  } catch (error) {
    console.error(JSON.stringify({ event: "image.upload_failed", error: errorMessage(error) }));
    return json({ error: errorMessage(error) }, { status: 400 });
  }
};
