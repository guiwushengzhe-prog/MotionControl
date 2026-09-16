/**
 * Who is signed in, as one shared reactive value.
 *
 * The browser holds the session in an HttpOnly cookie, which this code cannot
 * read, so "am I signed in?" is only answerable by asking the server. That
 * question is asked once at start-up and the answer is cached here; every
 * component reads the same ref rather than each making its own request.
 *
 * `ready` exists so the router does not redirect to the login page during the
 * moment before the first answer comes back -- without it, a signed-in user
 * reloading a page would be bounced to the login form and then back.
 */

import { ref } from "vue";
import { api, type User } from "./api";

export const user = ref<User | null>(null);
export const ready = ref(false);

export async function refresh(): Promise<void> {
  try {
    user.value = await api.me();
  } catch {
    // 401 is the normal answer for a visitor, not an error worth surfacing.
    user.value = null;
  } finally {
    ready.value = true;
  }
}

export async function signOut(): Promise<void> {
  await api.logout();
  user.value = null;
}
