// Shared catalog state + label registry. ES module live-bindings mean imports
// stay in sync after setAppLabels reassigns the underlying array.

export const DEFAULT_LABEL = "nucleus";

export const state = {
  catalog: [],
  models: [],
  config: null,
  filter: "all",
  idx: 0,
  user: "",
};

export let appLabels = [];
export function setAppLabels(arr) { appLabels = arr; }

export function filtered() {
  if (state.filter === "all") {
    return state.catalog.filter((it) => !(it.state && it.state.status === "excluded"));
  }
  return state.catalog.filter((it) => {
    const st = it.state && it.state.status;
    if (state.filter === "unreviewed") {
      return !st || st === "" || (st !== "selected" && st !== "skipped" && st !== "excluded");
    }
    return st === state.filter;
  });
}

export function currentItem() {
  const list = filtered();
  if (!list.length) return null;
  if (state.idx >= list.length) state.idx = list.length - 1;
  if (state.idx < 0) state.idx = 0;
  return list[state.idx];
}

export function getLabelByName(name) {
  return appLabels.find((l) => l.name === name)
    || appLabels[0]
    || { name: "nucleus", color: "#4488ff", shortcut: "1" };
}

export function getLabelColor(name) {
  return getLabelByName(name).color || "#4488ff";
}
