"use strict";
/*
 * The Pocketful Editor's page.
 *
 * Plain script, no build step, no framework: the page is a handful of views drawn from the
 * editor server's API, and a hash in the address says which one (#/set/ptcg-en-base01).
 * Everything that writes goes through that API, and the database's own explanation of a
 * refusal is shown as it is.
 *
 * Forms keep a copy of the record as it was loaded and compare against it, so Save sends
 * only what changed and leaving a page with unsaved edits asks first.
 */

// ============================================================================ helpers

const $ = (selector, root = document) => root.querySelector(selector);

function h(tag, props, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") el.className = value;
    else if (key === "text") el.textContent = value;
    else if (key === "style") Object.assign(el.style, value);
    else if (key === "dataset") Object.assign(el.dataset, value);
    else if (key.startsWith("on") && typeof value === "function") el.addEventListener(key.slice(2).toLowerCase(), value);
    else if (key === "value") el.value = value;
    else if (["checked", "disabled", "selected", "multiple", "hidden", "open", "required"].includes(key)) el[key] = true;
    else el.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : String(child));
  }
  return el;
}

/** replaceChildren, but lists are unpacked and null/false are skipped, the same as h(). */
function fill(el, ...children) {
  el.replaceChildren(...children.flat(Infinity).filter((c) => c !== null && c !== undefined && c !== false)
    .map((c) => (c instanceof Node ? c : String(c))));
  return el;
}

const clone = (value) => (value === undefined ? undefined : JSON.parse(JSON.stringify(value)));
const canonical = (value) => JSON.stringify(value ?? null, (key, v) =>
  (v && typeof v === "object" && !Array.isArray(v) ? Object.fromEntries(Object.keys(v).sort().map((k) => [k, v[k]])) : v));
const same = (a, b) => canonical(a) === canonical(b);
const plural = (n, word, many) => `${n} ${n === 1 ? word : many || word + "s"}`;
const dateOnly = (iso) => (iso ? String(iso).slice(0, 10) : "");
const natural = (a, b) => String(a).localeCompare(String(b), undefined, { numeric: true });

// ============================================================================ API

async function api(method, path, body) {
  const options = { method, headers: {} };
  if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  let response;
  try {
    response = await fetch(path, options);
  } catch (e) {
    throw new Error("The editor is not running. Close this window and open Pocketful Editor again.");
  }
  const kind = response.headers.get("Content-Type") || "";
  const data = kind.includes("application/json") ? await response.json() : await response.blob();
  if (!response.ok) {
    const error = new Error((data && data.error) || response.statusText);
    error.status = response.status;
    error.data = data;
    throw error;
  }
  return data;
}
const GET = (path) => api("GET", path);
const POST = (path, body = {}) => api("POST", path, body);
const PATCH = (path, body) => api("PATCH", path, body);
const DELETE = (path) => api("DELETE", path);
const enc = encodeURIComponent;

// ============================================================================ state

const S = {
  boot: null,
  catalog: localStorage.getItem("catalog") || "ptcg-en",
  guard: null,        // () => true while the view has unsaved changes
  keys: null,         // the view's keyboard shortcuts
  paste: null,        // () => the picture slot a paste should go to
  token: 0,
};

const catalogRow = () => S.boot.catalogs.find((c) => c.id === S.catalog) || S.boot.catalogs[0];
const language = () => catalogRow().language;
const words = (kind) => S.boot.words.filter((w) => w.kind === kind);
const wordRow = (word) => S.boot.words.find((w) => w.word === word);
const terms = (kind) => S.boot.terms.filter((t) => t.kind === kind);
function termLabel(kind, code) {
  const term = S.boot.terms.find((t) => t.kind === kind && t.code === code);
  if (!term) return code;
  return term.labels[language()] || term.labels.en || code;
}
const summaryOf = (setId) => S.boot.summaries.find((s) => s.set_id === setId);
const imageUrl = (path) => (path ? `${S.boot.project.publicUrl}/${path.split("/").map(enc).join("/")}` : null);

async function loadBoot() {
  S.boot = await GET("/api/bootstrap");
  if (!S.boot.catalogs.some((c) => c.id === S.catalog)) S.catalog = S.boot.catalogs[0].id;
}

const STATUS = { draft: "Draft", published: "Published", published_changed: "Changed since publishing" };
const REVIEW = { unreviewed: "Unreviewed", reviewed: "Reviewed", flagged: "Flagged" };
const SET_KINDS = ["expansion", "special", "promo", "deck", "kit", "other"];
const statusBadge = (status) => h("span", { class: `badge ${status}`, text: STATUS[status] || status });
const reviewBadge = (review) => h("span", { class: `badge ${review}`, text: REVIEW[review] || review });

// ============================================================================ toasts and dialogs

function toast(message, kind = "ok", ms) {
  const el = h("div", { class: `toast ${kind}`, text: message });
  $("#toasts").append(el);
  setTimeout(() => el.remove(), ms || (kind === "bad" ? 9000 : 3500));
}

function fail(error) {
  console.error(error);
  toast(error.message || String(error), "bad");
}

/** A modal question. Resolves to the form's values (or true) on OK, false on cancel. */
function ask({ title, body, ok = "OK", cancel = "Cancel", danger = false, fields = [] }) {
  const dialog = $("#dialog");
  return new Promise((resolve) => {
    const controls = {};
    const form = h("form", { method: "dialog" },
      h("h2", { text: title }),
      body ? (body instanceof Node ? body : h("p", { class: "muted", text: body })) : null,
      fields.map((f) => {
        let control;
        if (f.options) {
          control = h("select", {}, f.options.map((o) => h("option", { value: o.value, text: o.label, selected: o.value === f.value })));
        } else if (f.type === "textarea") {
          control = h("textarea", { value: f.value || "" });
        } else {
          control = h("input", { type: f.type || "text", value: f.value ?? "", placeholder: f.placeholder || "" });
        }
        if (f.list) {
          const id = `list-${f.name}`;
          control.setAttribute("list", id);
          control.after?.call(control);
          controls[`${f.name}__list`] = h("datalist", { id }, f.list.map((o) => h("option", { value: o.value, text: o.label })));
        }
        controls[f.name] = control;
        return h("label", { class: "field" }, h("span", { class: "label", text: f.label }), control,
          controls[`${f.name}__list`] || null, f.hint ? h("span", { class: "hint", text: f.hint }) : null);
      }),
      h("div", { class: "buttons" },
        cancel ? h("button", { type: "button", text: cancel, onclick: () => dialog.close("cancel") }) : null,
        h("button", { type: "submit", class: danger ? "danger" : "primary", text: ok, value: "ok" })),
    );
    fill(dialog, form);
    const done = () => {
      dialog.removeEventListener("close", done);
      if (dialog.returnValue !== "ok") return resolve(false);
      if (!fields.length) return resolve(true);
      const values = {};
      for (const f of fields) values[f.name] = controls[f.name].value;
      resolve(values);
    };
    dialog.addEventListener("close", done);
    dialog.returnValue = "";
    dialog.showModal();
    const first = fields.length ? controls[fields[0].name] : form.querySelector("button[type=submit]");
    setTimeout(() => first.focus(), 30);
  });
}

async function busy(button, work) {
  // Always capture the button before awaiting anything: event.currentTarget is null after an await.
  if (!button) return work().catch(fail);
  const label = button.textContent;
  button.disabled = true;
  button.textContent = "Working…";
  try {
    return await work();
  } catch (e) {
    fail(e);
    return undefined;
  } finally {
    button.disabled = false;
    button.textContent = label;
  }
}

// ============================================================================ forms

/**
 * A record being edited: the copy as loaded, the working copy, and which fields changed.
 */
class Form {
  constructor(record, fields, onDirty) {
    this.original = clone(record);
    this.model = clone(record);
    this.fields = fields;
    this.onDirty = onDirty || (() => {});
    this.controls = {};
  }
  changed(field) { return !same(this.original[field], this.model[field]); }
  dirty() { return this.fields.some((f) => this.changed(f)); }
  patch() {
    const out = {};
    for (const f of this.fields) if (this.changed(f)) out[f] = this.model[f];
    return out;
  }
  set(field, value) {
    this.model[field] = value;
    const wrapper = this.controls[field];
    if (wrapper) wrapper.classList.toggle("changed", this.changed(field));
    this.onDirty();
  }
  saved(record) {
    for (const f of this.fields) if (f in record) this.original[f] = clone(record[f]);
    for (const [k, v] of Object.entries(record)) if (!this.fields.includes(k)) { this.original[k] = clone(v); this.model[k] = clone(v); }
    for (const w of Object.values(this.controls)) w.classList.remove("changed");
    this.onDirty();
  }
  wrap(field, label, control, extra) {
    const wrapper = h("label", { class: `field${extra?.wide ? " wide" : ""}${extra?.check ? " check" : ""}` },
      extra?.check ? [control, h("span", { class: "label", text: label })] : [h("span", { class: "label", text: label }), control],
      extra?.hint ? h("span", { class: "hint", text: extra.hint }) : null,
      extra?.suggest || null);
    if (this.changed(field)) wrapper.classList.add("changed");
    this.controls[field] = wrapper;
    return wrapper;
  }
  text(field, label, extra = {}) {
    const input = h("input", { type: extra.type || "text", value: this.model[field] ?? "", disabled: extra.disabled,
      placeholder: extra.placeholder || "" });
    input.addEventListener("input", () => this.set(field, input.value === "" ? null : input.value));
    return this.wrap(field, label, input, extra);
  }
  number(field, label, extra = {}) {
    const input = h("input", { type: "number", value: this.model[field] ?? "", disabled: extra.disabled, min: extra.min ?? "" });
    input.addEventListener("input", () => this.set(field, input.value === "" ? null : Number(input.value)));
    return this.wrap(field, label, input, extra);
  }
  area(field, label, extra = {}) {
    const input = h("textarea", { value: this.model[field] ?? "", disabled: extra.disabled, rows: extra.rows || 3 });
    input.addEventListener("input", () => this.set(field, input.value === "" ? null : input.value));
    return this.wrap(field, label, input, { wide: true, ...extra });
  }
  select(field, label, options, extra = {}) {
    const select = h("select", { disabled: extra.disabled },
      extra.empty !== undefined ? h("option", { value: "", text: extra.empty }) : null,
      options.map((o) => h("option", { value: o.value, text: o.label, selected: o.value === this.model[field] })));
    if (this.model[field] && !options.some((o) => o.value === this.model[field])) {
      select.append(h("option", { value: this.model[field], text: `${this.model[field]} (not in the list)`, selected: true }));
    }
    select.addEventListener("change", () => this.set(field, select.value === "" ? null : select.value));
    return this.wrap(field, label, select, extra);
  }
  check(field, label, extra = {}) {
    const input = h("input", { type: "checkbox", checked: !!this.model[field], disabled: extra.disabled });
    input.addEventListener("change", () => this.set(field, input.checked));
    return this.wrap(field, label, input, { check: true, ...extra });
  }
  chips(field, label, options, extra = {}) {
    const box = h("div", { class: "chips" });
    const draw = () => {
      const values = this.model[field] || [];
      fill(box, 
        values.map((v, i) => h("span", { class: "chip" }, options.find((o) => o.value === v)?.label || v,
          extra.disabled ? null : h("button", { type: "button", title: "Remove", text: "×", onclick: (e) => {
            e.preventDefault();
            const next = values.slice(); next.splice(i, 1);
            this.set(field, next); draw();
          } }))),
        extra.disabled ? null : h("select", { onchange: (e) => {
          if (!e.target.value) return;
          const next = values.concat(e.target.value);
          this.set(field, extra.repeat ? next : [...new Set(next)]); draw();
        } }, h("option", { value: "", text: "+ add" }),
          options.filter((o) => extra.repeat || !values.includes(o.value)).map((o) => h("option", { value: o.value, text: o.label }))),
      );
    };
    draw();
    return this.wrap(field, label, box, extra);
  }
}

