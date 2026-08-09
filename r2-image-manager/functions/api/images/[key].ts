import { json, type ImageManagerEnv } from "../../_shared";

function keyFrom(params: EventContext<ImageManagerEnv, string, Record<string, unknown>>["params"]): string {
  const value = params.key;
  return Array.isArray(value) ? value.join("/") : value;
}

function isManagedImageKey(key: string): boolean {
  return /^image-[0-9a-f-]{36}\.(?:jpg|png|webp|gif|avif)$/.test(key);
}

export const onRequestGet: PagesFunction<ImageManagerEnv> = async ({ request, env, params }) => {
  const key = keyFrom(params);
  if (!isManagedImageKey(key)) return json({ error: "图片不存在" }, { status: 404 });
  const object = await env.IMAGES.get(key, { onlyIf: request.headers });
  if (!object) return json({ error: "图片不存在" }, { status: 404 });

  const headers = new Headers();
  object.writeHttpMetadata(headers);
  headers.set("ETag", object.httpEtag);
  headers.set("X-Content-Type-Options", "nosniff");
  headers.set("Cache-Control", "private, max-age=3600");

  if (!("body" in object)) return new Response(null, { status: 304, headers });
  return new Response(object.body, { headers });
};

export const onRequestDelete: PagesFunction<ImageManagerEnv> = async ({ env, params }) => {
  const key = keyFrom(params);
  if (!isManagedImageKey(key)) return json({ error: "图片不存在" }, { status: 404 });
  if (!(await env.IMAGES.head(key))) {
    return json({ error: "图片不存在" }, { status: 404 });
  }

  await env.IMAGES.delete(key);
  console.log(JSON.stringify({ event: "image.deleted", key }));
  return json({ ok: true });
};
