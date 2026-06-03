// Thin fetch wrapper. Throws on non-2xx with status + body for clearer errors.

export async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  if (!res.ok) {
    const txt = await res.text();
    throw new Error(`${res.status}: ${txt || res.statusText}`);
  }
  return res.json();
}