/** A value a source suggests, with a button that copies it into the form. */
function suggestion(form, field, value, sourceName, format, after) {
  if (value === undefined || same(value, form.model[field]) || (value === null && form.model[field] === null)) return null;
  if ((value === null || (Array.isArray(value) && !value.length)) && !form.model[field]) return null;
  return h("span", { class: "suggest" }, `${sourceName}: ${format ? format(value) : (value ?? "(empty)")}`,
    h("button", { type: "button", class: "link", text: "Use", onclick: (e) => {
      e.preventDefault();
      form.set(field, clone(value));
      after();
    } }));
}

function setUnsavedIndicator() {
  $("#unsaved").hidden = !(S.guard && S.guard());
}

// ============================================================================ pictures

const SIZES = {
  front: [734, 1024], back: [734, 1024], logo: [1200, 600], symbol: [256, 256],
};
const THUMB = [245, 342];
const SOFT = [600, 825];

function fit(width, height, [maxW, maxH]) {
  const scale = Math.min(1, maxW / width, maxH / height);
  return [Math.max(1, Math.round(width * scale)), Math.max(1, Math.round(height * scale))];
}

/** Draws a bitmap at a smaller size, halving in steps so a big downscale stays sharp. */
async function encodeWebp(bitmap, width, height, quality = 0.9) {
  let source = bitmap;
  let w = bitmap.width;
  let hgt = bitmap.height;
  while (w / 2 >= width && hgt / 2 >= height) {
    w = Math.round(w / 2); hgt = Math.round(hgt / 2);
    const step = new OffscreenCanvas(w, hgt);
    const ctx = step.getContext("2d");
    ctx.imageSmoothingQuality = "high";
    ctx.drawImage(source, 0, 0, w, hgt);
    source = step;
  }
  const canvas = new OffscreenCanvas(width, height);
  const ctx = canvas.getContext("2d");
  ctx.imageSmoothingQuality = "high";
  ctx.drawImage(source, 0, 0, width, height);
  const blob = await canvas.convertToBlob({ type: "image/webp", quality });
  if (blob.type !== "image/webp") throw new Error("This browser cannot save WebP pictures. Use Microsoft Edge.");
  return blob;
}

const toBase64 = (blob) => new Promise((resolve, reject) => {
  const reader = new FileReader();
  reader.onload = () => resolve(String(reader.result).split(",")[1]);
  reader.onerror = () => reject(reader.error);
  reader.readAsDataURL(blob);
});

async function preparePicture(blob, role, { keepOriginal }) {
  let bitmap;
  try {
    bitmap = await createImageBitmap(blob);
  } catch (e) {
    throw new Error("That is not a picture this browser can open.");
  }
  const [width, height] = fit(bitmap.width, bitmap.height, SIZES[role]);
  const cardShaped = Math.abs(bitmap.width / bitmap.height - 63 / 88) < 0.03;
  const warnings = [];
  if ((role === "front" || role === "back") && !cardShaped) {
    warnings.push(`It is ${bitmap.width}×${bitmap.height}, which is not card-shaped. Crop it to the card's edges first (Win+Shift+S, then paste) unless that is really how it should look.`);
  }
  if ((role === "front" || role === "back") && (width < SOFT[0] || height < SOFT[1])) {
    warnings.push(`It is only ${width}×${height}, so it will look soft next to other cards. It will be marked as below the standard.`);
  }
  const image = await encodeWebp(bitmap, width, height);
  const out = { image: await toBase64(image), width, height, warnings };
  if (role === "front" || role === "back") {
    const [tw, th] = fit(width, height, THUMB);
    out.thumb = await toBase64(await encodeWebp(bitmap, tw, th, 0.85));
  }
  if (keepOriginal && blob.size <= 20 * 1024 * 1024 && ["image/png", "image/jpeg", "image/webp", "image/gif"].includes(blob.type)) {
    out.original = await toBase64(blob);
    out.original_type = blob.type;
  }
  bitmap.close?.();
  return out;
}

/**
 * One picture slot: what is chosen, what else has been stored, and every way to add one.
 * `onChanged(image)` runs after a picture is added or chosen, with the new chosen picture.
 */
function pictureSlot({ kind, id, role, images, suggestions = [], noImage, onChanged, label }) {
  const shape = role === "front" || role === "back" ? "card" : role;
  const slot = h("div", { class: `slot ${shape}`, tabindex: "-1" });
  let current = images.filter((i) => i.role === role);

  const upload = async (blob, source) => {
    frame.append(h("div", { class: "busy", text: "Preparing picture…" }));
    try {
      const prepared = await preparePicture(blob, role, { keepOriginal: !source?.url });
      if (prepared.warnings.length) {
        const go = await ask({ title: "Use this picture?", body: prepared.warnings.join("\n\n"), ok: "Use it anyway" });
        if (!go) return;
      }
      const row = await POST("/api/images", {
        subject_kind: kind, subject_id: id, role, image: prepared.image, thumb: prepared.thumb,
        original: prepared.original, original_type: prepared.original_type,
        source_id: source?.source_id || "upload", source_url: source?.url || null,
      });
      current = current.map((i) => ({ ...i, chosen: false })).filter((i) => i.id !== row.id).concat(row);
      toast("Picture saved.");
      draw();
      onChanged?.(row);
    } catch (e) {
      fail(e);
    } finally {
      frame.querySelector(".busy")?.remove();
    }
  };

  const fromUrl = async (url, source_id = "upload") => {
    if (!url) return;
    try {
      frame.append(h("div", { class: "busy", text: "Getting picture…" }));
      const blob = await POST("/api/fetch-image", { url });
      frame.querySelector(".busy")?.remove();
      await upload(blob, { url, source_id });
    } catch (e) {
      frame.querySelector(".busy")?.remove();
      fail(e);
    }
  };

  const frame = h("div", { class: "frame" });
  const meta = h("div", { class: "meta" });
  const others = h("div", { class: "others" });
  const fileInput = h("input", { type: "file", accept: "image/*", hidden: true, onchange: () => {
    if (fileInput.files[0]) upload(fileInput.files[0]);
    fileInput.value = "";
  } });
  const urlInput = h("input", { type: "url", placeholder: "…or a picture's web address" });

  function draw() {
    const chosen = current.find((i) => i.chosen);
    fill(frame, chosen
      ? h("img", { src: imageUrl(chosen.path), alt: "" })
      : h("div", { class: "none" }, noImage?.value() ? "Marked as having no picture. The app shows the card back." : `No ${label || role} yet. Drop, paste or choose one.`));
    fill(meta, chosen ? [
      h("span", { text: `${chosen.width}×${chosen.height}` }),
      chosen.below_standard ? h("span", { class: "badge warn", text: "below standard" }) : null,
      chosen.source_id ? h("span", { text: `from ${chosen.source_id}` }) : null,
    ] : []);
    const rest = current.filter((i) => !i.chosen && i.path);
    fill(others, rest.length ? rest.map((i) => h("figure", {},
      h("img", { src: imageUrl(i.thumb_path || i.path), title: "Use this picture instead", onclick: async () => {
        try {
          const row = await PATCH(`/api/images/${enc(i.id)}`, { chosen: true });
          current = current.map((x) => ({ ...x, chosen: x.id === row.id }));
          draw();
          onChanged?.(row);
        } catch (e) { fail(e); }
      } }),
      h("button", { type: "button", class: "link small", text: "remove", onclick: async () => {
        if (!(await ask({ title: "Remove this picture?", body: "It stops being a choice for this slot. The file itself stays in storage.", ok: "Remove", danger: true }))) return;
        try {
          await DELETE(`/api/images/${enc(i.id)}`);
          current = current.filter((x) => x.id !== i.id);
          draw();
        } catch (e) { fail(e); }
      } }))) : []);
  }

  frame.addEventListener("dragover", (e) => { e.preventDefault(); frame.classList.add("hover"); });
  frame.addEventListener("dragleave", () => frame.classList.remove("hover"));
  frame.addEventListener("drop", (e) => {
    e.preventDefault();
    frame.classList.remove("hover");
    const file = [...(e.dataTransfer.files || [])].find((f) => f.type.startsWith("image/"));
    if (file) return upload(file);
    const url = e.dataTransfer.getData("text/uri-list") || e.dataTransfer.getData("text/plain");
    if (url) fromUrl(url.trim());
  });
  slot.addEventListener("mouseenter", () => { S.pasteSlot = slot; });
  slot.pasteBlob = (blob) => upload(blob);
  slot.pasteUrl = (url) => fromUrl(url);

  draw();
  slot.append(
    frame, meta,
    h("div", { class: "row" },
      h("button", { type: "button", class: "small", text: "Choose file…", onclick: () => fileInput.click() }),
      suggestions.filter((s) => s.url).map((s) => h("button", { type: "button", class: "small", text: s.label,
        onclick: (e) => busy(e.currentTarget, () => fromUrl(s.url, s.source_id)) })),
      fileInput),
    h("div", { class: "urlrow" }, urlInput, h("button", { type: "button", class: "small", text: "Get",
      onclick: (e) => busy(e.currentTarget, () => fromUrl(urlInput.value.trim())) })),
    others,
  );
  slot.refresh = (list) => { current = list.filter((i) => i.role === role); draw(); };
  return slot;
}

document.addEventListener("paste", (e) => {
  const target = e.target;
  const typing = target && (target.tagName === "INPUT" || target.tagName === "TEXTAREA");
  const slot = (S.pasteSlot && document.body.contains(S.pasteSlot) ? S.pasteSlot : null) || (S.paste && S.paste());
  if (!slot) return;
  const file = [...(e.clipboardData?.files || [])].find((f) => f.type.startsWith("image/"));
  if (file) {
    e.preventDefault();
    slot.pasteBlob(file);
    return;
  }
  const text = e.clipboardData?.getData("text/plain")?.trim();
  if (!typing && text && /^https?:\/\/\S+$/i.test(text)) {
    e.preventDefault();
    slot.pasteUrl(text);
  }
});

// ============================================================================ sidebar

function renderSide() {
  const side = $("#side");
  const route = location.hash;
  const link = (href, content, cls = "") => h("a", { href, class: `item ${cls}${route === href ? " active" : ""}` }, content);
  const series = S.boot.series.filter((s) => s.catalog_id === S.catalog);

  fill(side, 
    h("div", { class: "group", text: "Catalog" }),
    link("#/", "Overview"),
    link("#/review", "Needs review"),
    link("#/no-picture", "Published without a picture"),
    link("#/words", "Words & terms"),
    h("div", { class: "group" }, "Series", h("span", { class: "spacer" }),
      h("button", { class: "small", text: "+ New", onclick: newSeries })),
    series.length ? series.map((s) => {
      const sets = S.boot.sets.filter((x) => x.series_id === s.id);
      return [
        link(`#/series/${enc(s.id)}`, [h("span", { text: s.name }), h("span", { class: "count", text: sets.length || "" })], "series"),
        sets.map((x) => {
          const summary = summaryOf(x.id);
          const href = `#/set/${enc(x.id)}`;
          const active = route === href || route.startsWith(href + "/");
          return h("a", { href, class: `item set${active ? " active" : ""}`, title: STATUS[x.status] },
            h("span", { class: `dot ${x.status}` }), h("span", { text: x.name }),
            h("span", { class: "count", text: summary && summary.cards ? `${summary.reviewed}/${summary.cards}` : "" }));
        }),
      ];
    }) : h("div", { class: "empty", text: "No series yet." }),
  );
}

function renderCatalogPicker() {
  const select = $("#catalog");
  fill(select, ...S.boot.catalogs.map((c) => h("option", { value: c.id, text: `${c.name}${c.native_name && c.native_name !== c.name ? ` · ${c.native_name}` : ""}`, selected: c.id === S.catalog })));
  select.onchange = () => {
    if (S.guard && S.guard() && !confirm("Leave without saving your changes?")) {
      select.value = S.catalog;
      return;
    }
    S.guard = null;
    S.catalog = select.value;
    localStorage.setItem("catalog", S.catalog);
    location.hash = "#/";
    render();
  };
  const p = S.boot.project;
  $("#project").textContent = `database ${p.database} · files on ${p.storage === "r2" ? "Cloudflare R2" : "a local folder"}`;
}

