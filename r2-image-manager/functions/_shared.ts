const encoder = new TextEncoder();

// Pages secrets are configured separately from wrangler.jsonc.
export type ImageManagerEnv = Env & { ADMIN_TOKEN: string };

export const SESSION_COOKIE = "__Host-r2_admin";

export function json(data: unknown, init: ResponseInit = {}): Response {
  const headers = new Headers(init.headers);
  headers.set("Content-Type", "application/json; charset=utf-8");
  headers.set("Cache-Control", "no-store");
  return Response.json(data, { ...init, headers });
}

async function tokenDigest(token: string): Promise<ArrayBuffer> {
  return crypto.subtle.digest("SHA-256", encoder.encode(token));
}

function toBase64Url(value: ArrayBuffer): string {
  const bytes = new Uint8Array(value);
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replaceAll("+", "-").replaceAll("/", "_").replace(/=+$/, "");
}

export async function sessionValue(adminToken: string): Promise<string> {
  return toBase64Url(await tokenDigest(adminToken));
}

function readCookie(request: Request, name: string): string | null {
  const cookieHeader = request.headers.get("Cookie");
  if (!cookieHeader) return null;

  for (const item of cookieHeader.split(";")) {
    const [key, ...value] = item.trim().split("=");
    if (key === name) return value.join("=");
  }
  return null;
}

export async function secretsEqual(provided: string, expected: string): Promise<boolean> {
  const [providedHash, expectedHash] = await Promise.all([
    tokenDigest(provided),
    tokenDigest(expected),
  ]);
  return crypto.subtle.timingSafeEqual(providedHash, expectedHash);
}

export async function hasValidSession(request: Request, env: ImageManagerEnv): Promise<boolean> {
  const cookie = readCookie(request, SESSION_COOKIE);
  if (!cookie) return false;
  return secretsEqual(cookie, await sessionValue(env.ADMIN_TOKEN));
}

export function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "Unexpected error";
}
