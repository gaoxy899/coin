import { hasValidSession, json, type ImageManagerEnv } from "../_shared";

export const onRequest: PagesFunction<ImageManagerEnv> = async (context) => {
  const isSessionRoute = new URL(context.request.url).pathname === "/api/session";

  if (!isSessionRoute && !(await hasValidSession(context.request, context.env))) {
    return json({ error: "请先登录" }, { status: 401 });
  }

  const response = await context.next();
  const secured = new Response(response.body, response);
  secured.headers.set("X-Content-Type-Options", "nosniff");
  secured.headers.set("Referrer-Policy", "no-referrer");
  secured.headers.set("X-Frame-Options", "DENY");
  return secured;
};