// ============================================================================ routing

let currentHash = location.hash;
let skipHash = false;

window.addEventListener("hashchange", async () => {
  if (skipHash) { skipHash = false; return; }
  if (S.guard && S.guard()) {
    const target = location.hash;
    skipHash = true;
    location.hash = currentHash;
    const leave = await ask({ title: "Leave without saving?", body: "Your changes on this page have not been saved.", ok: "Leave", danger: true });
    if (!leave) return;
    S.guard = null;
    location.hash = target;
    return;
  }
  currentHash = location.hash;
  render();
});

window.addEventListener("beforeunload", (e) => {
  if (S.guard && S.guard()) { e.preventDefault(); e.returnValue = ""; }
});

document.addEventListener("keydown", (e) => {
  if (S.keys) S.keys(e);
});

async function render() {
  const token = ++S.token;
  const parts = location.hash.replace(/^#\/?/, "").split("/").filter(Boolean).map(decodeURIComponent);
  const view = $("#view");
  S.guard = null; S.keys = null; S.paste = null; S.pasteSlot = null;
  setUnsavedIndicator();
  renderSide();
  let node;
  try {
    const [page, id, tab] = parts;
    if (!page) node = await viewOverview();
    else if (page === "series") node = await viewSeries(id);
    else if (page === "set") node = await viewSet(id, tab || "cards");
    else if (page === "card") node = await viewCard(id);
    else if (page === "words") node = await viewWords();
    else if (page === "review") node = await viewReviewList();
    else if (page === "no-picture") node = await viewNoPictureList();
    else node = h("p", { text: "Nothing here." });
  } catch (e) {
    console.error(e);
    node = h("div", { class: "box bad" }, h("h3", { text: "This page could not be shown" }), h("p", { text: e.message }));
  }
  if (token !== S.token) return;
  fill(view, node);
  view.scrollTop = 0;
  S.guard = node._guard || null;
  S.keys = node._keys || null;
  S.paste = node._paste || null;
  setUnsavedIndicator();
  renderSide();
}

async function refreshBoot() {
  await loadBoot();
  renderSide();
}

// ============================================================================ overview

async function viewOverview() {
  const catalog = catalogRow();
  const detail = await GET(`/api/catalogs/${enc(catalog.id)}`);
  const series = S.boot.series.filter((s) => s.catalog_id === catalog.id);
  const sets = S.boot.sets.filter((x) => series.some((s) => s.id === x.series_id));
  const summaries = sets.map((x) => summaryOf(x.id)).filter(Boolean);
  const total = (key) => summaries.reduce((n, s) => n + Number(s[key] || 0), 0);

  return h("div", {},
    h("div", { class: "title" }, h("h2", { text: catalog.name }), catalog.native_name !== catalog.name ? h("span", { class: "muted", text: catalog.native_name }) : null,
      h("span", { class: "id", text: catalog.id })),
    h("div", { class: "cardview", style: { gridTemplateColumns: "220px 1fr" } },
      h("div", { class: "box" }, h("h3", { text: "Card back" }),
        h("p", { class: "dim", text: "Shown in the app for any card published without a picture." }),
        pictureSlot({ kind: "catalog", id: catalog.id, role: "back", images: detail.images, label: "card back" })),
      h("div", {},
        h("div", { class: "box" }, h("h3", { text: "Progress" }),
          h("div", { class: "fields" },
            stat("Series", series.length), stat("Sets", sets.length),
            stat("Published sets", sets.filter((x) => x.status !== "draft").length),
            stat("Cards", total("cards")), stat("Reviewed", total("reviewed")),
            stat("Flagged", total("flagged")), stat("With a picture", total("with_picture")))),
        series.length ? h("div", { class: "box" }, h("h3", {}, "Series", h("span", { class: "spacer" }),
          h("button", { class: "small", text: "+ New series", onclick: newSeries })),
          h("table", { class: "list" }, h("thead", {}, h("tr", {}, h("th", { text: "Series" }), h("th", { text: "Code" }),
            h("th", { class: "num", text: "Sets" }), h("th", { text: "Status" }))),
            h("tbody", {}, series.map((s) => h("tr", { class: "click", onclick: () => { location.hash = `#/series/${enc(s.id)}`; } },
              h("td", { text: s.name }), h("td", { class: "mono", text: s.code }),
              h("td", { class: "num", text: S.boot.sets.filter((x) => x.series_id === s.id).length }),
              h("td", {}, statusBadge(s.status)))))))
          : h("div", { class: "empty-state" }, h("h2", { text: "Start with a series" }),
            h("p", { text: "A series groups sets, like Base, Neo or Scarlet & Violet. Create one, then add its sets." }),
            h("button", { class: "primary", text: "+ New series", onclick: newSeries })),
      )));
}

function stat(label, value) {
  return h("div", { class: "field" }, h("span", { class: "label", text: label }), h("span", { class: "num", style: { fontSize: "20px" }, text: value }));
}

async function newSeries() {
  const catalog = catalogRow();
  const values = await ask({
    title: `New ${catalog.name} series`,
    body: "The code is part of the series' ID and cannot change once any of its sets is published.",
    ok: "Create series",
    fields: [
      { name: "name", label: "Name", placeholder: "Base" },
      { name: "code", label: "Code", placeholder: "base", hint: "Lowercase letters and digits, like base, neo, sv, me." },
      ...(catalog.language !== "en" ? [{ name: "name_en", label: "English name", hint: "So it can be found in English." }] : []),
      { name: "sort", label: "Order in the catalog", type: "number", value: S.boot.series.filter((s) => s.catalog_id === catalog.id).length + 1 },
    ],
  });
  if (!values) return;
  try {
    const row = await POST("/api/series", { catalog_id: catalog.id, ...values, code: values.code.trim().toLowerCase() });
    await refreshBoot();
    toast(`Created ${row.name}.`);
    location.hash = `#/series/${enc(row.id)}`;
  } catch (e) { fail(e); }
}

// ============================================================================ series

async function viewSeries(id) {
  const data = await GET(`/api/series/${enc(id)}`);
  const series = data.series;
  const catalog = S.boot.catalogs.find((c) => c.id === series.catalog_id);
  const node = h("div", {});
  const form = new Form(series, ["code", "name", "name_en", "sort", "notes"], setUnsavedIndicator);

  const save = h("button", { class: "primary", text: "Save", onclick: (e) => busy(e.currentTarget, async () => {
    const patch = form.patch();
    if (!Object.keys(patch).length) return;
    const row = await PATCH(`/api/series/${enc(series.id)}`, patch);
    form.saved(row);
    await refreshBoot();
    toast("Series saved.");
    if (row.id !== series.id) location.hash = `#/series/${enc(row.id)}`;
  }) });

  node.append(
    h("div", { class: "crumbs" }, h("a", { href: "#/", text: catalog.name })),
    h("div", { class: "title" }, h("h2", { text: series.name }), h("span", { class: "id mono", text: series.id }), statusBadge(series.status),
      series.locked ? h("span", { class: "badge locked", text: "code locked" }) : null),
    h("div", { class: "box" }, h("h3", { text: "Series" }),
      h("div", { class: "fields" },
        form.text("name", "Name"),
        form.text("code", "Code", { disabled: series.locked, hint: series.locked ? "Locked: a set in this series is published." : "Part of the ID." }),
        catalog.language !== "en" ? form.text("name_en", "English name") : null,
        form.number("sort", "Order in the catalog"),
        form.area("notes", "Notes", { rows: 2 }))),
    h("div", { class: "fields", style: { gridTemplateColumns: "1fr 220px", alignItems: "start" } },
      h("div", { class: "box" }, h("h3", { text: "Logo" }), pictureSlot({ kind: "series", id: series.id, role: "logo", images: data.images, label: "logo" })),
      h("div", { class: "box" }, h("h3", { text: "Card back" }),
        h("p", { class: "dim", text: "Only if this series' cards have a different back from the catalog's." }),
        pictureSlot({ kind: "series", id: series.id, role: "back", images: data.images, label: "card back" }))),
    h("div", { class: "box" }, h("h3", {}, "Sets", h("span", { class: "spacer" }),
      h("button", { class: "small", text: "+ New set", onclick: () => newSet(series, data.sets) })),
      data.sets.length ? h("table", { class: "list" },
        h("thead", {}, h("tr", {}, ["Set", "Code", "Kind", "Released"].map((t) => h("th", { text: t })),
          h("th", { class: "num", text: "Cards" }), h("th", { class: "num", text: "Reviewed" }), h("th", { text: "Status" }))),
        h("tbody", {}, data.sets.map((x) => {
          const summary = data.summaries.find((s) => s.set_id === x.id) || {};
          return h("tr", { class: "click", onclick: () => { location.hash = `#/set/${enc(x.id)}`; } },
            h("td", { text: x.name }), h("td", { class: "mono", text: x.code }), h("td", { text: x.kind }),
            h("td", { text: x.release_date || "" }), h("td", { class: "num", text: summary.cards ?? 0 }),
            h("td", { class: "num", text: summary.reviewed ?? 0 }), h("td", {}, statusBadge(x.status)));
        })))
        : h("p", { class: "dim", text: "No sets yet." })),
    h("div", { class: "savebar" }, save,
      !series.locked && !data.sets.length ? h("button", { class: "danger", text: "Delete series", onclick: async (e) => {
        const button = e.currentTarget;
        if (!(await ask({ title: `Delete ${series.name}?`, body: "It has no sets, so nothing else goes with it.", ok: "Delete", danger: true }))) return;
        await busy(button, async () => {
          await DELETE(`/api/series/${enc(series.id)}`);
          S.guard = null;
          await refreshBoot();
          location.hash = "#/";
        });
      } }) : null),
  );
  node._guard = () => form.dirty();
  node._keys = (e) => { if (e.ctrlKey && e.key.toLowerCase() === "s") { e.preventDefault(); save.click(); } };
  return node;
}

async function newSet(series, existing) {
  const next = String(existing.length + 1).padStart(2, "0");
  const values = await ask({
    title: `New set in ${series.name}`,
    body: "The code follows the series: its code plus the set's place in it, two digits, in release order.",
    ok: "Create set",
    fields: [
      { name: "name", label: "Name", placeholder: "Base Set" },
      { name: "code", label: "Code", value: `${series.code}${next}`, hint: "Half sets take .5, like me02.5. A series' promos: the series code plus p." },
      { name: "kind", label: "Kind", options: SET_KINDS.map((k) => ({ value: k, label: k })), value: "expansion" },
      { name: "release_date", label: "Release date", type: "date" },
      { name: "printed_total", label: "Printed total", type: "number", hint: "The number after the slash on the main set's cards." },
      { name: "sort", label: "Order in the series", type: "number", value: existing.length + 1 },
    ],
  });
  if (!values) return;
  try {
    const row = await POST("/api/sets", { series_id: series.id, ...values, code: values.code.trim().toLowerCase() });
    await refreshBoot();
    toast(`Created ${row.name}.`);
    location.hash = `#/set/${enc(row.id)}/details`;
  } catch (e) { fail(e); }
}

// ============================================================================ set

async function viewSet(id, tab) {
  const data = await GET(`/api/sets/${enc(id)}`);
  const theSet = data.set;
  const summary = data.summary || {};
  const logo = data.images.find((i) => i.set_id === theSet.id && i.role === "logo" && i.chosen);

  const tabs = [["cards", `Cards (${data.cards.length})`], ["details", "Details"], ["import", "Import"], ["publish", "Publish"]];
  const head = h("div", {},
    h("div", { class: "crumbs" }, h("a", { href: "#/", text: data.catalog.name }), " › ",
      h("a", { href: `#/series/${enc(data.series.id)}`, text: data.series.name })),
    h("div", { class: "title" },
      logo ? h("img", { class: "logo", src: imageUrl(logo.path), alt: "" }) : null,
      h("h2", { text: theSet.name }), h("span", { class: "id mono", text: theSet.id }), statusBadge(theSet.status),
      theSet.version ? h("span", { class: "badge info", text: `version ${theSet.version}` }) : null,
      h("span", { class: "spacer" }),
      h("span", { class: "muted num", text: `${summary.reviewed ?? 0} of ${summary.cards ?? 0} reviewed · ${summary.with_picture ?? 0} with a picture` })),
    h("nav", { class: "tabs" }, tabs.map(([key, label]) => h("a", { href: `#/set/${enc(id)}${key === "cards" ? "" : "/" + key}`, class: key === tab ? "active" : "", text: label }))),
  );

  let body;
  if (tab === "details") body = setDetails(data);
  else if (tab === "import") body = await setImport(data);
  else if (tab === "publish") body = await setPublish(data);
  else body = setCards(data);
  const node = h("div", {}, head, body);
  node._guard = body._guard; node._keys = body._keys; node._paste = body._paste;
  return node;
}

function setCards(data) {
  const theSet = data.set;
  const printingsByCard = {};
  for (const p of data.printings) (printingsByCard[p.card_id] ||= []).push(p);
  const thumbs = {};
  for (const i of data.images) if (i.card_id && i.role === "front" && i.chosen) thumbs[i.card_id] = i;

  const filters = [
    ["all", "All", () => true],
    ["unreviewed", "Unreviewed", (c) => c.review === "unreviewed"],
    ["flagged", "Flagged", (c) => c.review === "flagged"],
    ["nopicture", "No picture", (c) => !thumbs[c.id]],
    ["withdrawn", "Withdrawn", (c) => c.withdrawn],
  ];
  let active = sessionStorage.getItem("cardFilter") || "all";
  let search = "";
  const selected = new Set();
  const tbody = h("tbody");
  const bar = h("div", { class: "filters" });
  const bulk = h("div", { class: "row", style: { marginBottom: "10px" } });
  const all = h("input", { type: "checkbox", title: "Select every card shown" });

  const shownCards = () => {
    const test = (filters.find((f) => f[0] === active) || filters[0])[2];
    const needle = search.trim().toLowerCase();
    return data.cards.filter((c) => test(c) && (!needle || c.name.toLowerCase().includes(needle) || c.number.includes(needle)));
  };

  // Runs one task per card, a few at a time, with a running count on the bar.
  async function forEachSelected(label, task, button) {
    const cards = data.cards.filter((c) => selected.has(c.id));
    if (!cards.length) return;
    button.disabled = true;
    let done = 0, failed = 0;
    const queue = cards.slice();
    const status = h("span", { class: "muted" });
    bulk.append(status);
    const worker = async () => {
      while (queue.length) {
        const card = queue.shift();
        try { await task(card); } catch (e) { failed++; console.error(card.id, e); }
        done++;
        status.textContent = `${label}: ${done} of ${cards.length}${failed ? `, ${failed} failed` : ""}`;
      }
    };
    await Promise.all([worker(), worker(), worker()]);
    toast(`${label}: ${cards.length - failed} done${failed ? `, ${failed} failed (see the console)` : ""}.`, failed ? "warn" : "ok");
    await refreshBoot();
    render();
  }

  function drawBulk() {
    fill(bulk,
      h("span", { class: "muted", text: selected.size ? `${plural(selected.size, "card")} selected` : "Select cards for bulk actions" }),
      h("button", { class: "small", text: "Use TCGdex pictures", disabled: !selected.size, onclick: async (e) => {
        const button = e.currentTarget;
        const { candidates } = await GET(`/api/sets/${enc(theSet.id)}/candidates`);
        const urls = Object.fromEntries(candidates.filter((c) => c.matched && c.image_url).map((c) => [c.matched, c.image_url]));
        const skipped = [...selected].filter((id) => thumbs[id] || !urls[id]).length;
        if (skipped) toast(`${skipped} selected card(s) already have a picture or have none on TCGdex, and are skipped.`, "warn");
        [...selected].forEach((id) => { if (thumbs[id] || !urls[id]) selected.delete(id); });
        await forEachSelected("Pictures", async (card) => {
          const url = urls[card.id];
          const blob = await POST("/api/fetch-image", { url });
          const prepared = await preparePicture(blob, "front", { keepOriginal: false });
          await POST("/api/images", { subject_kind: "card", subject_id: card.id, role: "front", image: prepared.image,
            thumb: prepared.thumb, source_id: "tcgdex", source_url: url });
        }, button);
      } }),
      h("button", { class: "small", text: "Mark reviewed", disabled: !selected.size, onclick: (e) =>
        forEachSelected("Reviewed", async (card) => {
          if (card.review !== "reviewed") await PATCH(`/api/cards/${enc(card.id)}`, { review: "reviewed" });
          for (const p of printingsByCard[card.id] || []) {
            if (p.review !== "reviewed" && !p.withdrawn) await PATCH(`/api/printings/${enc(p.id)}`, { review: "reviewed" });
          }
        }, e.currentTarget) }),
      h("button", { class: "small", text: "Mark no picture", disabled: !selected.size, onclick: (e) =>
        forEachSelected("No picture", async (card) => {
          if (!thumbs[card.id] && !card.no_image) await PATCH(`/api/cards/${enc(card.id)}`, { no_image: true });
        }, e.currentTarget) }),
      selected.size ? h("button", { class: "small link", text: "Clear selection", onclick: () => { selected.clear(); drawRows(); } }) : null,
    );
  }

  function draw() {
    fill(bar,
      filters.map(([key, label, t]) => h("button", { class: key === active ? "on" : "", text: `${label} ${data.cards.filter(t).length}`,
        onclick: () => { active = key; sessionStorage.setItem("cardFilter", key); draw(); } })),
      h("input", { type: "search", placeholder: "Find by name or number", value: search, oninput: (e) => { search = e.target.value; drawRows(); } }),
    );
    drawRows();
  }

  function drawRows() {
    const shown = shownCards();
    all.checked = shown.length > 0 && shown.every((c) => selected.has(c.id));
    all.onchange = () => { shown.forEach((c) => (all.checked ? selected.add(c.id) : selected.delete(c.id))); drawRows(); };
    fill(tbody, shown.map((c) => {
      const thumb = thumbs[c.id];
      const box = h("input", { type: "checkbox", checked: selected.has(c.id) });
      box.addEventListener("click", (e) => e.stopPropagation());
      box.addEventListener("change", () => { box.checked ? selected.add(c.id) : selected.delete(c.id); drawRows(); });
      return h("tr", { class: `click${c.withdrawn ? " muted" : ""}`, onclick: () => { location.hash = `#/card/${enc(c.id)}`; } },
        h("td", { onclick: (e) => e.stopPropagation() }, box),
        h("td", {}, thumb ? h("img", { class: "thumb", src: imageUrl(thumb.thumb_path || thumb.path), loading: "lazy", alt: "" })
          : h("div", { class: "nothumb", text: c.no_image ? "back" : "no picture" })),
        h("td", { class: "mono", text: c.printed_number || c.number }),
        h("td", {}, h("div", { text: c.name }), c.section ? h("div", { class: "dim", text: c.section }) : null),
        h("td", { text: c.rarity ? termLabel("rarity", c.rarity) : "" }),
        h("td", { class: "dim", text: (printingsByCard[c.id] || []).map((p) => p.variant).join(", ") }),
        h("td", {}, reviewBadge(c.review), c.withdrawn ? h("span", { class: "badge", text: "withdrawn" }) : null,
          c.review === "flagged" && c.review_note ? h("div", { class: "dim", text: c.review_note }) : null));
    }), shown.length ? null : h("tr", {}, h("td", { colspan: 7, class: "dim", text: data.cards.length ? "No cards match." : "No cards yet." })));
    drawBulk();
  }

  const node = h("div", {},
    data.cards.length ? null : h("div", { class: "box notice" }, h("h3", { text: "No cards yet" }),
      h("p", { text: "Add cards one at a time, or import this set from TCGdex and choose which of its cards to accept." }),
      h("div", { class: "row" }, h("a", { class: "btn", href: `#/set/${enc(theSet.id)}/import`, text: "Import from TCGdex…" }))),
    h("div", { class: "row", style: { marginBottom: "10px" } },
      h("button", { class: "primary", text: "+ New card", onclick: () => newCard(data) }),
      data.cards.some((c) => c.review !== "reviewed" && !c.withdrawn)
        ? h("a", { class: "btn", href: `#/card/${enc((data.cards.find((c) => c.review !== "reviewed" && !c.withdrawn)).id)}`, text: "Review the next unreviewed card" }) : null),
    bar,
    bulk,
    h("table", { class: "list" }, h("thead", {}, h("tr", {}, h("th", {}, all), h("th", { text: "" }), h("th", { text: "No." }), h("th", { text: "Name" }),
      h("th", { text: "Rarity" }), h("th", { text: "Printings" }), h("th", { text: "Review" }))), tbody),
  );
  draw();
  return node;
}

async function newCard(data) {
  const values = await ask({
    title: `New card in ${data.set.name}`,
    body: "The number is what is printed before the slash, lowercase, leading zeros kept (062, 4, swsh050, tg01). It is part of the card's ID.",
    ok: "Create card",
    fields: [
      { name: "number", label: "Number", placeholder: "4" },
      { name: "printed_number", label: "Printed number", placeholder: data.set.printed_total ? `4/${data.set.printed_total}` : "4/102" },
      { name: "name", label: "Name", placeholder: "Charizard" },
      { name: "category", label: "Category", options: [{ value: "pokemon", label: "Pokémon" }, { value: "trainer", label: "Trainer" }, { value: "energy", label: "Energy" }], value: "pokemon" },
    ],
  });
  if (!values) return;
  try {
    const row = await POST("/api/cards", { set_id: data.set.id, ...values, number: values.number.trim().toLowerCase() });
    await refreshBoot();
    location.hash = `#/card/${enc(row.id)}`;
  } catch (e) { fail(e); }
}

function setDetails(data) {
  const theSet = data.set;
  const suggestionSource = data.import ? `TCGdex ${data.import.key}` : null;
  const s = data.import?.suggestion || {};
  const form = new Form(theSet, ["series_id", "code", "name", "name_en", "kind", "release_date", "printed_total", "abbreviation",
    "sort", "no_logo", "no_symbol", "notes"], setUnsavedIndicator);
  const holder = h("div");
  const seriesOptions = S.boot.series.filter((x) => x.catalog_id === data.catalog.id).map((x) => ({ value: x.id, label: x.name }));

  const draw = () => {
    const sug = (field, value) => (suggestionSource ? suggestion(form, field, value, suggestionSource, null, draw) : null);
    fill(holder, 
      h("div", { class: "box" }, h("h3", { text: "Set" }),
        h("div", { class: "fields" },
          form.text("name", "Name", { suggest: sug("name", s.name) }),
          form.text("code", "Code", { disabled: theSet.locked, hint: theSet.locked ? "Locked: this set is published." : "Part of every card's ID." }),
          data.catalog.language !== "en" ? form.text("name_en", "English name") : null,
          form.select("series_id", "Series", seriesOptions, { disabled: theSet.locked && false }),
          form.select("kind", "Kind", SET_KINDS.map((k) => ({ value: k, label: k }))),
          form.text("release_date", "Release date", { type: "date", suggest: sug("release_date", s.release_date) }),
          form.number("printed_total", "Printed total", { hint: "The number after the slash.", suggest: sug("printed_total", s.printed_total) }),
          form.text("abbreviation", "Printed set code", { hint: "Like PBL, where the cards print one. Never part of an ID.", suggest: sug("abbreviation", s.abbreviation) }),
          form.number("sort", "Order in the series"),
          form.area("notes", "Notes", { rows: 2 }))),
      h("div", { class: "fields", style: { gridTemplateColumns: "1fr 260px", alignItems: "start" } },
        h("div", { class: "box" }, h("h3", { text: "Logo" }),
          pictureSlot({ kind: "set", id: theSet.id, role: "logo", images: data.images, label: "logo",
            suggestions: [{ label: "Use TCGdex's logo", url: s.logo_url, source_id: "tcgdex" }],
            onChanged: () => { form.set("no_logo", false); form.original.no_logo = false; } }),
          form.check("no_logo", "This set has no logo")),
        h("div", { class: "box" }, h("h3", { text: "Symbol" }),
          pictureSlot({ kind: "set", id: theSet.id, role: "symbol", images: data.images, label: "symbol",
            suggestions: [{ label: "Use TCGdex's symbol", url: s.symbol_url, source_id: "tcgdex" }],
            onChanged: () => { form.set("no_symbol", false); form.original.no_symbol = false; } }),
          form.check("no_symbol", "This set has no symbol"))),
      tcgplayerBox(theSet),
    );
  };
  draw();

  const save = h("button", { class: "primary", text: "Save", onclick: (e) => busy(e.currentTarget, async () => {
    const patch = form.patch();
    if (!Object.keys(patch).length) return;
    const row = await PATCH(`/api/sets/${enc(theSet.id)}`, patch);
    form.saved(row);
    await refreshBoot();
    toast("Set saved.");
    if (row.id !== theSet.id) { S.guard = null; location.hash = `#/set/${enc(row.id)}/details`; }
  }) });

  const node = h("div", {}, holder, h("div", { class: "savebar" }, save,
    h("span", { class: "dim" }, h("kbd", { text: "Ctrl" }), "+", h("kbd", { text: "S" })),
    h("span", { class: "spacer" }),
    !theSet.locked ? h("button", { class: "danger", text: "Delete set", onclick: async (e) => {
        const button = e.currentTarget;
      if (!(await ask({ title: `Delete ${theSet.name}?`, body: `Its ${plural(data.cards.length, "card")}, their printings and pictures go with it. It has never been published, so nothing depends on it.`, ok: "Delete", danger: true }))) return;
      await busy(button, async () => {
        await DELETE(`/api/sets/${enc(theSet.id)}`);
        S.guard = null;
        await refreshBoot();
        location.hash = `#/series/${enc(theSet.series_id)}`;
      });
    } }) : null));
  node._guard = () => form.dirty();
  node._keys = (e) => { if (e.ctrlKey && e.key.toLowerCase() === "s") { e.preventDefault(); save.click(); } };
  return node;
}

/**
 * Linking a set's printings to TCGplayer, which is what prices them.
 * Nothing is fetched until a button here is pressed.
 */
function tcgplayerBox(theSet) {
  const box = h("div", { class: "box" });
  const money = (cents) => (cents == null ? "" : `$${(cents / 100).toFixed(2)}`);
  let state = { group: theSet.tcgplayer_group ? { groupId: theSet.tcgplayer_group, via: theSet.tcgplayer_via } : null };

  const link = async (button, groupId, byHand) => {
    await busy(button, async () => {
      const answer = await POST(`/api/sets/${enc(theSet.id)}/tcgplayer`, { group_id: groupId, by_hand: byHand });
      state = { group: { groupId: answer.group.groupId, name: answer.group.name, via: byHand ? "manual" : "auto" }, result: answer };
      toast(`Linked ${plural(answer.linked, "printing")} of ${answer.printings} to TCGplayer.`);
      draw();
    });
  };

  function draw() {
    const groupId = h("input", { type: "number", placeholder: "Group ID", value: state.group?.groupId ?? "", style: { width: "140px" } });
    fill(box,
      h("h3", {}, "TCGplayer", h("span", { class: "spacer" }),
        state.group ? h("span", { class: "badge info", text: `group ${state.group.groupId}${state.group.name ? ` · ${state.group.name}` : ""}` }) : null),
      h("p", { class: "muted", text: "The nightly price job prices every published printing from the TCGplayer product linked here. Matching links every printing it can; a link you set by hand on a printing is never changed." }),
      h("div", { class: "row" },
        h("button", { text: "Find this set on TCGplayer", onclick: (e) => busy(e.currentTarget, async () => {
          const answer = await GET(`/api/sets/${enc(theSet.id)}/tcgplayer?suggest=1`);
          state = { ...state, suggestions: answer.suggestions };
          if (!answer.suggestions.length) toast("TCGplayer has no group with a name like this set's. Give its group ID instead.", "warn");
          draw();
        }) }),
        h("span", { class: "dim", text: "or" }), groupId,
        h("button", { class: "primary", text: state.group ? "Match printings again" : "Use this group", onclick: (e) => {
          if (!groupId.value) return toast("Give the TCGplayer group ID.", "warn");
          link(e.currentTarget, Number(groupId.value), state.group?.groupId !== Number(groupId.value) || state.group?.via === "manual");
        } })),
      state.suggestions ? h("table", { class: "list", style: { marginTop: "10px" } },
        h("thead", {}, h("tr", {}, ["Group", "Code", "Holds", ""].map((t) => h("th", { text: t })))),
        h("tbody", {}, state.suggestions.map((g) => h("tr", {},
          h("td", {}, h("div", { text: g.name }), h("div", { class: "dim mono", text: `group ${g.groupId}` })),
          h("td", { class: "mono", text: g.abbreviation || "" }),
          h("td", { class: "num", text: `${g.matched} of ${g.cards} cards` }),
          h("td", {}, h("button", { class: "small", text: "Use and match", onclick: (e) => link(e.currentTarget, g.groupId, false) })))))) : null,
      state.result ? h("details", { open: true, style: { marginTop: "10px" } },
        h("summary", { text: `${state.result.linked} of ${state.result.printings} printings linked` }),
        h("table", { class: "list" },
          h("thead", {}, h("tr", {}, ["Printing", "TCGplayer product", "Printing", "Market", ""].map((t) => h("th", { text: t })))),
          h("tbody", {}, state.result.results.map((r) => h("tr", { class: r.status === "no match" ? "muted" : "" },
            h("td", {}, h("a", { class: "mono", href: `#/card/${enc(r.printing.split("_")[0])}`, text: r.printing })),
            h("td", {}, r.productId ? h("a", { href: `https://www.tcgplayer.com/product/${r.productId}`, target: "_blank", text: r.productName || `product ${r.productId}` }) : ""),
            h("td", { text: r.printingName || "" }),
            h("td", { class: "num", text: money(r.market) }),
            h("td", {}, h("span", { class: `badge ${r.status === "linked" ? "reviewed" : r.status === "no match" ? "warn" : "info"}`, text: r.status }))))))) : null,
    );
  }
  draw();
  return box;
}

async function setImport(data) {
  const theSet = data.set;
  const node = h("div");
  let result = await GET(`/api/sets/${enc(theSet.id)}/candidates`);

  const startImport = async (button, key) => {
    key = (key || "").trim();
    if (!key) return toast("Which TCGdex set? Type its TCGdex ID, like base1, or pick from the list.", "warn");
    const go = await ask({
      title: `Import "${key}" from TCGdex?`,
      body: `This fetches that one set's cards from TCGdex and keeps what TCGdex says as suggestions for ${theSet.name}. It creates no cards: you choose which to accept, and every accepted card starts unreviewed.`,
      ok: "Import",
    });
    if (!go) return;
    await busy(button, async () => {
      result = await POST(`/api/sets/${enc(theSet.id)}/import`, { source: "tcgdex", key });
      const missing = result.imported.missing.length;
      toast(`TCGdex answered for ${plural(result.imported.cards, "card")}${missing ? `; ${missing} did not answer` : ""}.`, missing ? "warn" : "ok");
      draw();
    });
  };

  function picker(label) {
    const input = h("input", { type: "text", placeholder: "TCGdex set ID, like base1", value: result.import?.key || "" });
    const list = h("datalist", { id: "tcgdex-sets" });
    input.setAttribute("list", "tcgdex-sets");
    return h("div", { class: "row" }, h("div", { style: { width: "260px" } }, input, list),
      h("button", { type: "button", text: "Show TCGdex's sets", onclick: (e) => busy(e.currentTarget, async () => {
        const answer = await GET(`/api/tcgdex/sets?catalog=${enc(data.catalog.id)}`);
        fill(list, ...answer.sets.map((x) => h("option", { value: x.id, text: `${x.name} (${x.cards ?? "?"} cards)` })));
        toast(`TCGdex lists ${plural(answer.sets.length, "set")}. Start typing a name or ID.`);
        input.focus();
      }) }),
      h("button", { type: "button", class: "primary", text: label, onclick: (e) => startImport(e.currentTarget, input.value) }));
  }

  function draw() {
    if (!result.import) {
      fill(node, h("div", { class: "box notice" }, h("h3", { text: "Import this set from TCGdex" }),
        h("p", { text: "Nothing is imported unless you start it here. An import brings one set's cards as suggestions; you decide which become cards, and each one starts unreviewed." }),
        picker("Import…")));
      return;
    }
    const imp = result.import;
    const selectable = result.candidates.filter((c) => c.status === "new");
    const selected = new Set(selectable.filter((c) => !c.problems.length).map((c) => c.key));
    const counter = h("span", { class: "muted" });
    const acceptButton = h("button", { class: "primary", onclick: (e) => busy(e.currentTarget, async () => {
      if (!selected.size) return;
      const answer = await POST(`/api/sets/${enc(theSet.id)}/accept`, { keys: [...selected] });
      result = answer;
      const skipped = Object.keys(answer.accepted.skipped).length;
      toast(`Made ${plural(answer.accepted.created, "card")} with ${plural(answer.accepted.printings, "printing")}${skipped ? `; skipped ${skipped}` : ""}.`);
      await refreshBoot();
      draw();
    }) });
    const syncCount = () => { counter.textContent = `${selected.size} selected`; acceptButton.textContent = `Accept ${plural(selected.size, "card")}`; acceptButton.disabled = !selected.size; };

    const statusText = { new: ["new", "info"], accepted: ["accepted", "reviewed"], changed: ["TCGdex changed it", "warn"],
      "number-taken": ["number already used", "warn"], unusable: ["cannot be read", "bad"], missing: ["no answer", "bad"] };
    fill(node, 
      h("div", { class: "box" }, h("h3", { text: "Imported from TCGdex" }),
        h("p", {}, `TCGdex set `, h("span", { class: "mono", text: imp.key }), `, fetched ${dateOnly(imp.fetched_at)}. `,
          `${plural(result.candidates.length, "card")}: ${result.candidates.filter((c) => c.status === "new").length} not yet accepted.`),
        h("details", {}, h("summary", { class: "muted", text: "Import again, or import a different TCGdex set" }),
          h("p", { class: "dim", text: "Importing again refreshes what TCGdex says. Cards you already made are not changed; any whose TCGdex data changed are marked so you can compare." }),
          picker("Import again…"))),
      h("div", { class: "row", style: { marginBottom: "10px" } }, acceptButton, counter,
        h("button", { class: "small", text: "Select all new", onclick: () => { selectable.forEach((c) => selected.add(c.key)); draw2(); } }),
        h("button", { class: "small", text: "Select none", onclick: () => { selected.clear(); draw2(); } })),
    );
    const table = h("table", { class: "list" });
    const draw2 = () => {
      fill(table, h("thead", {}, h("tr", {}, h("th"), h("th"), h("th", { text: "No." }), h("th", { text: "Name" }),
        h("th", { text: "Printings" }), h("th", { text: "Status" }), h("th", { text: "Not carried over" }))),
        h("tbody", {}, result.candidates.map((c) => {
          const [label, cls] = statusText[c.status] || [c.status, ""];
          return h("tr", {},
            h("td", {}, c.status === "new" ? h("input", { type: "checkbox", checked: selected.has(c.key), onchange: (e) => {
              if (e.target.checked) selected.add(c.key); else selected.delete(c.key);
              syncCount();
            } }) : null),
            h("td", {}, c.image_url ? h("img", { class: "thumb", src: c.image_url.replace("/high.webp", "/low.webp"), loading: "lazy", alt: "" }) : null),
            h("td", { class: "mono", text: c.printed_number || c.number || c.key }),
            h("td", {}, c.matched ? h("a", { href: `#/card/${enc(c.matched)}`, text: c.name }) : (c.name || "")),
            h("td", { class: "dim", text: (c.printings || []).join(", ") }),
            h("td", {}, h("span", { class: `badge ${cls}`, text: label })),
            h("td", { class: "dim" }, (c.problems || []).length ? h("ul", { class: "problems" }, c.problems.map((p) => h("li", { text: p }))) : ""));
        })));
      syncCount();
    };
    node.append(table,
      h("p", { class: "dim", text: "Cards with something not carried over start unselected. Add the missing word or term under Words & terms first and it will map, or accept and fix the card by hand." }));
    draw2();
  }
  draw();
  return node;
}

async function setPublish(data) {
  const theSet = data.set;
  const { problems } = await GET(`/api/sets/${enc(theSet.id)}/problems`);
  const node = h("div");
  const subjectLink = (subject) => {
    if (subject.startsWith(theSet.id + "-")) return h("a", { href: `#/card/${enc(subject.split("_")[0])}`, class: "mono", text: subject });
    if (subject === theSet.id) return h("a", { href: `#/set/${enc(theSet.id)}/details`, class: "mono", text: subject });
    return h("a", { href: "#/", class: "mono", text: subject });
  };
  node.append(
    problems.length
      ? h("div", { class: "box warn" }, h("h3", { text: `${plural(problems.length, "thing")} to do before publishing` }),
        h("ul", { class: "problems" }, problems.map((p) => h("li", {}, subjectLink(p.subject), " — ", p.problem))))
      : h("div", { class: "box notice" }, h("h3", { text: "Ready to publish" }),
        h("p", { text: theSet.version
          ? `Publishing writes version ${theSet.version + 1} for the app. Version ${theSet.version} stays as it is for anyone who already has it.`
          : "Publishing writes version 1 and adds this set to what the app downloads." }),
        h("p", { class: "muted", text: "Everything that goes out is locked: its IDs cannot change and its cards and printings cannot be deleted, only withdrawn." }),
        h("button", { class: "primary", text: `Publish version ${theSet.version + 1}`, onclick: async (e) => {
        const button = e.currentTarget;
          const go = await ask({ title: `Publish ${theSet.name}?`, body: `Version ${theSet.version + 1}, with ${plural(data.cards.filter((c) => !c.withdrawn).length, "card")}.`, ok: "Publish" });
          if (!go) return;
          await busy(button, async () => {
            const answer = await POST(`/api/sets/${enc(theSet.id)}/publish`);
            toast(`Published version ${answer.publish.version}: ${plural(answer.cards, "card")}, ${plural(answer.printings, "printing")}.`);
            await refreshBoot();
            render();
          });
        } })),
    h("div", { class: "box" }, h("h3", { text: "Published versions" }),
      data.publishes.length ? h("table", { class: "list" }, h("thead", {}, h("tr", {}, ["Version", "Published", "Cards", "Printings", "File"].map((t) => h("th", { text: t })))),
        h("tbody", {}, data.publishes.map((p) => h("tr", {}, h("td", { class: "num", text: p.version }), h("td", { text: new Date(p.published_at).toLocaleString() }),
          h("td", { class: "num", text: p.cards }), h("td", { class: "num", text: p.printings }),
          h("td", {}, h("a", { href: imageUrl(p.file), class: "mono", target: "_blank", text: p.file }))))))
        : h("p", { class: "dim", text: "Never published." }),
      h("div", { class: "row", style: { marginTop: "10px" } },
        h("button", { class: "small", text: "Publish the index again", title: "Rewrites catalog/index.json from what is published. Only needed if a publish stopped part way.",
          onclick: (e) => busy(e.currentTarget, async () => { const r = await POST("/api/publish-index"); toast(`Index rewritten: ${plural(r.sets, "published set")}.`); }) }))),
  );
  return node;
}

// ============================================================================ card

const CATEGORY_OPTIONS = [{ value: "pokemon", label: "Pokémon" }, { value: "trainer", label: "Trainer" }, { value: "energy", label: "Energy" }];
const CARD_FIELDS = ["number", "printed_number", "number_assigned", "section", "sort", "name", "name_en", "category", "subtypes", "hp",
  "types", "evolves_from", "abilities", "attacks", "weaknesses", "resistances", "retreat", "rules", "flavor_text", "illustrator", "rarity",
  "regulation_mark", "dex_numbers", "no_image", "review", "review_note", "withdrawn", "notes"];
const COMPARED = { number: "Number", printed_number: "Printed number", name: "Name", category: "Category", subtypes: "Subtypes", hp: "HP",
  types: "Types", evolves_from: "Evolves from", abilities: "Abilities", attacks: "Attacks", weaknesses: "Weaknesses", resistances: "Resistances",
  retreat: "Retreat", rules: "Card text", flavor_text: "Flavor text", illustrator: "Illustrator", rarity: "Rarity", regulation_mark: "Regulation mark",
  dex_numbers: "Pokédex numbers" };
const PRINTING_FIELDS = ["edition", "pattern", "finish", "stamps", "error", "tcgplayer_product", "tcgplayer_printing", "tcgplayer_via", "identify", "review", "review_note", "withdrawn", "notes"];

function describe(field, value) {
  if (value === null || value === undefined || (Array.isArray(value) && !value.length)) return "(empty)";
  if (field === "types") return value.map((t) => termLabel("type", t)).join(", ");
  if (field === "subtypes") return value.map((t) => termLabel("subtype", t)).join(", ");
  if (field === "rarity") return termLabel("rarity", value);
  if (field === "attacks") return value.map((a) => `${a.name} [${(a.cost || []).map((t) => termLabel("type", t)).join(" ")}] ${a.damage || ""}${a.text ? `: ${a.text}` : ""}`).join("\n");
  if (field === "abilities") return value.map((a) => `${termLabel("ability_kind", a.kind)}: ${a.name}${a.text ? ` — ${a.text}` : ""}`).join("\n");
  if (field === "weaknesses" || field === "resistances") return value.map((w) => `${termLabel("type", w.type)} ${w.value || ""}`).join(", ");
  if (field === "rules") return value.join("\n\n");
  if (Array.isArray(value)) return value.join(", ");
  return String(value);
}

function variantOf(p) {
  const stampOrder = (w) => [wordRow(w)?.sort ?? 0, w];
  const stamps = [...new Set(p.stamps || [])].sort((a, b) => { const x = stampOrder(a), y = stampOrder(b); return x[0] - y[0] || x[1].localeCompare(y[1]); });
  return [p.edition, p.pattern, p.finish, ...stamps, p.error].filter(Boolean).join("-");
}

async function viewCard(id) {
  const data = await GET(`/api/cards/${enc(id)}`);
  const card = data.card;
  const theSet = data.set;
  const source = data.sources[0] || null;
  const sourceName = source ? `TCGdex ${source.key}` : null;
  const form = new Form(card, CARD_FIELDS, setUnsavedIndicator);
  const printingForms = data.printings.map((p) => new Form(p, PRINTING_FIELDS, setUnsavedIndicator));
  let images = data.images;

  const typeOptions = terms("type").map((t) => ({ value: t.code, label: termLabel("type", t.code) }));
  const subtypeOptions = terms("subtype").map((t) => ({ value: t.code, label: termLabel("subtype", t.code) }));
  const rarityOptions = terms("rarity").map((t) => ({ value: t.code, label: termLabel("rarity", t.code) }));
  const abilityOptions = terms("ability_kind").map((t) => ({ value: t.code, label: termLabel("ability_kind", t.code) }));

  // ---- left column: picture and review
  const front = pictureSlot({
    kind: "card", id: card.id, role: "front", images: images.filter((i) => i.card_id === card.id), label: "picture",
    suggestions: source?.image_url ? [{ label: "Use TCGdex's picture", url: source.image_url, source_id: "tcgdex" }] : [],
    noImage: { value: () => form.model.no_image },
    onChanged: () => { if (form.model.no_image) { form.model.no_image = false; form.original.no_image = false; drawReview(); } },
  });
  const reviewBox = h("div", { class: "box reviewbox" });

  async function save({ review, reviewNote, quiet } = {}) {
    if (review) { form.model.review = review; form.model.review_note = reviewNote ?? null; }
    const patch = form.patch();
    let savedCard = card;
    if (Object.keys(patch).length) {
      savedCard = await PATCH(`/api/cards/${enc(card.id)}`, patch);
      form.saved(savedCard);
    }
    for (const pf of printingForms) {
      if (review === "reviewed" && pf.model.review !== "reviewed" && !pf.model.withdrawn) pf.model.review = "reviewed";
      const pp = pf.patch();
      if (Object.keys(pp).length) {
        const row = await PATCH(`/api/printings/${enc(pf.original.id)}`, pp);
        pf.saved(row);
      }
    }
    await refreshBoot();
    if (!quiet) toast(review === "reviewed" ? "Saved and marked reviewed." : "Saved.");
    if (savedCard.id !== card.id) { S.guard = null; location.hash = `#/card/${enc(savedCard.id)}`; return savedCard; }
    drawAll();
    return savedCard;
  }

  function drawReview() {
    const m = form.model;
    fill(reviewBox, 
      h("h3", {}, "Review", h("span", { class: "spacer" }), reviewBadge(form.original.review)),
      form.original.review === "flagged" && form.original.review_note ? h("p", { class: "muted", text: `Flag: ${form.original.review_note}` }) : null,
      h("div", { class: "row" },
        h("button", { class: "good", title: "Ctrl+Enter: save, mark reviewed and go to the next card", text: "✓ Reviewed", onclick: (e) => busy(e.currentTarget, async () => {
          await save({ review: "reviewed" });
        }) }),
        h("button", { text: "⚑ Flag…", onclick: async () => {
          const values = await ask({ title: "Flag this card", body: "A flagged card blocks publishing until it is reviewed.", ok: "Flag",
            fields: [{ name: "note", label: "What needs checking?", value: m.review_note || "" }] });
          if (values) await save({ review: "flagged", reviewNote: values.note || null }).catch(fail);
        } })),
      form.original.review !== "unreviewed" ? h("button", { class: "small", text: "Back to unreviewed", onclick: () => save({ review: "unreviewed" }).catch(fail) }) : null,
      h("p", { class: "dim" }, h("kbd", { text: "Ctrl" }), "+", h("kbd", { text: "Enter" }), " reviewed and next · ",
        h("kbd", { text: "Ctrl" }), "+", h("kbd", { text: "S" }), " save · ", h("kbd", { text: "Alt" }), "+", h("kbd", { text: "←/→" }), " previous/next"),
    );
  }

  // ---- right column: the form, rebuilt from the model whenever a suggestion is used
  const right = h("div");
  const sug = (field) => (source ? suggestion(form, field, source.fields[field], sourceName, (v) => describe(field, v), drawAll) : null);

  function listEditor(field, label, blank, drawItem) {
    const holder = h("div", { class: "listedit" });
    const draw = () => {
      const items = form.model[field] || [];
      fill(holder, 
        items.map((item, i) => h("div", { class: "item" }, drawItem(item, (patch) => {
          const next = clone(form.model[field]); next[i] = { ...next[i], ...patch }; form.set(field, next);
        }, () => { const next = clone(form.model[field]); next.splice(i, 1); form.set(field, next); draw(); }))),
        h("div", { class: "row" }, h("button", { type: "button", class: "small", text: `+ ${blank.label}`, onclick: () => {
          form.set(field, (form.model[field] || []).concat([clone(blank.value)])); draw();
        } })),
      );
    };
    draw();
    return form.wrap(field, label, holder, { wide: true, suggest: sug(field) });
  }

  const inputFor = (value, onChange, extra = {}) => {
    const el = extra.area ? h("textarea", { rows: 2, value: value ?? "" }) : h("input", { type: "text", value: value ?? "", placeholder: extra.placeholder || "" });
    el.addEventListener("input", () => onChange(el.value === "" ? null : el.value));
    return el;
  };
  const selectFor = (value, options, onChange, empty) => {
    const el = h("select", {}, empty !== undefined ? h("option", { value: "", text: empty }) : null,
      options.map((o) => h("option", { value: o.value, text: o.label, selected: o.value === value })));
    el.addEventListener("change", () => onChange(el.value || null));
    return el;
  };
  const little = (label, control) => h("label", { class: "field" }, h("span", { class: "label", text: label }), control);

  function costEditor(cost, onChange) {
    const box = h("div", { class: "chips" });
    const draw = () => fill(box, 
      (cost || []).map((t, i) => h("span", { class: "chip" }, termLabel("type", t),
        h("button", { type: "button", text: "×", onclick: (e) => { e.preventDefault(); cost = cost.slice(); cost.splice(i, 1); onChange(cost); draw(); } }))),
      h("select", { onchange: (e) => { if (!e.target.value) return; cost = (cost || []).concat(e.target.value); onChange(cost); draw(); } },
        h("option", { value: "", text: "+ energy" }), typeOptions.map((o) => h("option", { value: o.value, text: o.label }))));
    draw();
    return box;
  }

  function rulesEditor() {
    const holder = h("div", { class: "listedit" });
    const draw = () => {
      const rules = form.model.rules || [];
      fill(holder, 
        rules.map((text, i) => h("div", { class: "row" }, h("div", { style: { flex: "1" } }, inputFor(text, (v) => {
          const next = form.model.rules.slice(); next[i] = v || ""; form.set("rules", next);
        }, { area: true })), h("button", { type: "button", class: "small danger", text: "×", onclick: () => {
          const next = form.model.rules.slice(); next.splice(i, 1); form.set("rules", next); draw();
        } }))),
        h("div", { class: "row" }, h("button", { type: "button", class: "small", text: "+ paragraph", onclick: () => { form.set("rules", rules.concat([""])); draw(); } })));
    };
    draw();
    return form.wrap("rules", "Card text", holder, { wide: true, hint: "A Trainer's or Energy's text, and rule boxes. One paragraph each.", suggest: sug("rules") });
  }

  function printingEditor(pf) {
    const p = pf.model;
    const locked = pf.original.locked;
    const name = h("span", { class: "mono" });
    const syncName = () => { name.textContent = `${card.id}_${variantOf(pf.model)}`; };
    const pictures = images.filter((i) => i.printing_id === pf.original.id);
    const opts = (kind) => words(kind).map((w) => ({ value: w.word, label: w.label }));
    const partSelect = (field, label, empty) => {
      const wrapper = pf.select(field, label, opts(field === "stamps" ? "stamp" : field), { empty, disabled: locked });
      wrapper.querySelector("select").addEventListener("change", syncName);
      return wrapper;
    };
    const stamps = pf.chips("stamps", "Stamps", opts("stamp"), { disabled: locked });
    stamps.addEventListener("change", syncName);
    stamps.addEventListener("click", () => setTimeout(syncName, 0));
    syncName();
    const box = h("div", { class: `printing${p.withdrawn ? " withdrawn" : ""}` },
      h("div", { class: "head" }, name, reviewBadge(pf.original.review), locked ? h("span", { class: "badge locked", text: "published" }) : null,
        h("span", { class: "spacer" }),
        h("button", { class: "small good", text: "✓", title: "Mark this printing reviewed (saved with the card)", onclick: () => { pf.set("review", "reviewed"); toast("Marked; press Save to keep it.", "warn", 2000); } }),
        locked ? null : h("button", { class: "small danger", text: "Delete", onclick: async () => {
          if (!(await ask({ title: "Delete this printing?", body: `${card.id}_${pf.original.variant}`, ok: "Delete", danger: true }))) return;
          try {
            await DELETE(`/api/printings/${enc(pf.original.id)}`);
            printingForms.splice(printingForms.indexOf(pf), 1);
            await refreshBoot();
            drawAll();
          } catch (e) { fail(e); }
        } })),
      h("div", { class: "fields" },
        partSelect("edition", "Edition", "Unlimited"),
        partSelect("pattern", "Foil pattern", "None"),
        partSelect("finish", "Finish"),
        partSelect("error", "Misprint", "None"),
        h("div", { class: "wide" }, stamps),
        pf.text("identify", "How to tell it apart", { wide: true, placeholder: "e.g. no drop shadow to the right of the art box" }),
        pf.number("tcgplayer_product", "TCGplayer product ID", {
          suggest: pf.original.tcgplayer_product ? h("a", { href: `https://www.tcgplayer.com/product/${pf.original.tcgplayer_product}`, target: "_blank", text: "View on TCGplayer ↗" }) : null }),
        pf.text("tcgplayer_printing", "TCGplayer printing", { placeholder: "Reverse Holofoil" }),
        pf.select("tcgplayer_via", "Product matched", [{ value: "manual", label: "by hand" }, { value: "auto", label: "automatically" }], { empty: "—",
          hint: "Set a product by hand and choose \"by hand\", and matching will never change it." }),
        pf.check("withdrawn", "Withdrawn"),
      ),
      h("details", {}, h("summary", { text: pictures.some((i) => i.chosen) ? "Its own picture" : "Give it its own picture (only if it looks different)" }),
        pictureSlot({ kind: "printing", id: pf.original.id, role: "front", images: pictures, label: "picture" })),
    );
    return box;
  }

  async function addPrinting() {
    const opts = (kind, empty) => [{ value: "", label: empty }].concat(words(kind).map((w) => ({ value: w.word, label: w.label })));
    const values = await ask({
      title: "Add a printing",
      body: "Its ID is built from these words. Stamps can be added once it exists.",
      ok: "Add printing",
      fields: [
        { name: "finish", label: "Finish", options: words("finish").map((w) => ({ value: w.word, label: w.label })), value: "normal" },
        { name: "edition", label: "Edition", options: opts("edition", "Unlimited") },
        { name: "pattern", label: "Foil pattern", options: opts("pattern", "None") },
        { name: "error", label: "Misprint", options: opts("error", "None") },
      ],
    });
    if (!values) return;
    try {
      const row = await POST("/api/printings", { card_id: card.id, finish: values.finish, edition: values.edition || null,
        pattern: values.pattern || null, error: values.error || null });
      printingForms.push(new Form(row, PRINTING_FIELDS, setUnsavedIndicator));
      await refreshBoot();
      drawAll();
    } catch (e) { fail(e); }
  }

  function sourcePanel() {
    if (!source) return null;
    const differences = Object.keys(COMPARED).filter((f) => !same(source.fields[f] ?? null, form.model[f] ?? null)
      && !((source.fields[f] === null || (Array.isArray(source.fields[f]) && !source.fields[f].length)) && (form.model[f] === null || (Array.isArray(form.model[f]) && !form.model[f].length))));
    const ours = new Set(printingForms.map((pf) => pf.original.variant));
    const missingPrintings = source.printings.filter((p) => !ours.has(p.variant));
    return h("div", { class: `box ${source.changed ? "warn" : ""}` },
      h("h3", {}, `What ${sourceName} says`, source.changed ? h("span", { class: "badge warn", text: "changed since you reviewed it" }) : null,
        h("span", { class: "spacer" }), h("span", { class: "dim", text: `fetched ${dateOnly(source.fetched_at)}` })),
      differences.length || missingPrintings.length ? h("div", { class: "diff" },
        differences.map((f) => [h("div", { class: "k", text: COMPARED[f] }),
          h("div", { class: "v" }, h("div", { text: describe(f, source.fields[f]) }), h("div", { class: "was", text: describe(f, form.model[f]) })),
          h("button", { class: "small", text: "Use", onclick: () => { form.set(f, clone(source.fields[f])); drawAll(); } })]),
        missingPrintings.map((p) => [h("div", { class: "k", text: "Printing" }), h("div", { class: "v mono", text: p.variant }),
          h("button", { class: "small", text: "Add", onclick: async () => {
            try {
              const row = await POST("/api/printings", { card_id: card.id, edition: p.edition, pattern: p.pattern, finish: p.finish, stamps: p.stamps, error: p.error });
              printingForms.push(new Form(row, PRINTING_FIELDS, setUnsavedIndicator));
              await refreshBoot();
              drawAll();
            } catch (e) { fail(e); }
          } })]),
      ) : h("p", { class: "muted", text: "Everything matches." }),
      source.problems.length ? h("div", {}, h("p", { class: "dim", text: "Not carried over from TCGdex:" }), h("ul", { class: "problems" }, source.problems.map((p) => h("li", { text: p })))) : null,
    );
  }

  function drawAll() {
    const m = form.model;
    const locked = form.original.locked;
    const isPokemon = m.category === "pokemon";
    fill(right, 
      sourcePanel(),
      h("div", { class: "box" }, h("h3", { text: "Card" }),
        h("div", { class: "fields" },
          form.text("name", "Name", { suggest: sug("name") }),
          theSet && S.boot.catalogs.find((c) => c.id === S.catalog)?.language !== "en" ? form.text("name_en", "English name") : null,
          form.text("number", "Number", { disabled: locked, hint: locked ? "Locked: published." : "Before the slash, lowercase. Part of the ID.", suggest: locked ? null : sug("number") }),
          form.text("printed_number", "Printed number", { suggest: sug("printed_number") }),
          form.select("category", "Category", CATEGORY_OPTIONS, { suggest: sug("category") }),
          form.select("rarity", "Rarity", rarityOptions, { empty: "—", suggest: sug("rarity") }),
          form.text("illustrator", "Illustrator", { suggest: sug("illustrator") }),
          form.text("section", "Section", { placeholder: "e.g. Trainer Gallery", hint: "A subset inside this set." }),
          form.number("sort", "Order in the set", { hint: "Empty: by number." }),
          form.text("regulation_mark", "Regulation mark", { suggest: sug("regulation_mark") }),
          form.check("number_assigned", "No printed number (assigned)"),
          form.chips("subtypes", "Subtypes", subtypeOptions, { wide: true, suggest: sug("subtypes") }),
        )),
      isPokemon ? h("div", { class: "box" }, h("h3", { text: "Pokémon" }),
        h("div", { class: "fields" },
          form.number("hp", "HP", { suggest: sug("hp") }),
          form.chips("types", "Types", typeOptions, { suggest: sug("types") }),
          form.text("evolves_from", "Evolves from", { suggest: sug("evolves_from") }),
          form.number("retreat", "Retreat cost", { suggest: sug("retreat") }),
          (() => {
            const w = form.text("dex_numbers", "Pokédex numbers", { hint: "Comma-separated.", suggest: sug("dex_numbers") });
            const input = w.querySelector("input");
            input.value = (m.dex_numbers || []).join(", ");
            input.addEventListener("input", () => form.set("dex_numbers", input.value.split(/[\s,]+/).filter(Boolean).map(Number).filter((n) => Number.isInteger(n))));
            return w;
          })(),
          listEditor("abilities", "Abilities", { label: "ability", value: { name: "", kind: "ability", text: "" } }, (a, update, remove) => [
            h("div", { class: "row" }, little("Kind", selectFor(a.kind, abilityOptions, (v) => update({ kind: v }))),
              little("Name", inputFor(a.name, (v) => update({ name: v || "" }))),
              h("button", { type: "button", class: "small danger", text: "×", onclick: remove })),
            little("Text", inputFor(a.text, (v) => update({ text: v }), { area: true })),
          ]),
          listEditor("attacks", "Attacks", { label: "attack", value: { name: "", cost: [], damage: null, text: null } }, (a, update, remove) => [
            h("div", { class: "row" }, little("Name", inputFor(a.name, (v) => update({ name: v || "" }))),
              little("Damage", inputFor(a.damage, (v) => update({ damage: v }), { placeholder: "30+" })),
              h("button", { type: "button", class: "small danger", text: "×", onclick: remove })),
            little("Cost", costEditor(a.cost, (v) => update({ cost: v }))),
            little("Text", inputFor(a.text, (v) => update({ text: v }), { area: true })),
          ]),
          listEditor("weaknesses", "Weaknesses", { label: "weakness", value: { type: "fire", value: "×2" } }, (w, update, remove) => [
            h("div", { class: "row" }, little("Type", selectFor(w.type, typeOptions, (v) => update({ type: v }))),
              little("Value", inputFor(w.value, (v) => update({ value: v }), { placeholder: "×2" })),
              h("button", { type: "button", class: "small danger", text: "×", onclick: remove }))]),
          listEditor("resistances", "Resistances", { label: "resistance", value: { type: "fighting", value: "-30" } }, (w, update, remove) => [
            h("div", { class: "row" }, little("Type", selectFor(w.type, typeOptions, (v) => update({ type: v }))),
              little("Value", inputFor(w.value, (v) => update({ value: v }), { placeholder: "-30" })),
              h("button", { type: "button", class: "small danger", text: "×", onclick: remove }))]),
        )) : null,
      h("div", { class: "box" }, h("h3", { text: "Text" }),
        h("div", { class: "fields" }, rulesEditor(), form.area("flavor_text", "Flavor text", { suggest: sug("flavor_text") }))),
      h("div", { class: "box" }, h("h3", {}, "Printings", h("span", { class: "spacer" }),
        h("button", { class: "small", text: "+ Add printing", onclick: addPrinting })),
        printingForms.length ? printingForms.map(printingEditor) : h("p", { class: "warn", text: "No printings yet. A card needs at least one to be published." })),
      h("div", { class: "box" }, h("h3", { text: "Other" }),
        h("div", { class: "fields" },
          form.check("withdrawn", "Withdrawn", { hint: "Hidden from the app. Published cards are withdrawn, never deleted." }),
          form.area("notes", "Notes", { rows: 3 }))),
      h("div", { class: "savebar" },
        h("button", { class: "primary", text: "Save", onclick: (e) => busy(e.currentTarget, () => save()) }),
        h("button", { class: "good", text: "Save, reviewed, next →", onclick: (e) => busy(e.currentTarget, () => reviewedAndNext()) }),
        h("span", { class: "spacer" }),
        locked ? null : h("button", { class: "danger", text: "Delete card", onclick: async (e) => {
        const button = e.currentTarget;
          if (!(await ask({ title: `Delete ${card.name}?`, body: "Its printings and pictures go with it. It has never been published.", ok: "Delete", danger: true }))) return;
          await busy(button, async () => {
            await DELETE(`/api/cards/${enc(card.id)}`);
            S.guard = null;
            await refreshBoot();
            location.hash = `#/set/${enc(card.set_id)}`;
          });
        } })),
    );
    drawReview();
    setUnsavedIndicator();
  }

  async function reviewedAndNext() {
    const saved = await save({ review: "reviewed", quiet: true });
    toast("Reviewed.");
    if (data.next && saved && saved.id === card.id) location.hash = `#/card/${enc(data.next)}`;
  }

  drawAll();

  const node = h("div", {},
    h("div", { class: "crumbs" }, h("a", { href: `#/series/${enc(theSet.series_id)}`, text: "Series" }), " › ",
      h("a", { href: `#/set/${enc(theSet.id)}`, text: theSet.name })),
    h("div", { class: "title" }, h("h2", { text: card.name }), h("span", { class: "id mono", text: card.id }),
      card.locked ? h("span", { class: "badge locked", text: "published" }) : null,
      card.withdrawn ? h("span", { class: "badge", text: "withdrawn" }) : null,
      h("span", { class: "spacer" }),
      h("div", { class: "navbar" },
        h("button", { class: "small", text: "← Previous", disabled: !data.previous, onclick: () => { location.hash = `#/card/${enc(data.previous)}`; } }),
        h("span", { class: "muted num", text: `${data.position} of ${data.count}` }),
        h("button", { class: "small", text: "Next →", disabled: !data.next, onclick: () => { location.hash = `#/card/${enc(data.next)}`; } }))),
    h("div", { class: "cardview" },
      h("div", { class: "left" }, h("div", { class: "box" }, front,
        form.check("no_image", "No picture exists anywhere yet", { hint: "The app will show the card back." })), reviewBox),
      right),
  );
  // The no-picture checkbox lives outside drawAll, so keep the frame's message in step with it.
  node.querySelector(".left input[type=checkbox]").addEventListener("change", () => front.refresh(images.filter((i) => i.card_id === card.id)));

  node._guard = () => form.dirty() || printingForms.some((pf) => pf.dirty());
  node._paste = () => front;
  node._keys = (e) => {
    const key = e.key.toLowerCase();
    if (e.ctrlKey && key === "s") { e.preventDefault(); save().catch(fail); }
    else if (e.ctrlKey && key === "enter") { e.preventDefault(); reviewedAndNext().catch(fail); }
    else if (e.altKey && e.key === "ArrowRight" && data.next) { e.preventDefault(); location.hash = `#/card/${enc(data.next)}`; }
    else if (e.altKey && e.key === "ArrowLeft" && data.previous) { e.preventDefault(); location.hash = `#/card/${enc(data.previous)}`; }
  };
  return node;
}

// ============================================================================ words and terms

async function viewWords() {
  const node = h("div", {},
    h("div", { class: "title" }, h("h2", { text: "Words & terms" })),
    h("p", { class: "muted", text: "Printing IDs are built from variation words, and cards pick rarities, types, subtypes and ability kinds from terms. A word or term in use can have its label changed, but cannot be renamed or removed." }));

  const kinds = [["finish", "Finishes"], ["edition", "Editions"], ["pattern", "Foil patterns"], ["stamp", "Stamps"], ["error", "Misprints"]];
  for (const [kind, title] of kinds) {
    const list = words(kind);
    node.append(h("details", { class: "box", open: kind === "finish" },
      h("summary", {}, h("b", { text: title }), h("span", { class: "dim", text: ` · ${list.length}` })),
      h("table", { class: "list", style: { marginTop: "8px" } },
        h("thead", {}, h("tr", {}, ["Word (in IDs)", "Label (in the app)", "How to recognise it", "Order", ""].map((t) => h("th", { text: t })))),
        h("tbody", {}, list.map((w) => wordRowEditor(w)), newWordRow(kind)))));
  }

  const termKinds = [["rarity", "Rarities"], ["type", "Energy types"], ["subtype", "Subtypes"], ["ability_kind", "Ability kinds"]];
  const langs = S.boot.catalogs.map((c) => c.language);
  for (const [kind, title] of termKinds) {
    const list = terms(kind);
    node.append(h("details", { class: "box" },
      h("summary", {}, h("b", { text: title }), h("span", { class: "dim", text: ` · ${list.length}` })),
      h("table", { class: "list", style: { marginTop: "8px" } },
        h("thead", {}, h("tr", {}, h("th", { text: "Code" }), langs.map((l) => h("th", { text: `Label (${l})` })), h("th", { text: "Order" }), h("th"))),
        h("tbody", {}, list.map((t) => termRowEditor(t, langs)), newTermRow(kind, langs)))));
  }
  return node;
}

function wordRowEditor(w) {
  const label = h("input", { type: "text", value: w.label });
  const description = h("input", { type: "text", value: w.description || "" });
  const sort = h("input", { type: "number", value: w.sort, style: { width: "70px" } });
  return h("tr", {}, h("td", { class: "mono", text: w.word }), h("td", {}, label), h("td", {}, description), h("td", {}, sort),
    h("td", {}, h("button", { class: "small", text: "Save", onclick: (e) => busy(e.currentTarget, async () => {
      await PATCH(`/api/words/${enc(w.word)}`, { label: label.value, description: description.value || null, sort: sort.value });
      await refreshBoot();
      toast(`Saved ${w.word}.`);
    }) })));
}

function newWordRow(kind) {
  const word = h("input", { type: "text", placeholder: "new-word" });
  const label = h("input", { type: "text", placeholder: "Label" });
  const description = h("input", { type: "text" });
  const sort = h("input", { type: "number", value: 0, style: { width: "70px" } });
  return h("tr", {}, h("td", {}, word), h("td", {}, label), h("td", {}, description), h("td", {}, sort),
    h("td", {}, h("button", { class: "small primary", text: "Add", onclick: (e) => busy(e.currentTarget, async () => {
      await POST("/api/words", { word: word.value.trim().toLowerCase(), kind, label: label.value, description: description.value || null, sort: sort.value });
      await refreshBoot();
      toast(`Added ${word.value}.`);
      render();
    }) })));
}

function termRowEditor(t, langs) {
  const inputs = Object.fromEntries(langs.map((l) => [l, h("input", { type: "text", value: t.labels[l] || "" })]));
  const sort = h("input", { type: "number", value: t.sort, style: { width: "70px" } });
  return h("tr", {}, h("td", { class: "mono", text: t.code }), langs.map((l) => h("td", {}, inputs[l])), h("td", {}, sort),
    h("td", {}, h("button", { class: "small", text: "Save", onclick: (e) => busy(e.currentTarget, async () => {
      const labels = Object.fromEntries(langs.map((l) => [l, inputs[l].value]));
      await PATCH(`/api/terms/${enc(t.kind)}/${enc(t.code)}`, { labels, sort: sort.value });
      await refreshBoot();
      toast(`Saved ${t.code}.`);
    }) })));
}

function newTermRow(kind, langs) {
  const code = h("input", { type: "text", placeholder: "new-code" });
  const inputs = Object.fromEntries(langs.map((l) => [l, h("input", { type: "text" })]));
  const sort = h("input", { type: "number", value: 0, style: { width: "70px" } });
  return h("tr", {}, h("td", {}, code), langs.map((l) => h("td", {}, inputs[l])), h("td", {}, sort),
    h("td", {}, h("button", { class: "small primary", text: "Add", onclick: (e) => busy(e.currentTarget, async () => {
      const labels = Object.fromEntries(langs.map((l) => [l, inputs[l].value]));
      await POST("/api/terms", { kind, code: code.value.trim().toLowerCase(), labels, sort: sort.value });
      await refreshBoot();
      toast(`Added ${code.value}.`);
      render();
    }) })));
}

// ============================================================================ lists

function setName(setId) {
  return S.boot.sets.find((x) => x.id === setId)?.name || setId;
}

async function viewReviewList() {
  const { cards } = await GET(`/api/lists/review?catalog=${enc(S.catalog)}`);
  return h("div", {},
    h("div", { class: "title" }, h("h2", { text: "Needs review" }), h("span", { class: "muted", text: plural(cards.length, "card") })),
    cards.length ? h("table", { class: "list" }, h("thead", {}, h("tr", {}, ["Set", "No.", "Name", "Review", "Note"].map((t) => h("th", { text: t })))),
      h("tbody", {}, cards.map((c) => h("tr", { class: "click", onclick: () => { location.hash = `#/card/${enc(c.id)}`; } },
        h("td", { text: setName(c.set_id) }), h("td", { class: "mono", text: c.number }), h("td", { text: c.name }),
        h("td", {}, reviewBadge(c.review)), h("td", { class: "dim", text: c.review_note || "" })))))
      : h("div", { class: "empty-state" }, h("h2", { text: "Nothing waiting" }), h("p", { text: "Every card in this catalog is reviewed." })));
}

async function viewNoPictureList() {
  const { cards } = await GET(`/api/lists/no-picture?catalog=${enc(S.catalog)}`);
  return h("div", {},
    h("div", { class: "title" }, h("h2", { text: "Published without a picture" }), h("span", { class: "muted", text: plural(cards.length, "card") })),
    h("p", { class: "muted", text: "These cards went out marked as having no picture, so the app shows the card back. Give one a picture and publish its set again." }),
    cards.length ? h("table", { class: "list" }, h("thead", {}, h("tr", {}, ["Set", "No.", "Name"].map((t) => h("th", { text: t })))),
      h("tbody", {}, cards.map((c) => h("tr", { class: "click", onclick: () => { location.hash = `#/card/${enc(c.id)}`; } },
        h("td", { text: setName(c.set_id) }), h("td", { class: "mono", text: c.number }), h("td", { text: c.name })))))
      : h("div", { class: "empty-state" }, h("h2", { text: "None" }), h("p", { text: "Every published card has a picture." })));
}

// ============================================================================ start

const CLIENT = Math.random().toString(36).slice(2);
const hello = () => fetch(`/api/hello?c=${CLIENT}`).catch(() => {});
hello();
setInterval(hello, 30000);
window.addEventListener("pagehide", () => navigator.sendBeacon(`/api/bye?c=${CLIENT}`));

(async () => {
  try {
    await loadBoot();
  } catch (e) {
    fill($("#view"), h("div", { class: "box bad" }, h("h3", { text: "The catalog database could not be reached" }), h("p", { text: e.message })));
    return;
  }
  renderCatalogPicker();
  render();
})();
