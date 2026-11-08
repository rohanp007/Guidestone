import { NextResponse, type NextRequest } from "next/server";

/** Signed-out visitors never see the page shell: no session cookie means /login. (The cookie is only checked for presence here;
 *  the API verifies it, and a stale one gets a 401 that api.ts turns into the same redirect.) */
export function proxy(req: NextRequest) {
  const signedIn = req.cookies.has("gs_session");
  if (req.nextUrl.pathname === "/login") {      // already signed in: straight to the page, unless the API just said the session is stale
    return signedIn && !req.nextUrl.searchParams.has("expired") ? NextResponse.redirect(new URL("/accounts", req.url)) : NextResponse.next();
  }
  if (!signedIn) return NextResponse.redirect(new URL("/login", req.url));
  return NextResponse.next();
}

export const config = { matcher: ["/login", "/", "/accounts", "/debug/:path*"] };
