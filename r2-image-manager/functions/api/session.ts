import {
  SESSION_COOKIE,
  json,
  secretsEqual,
  sessionValue,
  type ImageManagerEnv,
} from "../_shared";

export const onRequestPost: PagesFunction<ImageManagerEnv> = async ({ request, env }) => {
  let body: { token?: unknown };
  try {
    body = await request.json<{ token?: unknown }>();
  } catch {
    return json({ error: "请求格式不正确" }, { status: 400 });
  }

  if (typeof body.token !== "string" || !(await secretsEqual(body.token, env.ADMIN_TOKEN))) {
    return json({ error: "管理令牌不正确" }, { status: 401 });
  }

  const cookie = [
    `${SESSION_COOKIE}=${await sessionValue(env.ADMIN_TOKEN)}`,
    "Path=/",
    "HttpOnly",
    "Secure",
    "SameSite=Strict",
    "Max-Age=43200",
  ].join("; ");

  return json(
    { ok: true },
    { headers: { "Set-Cookie": cookie } },
  );
};

export const onRequestDelete: PagesFunction<ImageManagerEnv> = async () =>
  json(
    { ok: true },
    {
      headers: {
        "Set-Cookie": `${SESSION_COOKIE}=; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age=0`,
      },
    },
  );
