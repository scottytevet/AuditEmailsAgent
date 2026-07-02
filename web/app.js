import React, { useEffect, useMemo, useState } from "https://esm.sh/react@18.3.1";
import { createRoot } from "https://esm.sh/react-dom@18.3.1/client";

const e = React.createElement;

function App() {
  const [status, setStatus] = useState(null);
  const [query, setQuery] = useState("Ryan Hofmockel transcript");
  const [question, setQuestion] = useState("");
  const [filters, setFilters] = useState({
    source_type: "any",
    quality: "any",
    sender: "",
    participant: "",
    date_from: "",
    date_to: "",
    has_attachment: "any",
  });
  const [useAllSources, setUseAllSources] = useState(true);
  const [results, setResults] = useState([]);
  const [selected, setSelected] = useState(null);
  const [thread, setThread] = useState(null);
  const [answer, setAnswer] = useState(null);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [me, setMe] = useState(null);

  useEffect(() => {
    loadMe();
    loadStatus();
  }, []);

  const isIndexed = status?.counts?.emails > 0;
  const hasSourceInputs = Boolean(status?.inputs?.normalized_emails_exists || status?.inputs?.graph_source_records_exists);
  const filterQuery = useMemo(() => cleanFilters(filters), [filters]);
  const sourceCatalog = status?.source_catalog || [];
  const sourceMeta = useMemo(
    () => Object.fromEntries(sourceCatalog.map((item) => [item.kind, item])),
    [sourceCatalog]
  );
  const sourceTypeOptions = useMemo(() => [
    { value: "any", label: "All sources" },
    ...sourceCatalog
      .filter((item) => item.count > 0)
      .map((item) => ({ value: item.kind, label: `${item.label} (${item.count})` })),
  ], [sourceCatalog]);
  const qualityOptions = useMemo(() => [
    { value: "any", label: "Any quality" },
    ...Object.keys(status?.quality_counts || {})
      .sort()
      .map((quality) => ({ value: quality, label: qualityLabel(quality) })),
  ], [status]);

  async function loadMe() {
    try {
      const data = await api("/api/me");
      setMe(data);
    } catch (err) {
      setMe({ auth_enabled: true, authenticated: false, error: err.message });
    }
  }

  async function loadStatus() {
    setError("");
    const data = await api("/api/status");
    setStatus(data);
  }

  async function rebuild(withEmbeddings = false) {
    setBusy(withEmbeddings ? "Indexing and embedding sources..." : "Indexing sources...");
    setError("");
    try {
      const data = await api("/api/rebuild", {
        method: "POST",
        body: JSON.stringify({ with_embeddings: withEmbeddings }),
      });
      setStatus(data);
      await search();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy("");
    }
  }

  async function buildEmbeddings() {
    setBusy("Building embeddings...");
    setError("");
    try {
      const data = await api("/api/embeddings/build", {
        method: "POST",
        body: JSON.stringify({}),
      });
      await loadStatus();
      setAnswer({
        answer: `Embedded ${data.chunks_embedded} chunks with ${data.model}.`,
        citations: [],
      });
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy("");
    }
  }

  async function search(event) {
    event?.preventDefault();
    setBusy("Searching sources...");
    setError("");
    try {
      const params = new URLSearchParams({ q: query, limit: "30", ...filterQuery });
      const data = await api(`/api/search?${params.toString()}`);
      setResults(data.results || []);
      if (!selected && data.results?.length) {
        await openSource(data.results[0].id);
      }
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy("");
    }
  }

  async function ask(event) {
    event?.preventDefault();
    setBusy("Answering from evidence...");
    setError("");
    try {
      const scopedFilters = useAllSources ? answerFilters(filterQuery) : filterQuery;
      const data = await api("/api/ask", {
        method: "POST",
        body: JSON.stringify({
          question,
          filters: scopedFilters,
          limit: 10,
          use_all_sources: useAllSources,
          review: true,
        }),
      });
      setAnswer(data);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy("");
    }
  }

  async function openSource(id) {
    setBusy("Opening source...");
    setError("");
    try {
      const data = await api(`/api/sources/${id}`);
      setSelected(data);
      setThread(null);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy("");
    }
  }

  async function openThread(threadId) {
    if (!threadId) return;
    setBusy("Opening group...");
    setError("");
    try {
      const data = await api(`/api/threads/${encodeURIComponent(threadId)}`);
      setThread(data);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy("");
    }
  }

  return e(
    "main",
    { className: "shell" },
    e("section", { className: "topbar" },
      e("div", { className: "productTitle" },
        e("h1", null, status?.app?.title || "Source Evidence QA"),
        e("p", null, status?.app?.subtitle || "Ask questions across indexed source records.")
      ),
      e("div", { className: "topbarRight" },
        e("div", { className: "statusStrip" }, ...statusPills(status, sourceCatalog)),
        authSession(me)
      )
    ),
    e("section", { className: "noticeRow" },
      statusNotice(status, isIndexed, hasSourceInputs)
    ),
    error ? e("div", { className: "error" }, error) : null,
    busy ? e("div", { className: "busy" }, busy) : null,
    e("section", { className: "workspace" },
      e("aside", { className: "filters" },
        e("h2", null, "Evidence Filters"),
        filterSelect("Source Type", "source_type", sourceTypeOptions, filters, setFilters),
        filterSelect("Quality", "quality", qualityOptions, filters, setFilters),
        filterInput("Owner / Sender", "sender", filters, setFilters),
        filterInput("Person / Entity", "participant", filters, setFilters),
        filterInput("From", "date_from", filters, setFilters, "date"),
        filterInput("To", "date_to", filters, setFilters, "date"),
        filterSelect("Email Attachment", "has_attachment", [
          { value: "any", label: "Any" },
          { value: "true", label: "Yes" },
          { value: "false", label: "No" },
        ], filters, setFilters),
        e("div", { className: "configBox" },
          e("strong", null, "Runtime"),
          e("span", null, status?.llm_configured ? `${status?.model_provider_label || "Model provider"} configured` : `${status?.model_provider_label || "Model provider"} missing config`),
          e("span", null, `Embedding: ${status?.embedding_model || ""}`),
          e("span", null, `Answer: ${status?.answer_model || ""}`)
        ),
        maintenancePanel(status, Boolean(busy), hasSourceInputs, isIndexed, rebuild, buildEmbeddings)
      ),
      e("section", { className: "mainColumn" },
        e("form", { className: "askBar", onSubmit: ask },
          e("input", {
            value: question,
            onChange: (event) => setQuestion(event.target.value),
            placeholder: "Ask across indexed sources",
          }),
          e("label", { className: "askToggle", title: "Use all source types for answers" },
            e("input", {
              type: "checkbox",
              checked: useAllSources,
              onChange: (event) => setUseAllSources(event.target.checked),
            }),
            e("span", null, "All sources")
          ),
          e("button", { disabled: Boolean(busy) || !isIndexed }, "Ask")
        ),
        answer ? answerPanel(answer, sourceMeta) : null,
        e("form", { className: "searchBar", onSubmit: search },
          e("input", {
            value: query,
            onChange: (event) => setQuery(event.target.value),
            placeholder: "Search names, IDs, topics, or source text",
          }),
          e("button", { disabled: Boolean(busy) || !isIndexed }, "Search")
        ),
        e("div", { className: "contentGrid" },
          e("div", { className: "results" },
            e("h2", null, "Source Results"),
            results.length
              ? results.map((item) => resultCard(item, selected?.id === item.id, openSource, openThread, sourceMeta))
              : e("p", { className: "muted" }, isIndexed ? "Search to see matching source records." : "Build the index after source files exist.")
          ),
          e("div", { className: "viewer" },
            selected ? sourceViewer(selected, thread, openThread, openSource, sourceMeta) : e("p", { className: "muted" }, "Select a source record to inspect the evidence.")
          )
        )
      )
    )
  );
}

function authSession(me) {
  if (!me?.auth_enabled) return null;
  if (!me.authenticated) {
    return e("a", { className: "authLink", href: "/auth/login" }, "Sign in");
  }
  const label = me.user?.email || me.user?.name || "Signed in";
  return e("div", { className: "authSession" },
    e("span", { title: label }, label),
    e("a", { href: "/auth/logout" }, "Logout")
  );
}
function statusPill(label, value) {
  return e("div", { className: "pill", key: label }, e("span", null, label), e("strong", null, value));
}

function statusPills(status, sourceCatalog) {
  const pills = [statusPill("Sources", status?.counts?.emails ?? 0)];
  for (const source of (sourceCatalog || []).filter((item) => item.count > 0).slice(0, 2)) {
    pills.push(statusPill(source.label, source.count));
  }
  pills.push(statusPill("Embedded", status?.embedded_chunks ?? 0));
  return pills;
}

function statusNotice(status, indexed, hasSourceInputs) {
  if (!status) return e("p", { className: "muted" }, "Checking local database...");
  if (!hasSourceInputs) {
    return e("p", { className: "notice" }, "Run at least one importer to create source JSONL files.");
  }
  if (!indexed) {
    return e("p", { className: "notice" }, "Source files found. Open Maintenance to build the index.");
  }
  if (!status.llm_configured) {
    return e("p", { className: "notice" }, `Source search is ready. Add ${status.model_provider_label || "model provider"} settings in .env for generated answers and embeddings.`);
  }
  return e("p", { className: "notice good" }, "Search and QA are ready.");
}

function maintenancePanel(status, busy, hasSourceInputs, isIndexed, rebuild, buildEmbeddings) {
  return e("details", { className: "maintenancePanel" },
    e("summary", null, "Maintenance"),
    e("div", { className: "maintenanceContent" },
      e("span", null, `Index: ${isIndexed ? "Ready" : "Not built"}`),
      e("span", null, `Embeddings: ${status?.embedded_chunks ?? 0} chunks`),
      e("div", { className: "maintenanceActions" },
        e("button", { onClick: () => rebuild(false), disabled: busy || !hasSourceInputs }, "Rebuild Index"),
        e("button", { onClick: buildEmbeddings, disabled: busy || !isIndexed }, "Build Embeddings")
      )
    )
  );
}
function filterInput(label, key, filters, setFilters, type = "text") {
  return e("label", { key }, label,
    e("input", {
      type,
      value: filters[key],
      onChange: (event) => setFilters({ ...filters, [key]: event.target.value }),
    })
  );
}

function filterSelect(label, key, options, filters, setFilters) {
  return e("label", { key }, label,
    e("select", {
      value: filters[key],
      onChange: (event) => setFilters({ ...filters, [key]: event.target.value }),
    },
      options.map((option) => e("option", { key: option.value, value: option.value }, option.label))
    )
  );
}

function answerPanel(answer, sourceMeta) {
  return e("section", { className: "answer" },
    e("h2", null, "Evidence Answer"),
    e("p", null, answer.answer),
    answer.review ? reviewPanel(answer.review, sourceMeta) : null,
    answer.citations?.length
      ? e("div", { className: "citations" },
          answer.citations.map((cite) =>
            e("article", { className: "citation", key: cite.number },
              e("div", { className: "sourceTags" },
                badge(sourceLabel(cite.source_kind, sourceMeta), sourceClass(cite.source_kind)),
                cite.quality ? badge(qualityLabel(cite.quality), qualityClass(cite.quality)) : null,
                badge(cite.retrieval || "retrieval", "retrieval")
              ),
              e("strong", null, `[${cite.number}] ${cite.subject || "(no subject)"}`),
              e("span", null, `${cite.source_origin || cite.sender || "unknown"} | ${formatDate(cite.sent_at)}`),
              e("p", null, cite.snippet)
            )
          )
        )
      : null
  );
}

function reviewPanel(review, sourceMeta) {
  const score = typeof review.score === "number" ? ` | ${Math.round(review.score * 100)}%` : "";
  const sourceMix = formatSourceMix(review.source_mix || {}, sourceMeta);
  const weakClaims = review.weak_claims || [];
  return e("div", { className: `reviewPanel ${review.status || ""}` },
    e("div", { className: "reviewLine" },
      e("strong", null, `Review: ${humanize(review.status || "unknown")}${score}`),
      sourceMix ? e("span", null, `Sources: ${sourceMix}`) : null
    ),
    review.notes?.length ? e("p", null, review.notes.join(" ")) : null,
    weakClaims.length
      ? e("ul", null, weakClaims.map((item, index) => e("li", { key: index }, item.claim)))
      : null
  );
}

function formatSourceMix(mix, sourceMeta) {
  return Object.entries(mix)
    .map(([kind, count]) => `${sourceLabel(kind, sourceMeta)} ${count}`)
    .join(", ");
}
function resultCard(item, active, openSource, openThread, sourceMeta) {
  return e("article", { className: `result ${active ? "active" : ""}`, key: item.id },
    e("button", { className: "resultMain", onClick: () => openSource(item.id) },
      e("div", { className: "sourceTags" },
        badge(sourceLabel(item.source_kind, sourceMeta), sourceClass(item.source_kind)),
        item.quality ? badge(qualityLabel(item.quality), qualityClass(item.quality)) : null
      ),
      e("strong", null, item.subject || "(no subject)"),
      e("span", null, `${item.source_origin || item.sender || "unknown source"} | ${formatDate(item.sent_at)}`),
      e("p", null, item.snippet || "No source text extracted.")
    ),
    item.thread_id
      ? e("button", { className: "threadButton", onClick: () => openThread(item.thread_id) }, "Group")
      : null
  );
}

function sourceViewer(source, thread, openThread, openSource, sourceMeta) {
  const kind = source.parse?.source_kind || source.source_kind || "email";
  const quality = source.parse?.quality || source.quality;
  return e("div", null,
    e("div", { className: "sourceHeader" },
      e("div", { className: "sourceTags" },
        badge(sourceLabel(kind, sourceMeta), sourceClass(kind)),
        quality ? badge(qualityLabel(quality), qualityClass(quality)) : null
      ),
      e("h2", null, source.subject || "(no subject)"),
      e("div", { className: "metaGrid" },
        metaItem("Owner / Sender", source.sender || "unknown"),
        metaItem("Date", formatDate(source.sent_at)),
        metaItem("Source Origin", source.source_origin || source.parse?.source_project || source.parse?.source_system || ""),
        metaItem("Source Path", source.relative_path || source.source_path || ""),
        metaItem("Record ID", source.internet_message_id || String(source.id || ""))
      ),
      source.thread_id ? e("button", { onClick: () => openThread(source.thread_id) }, "Open Group") : null
    ),
    thread ? threadPanel(thread, openSource) : null,
    source.parse ? metadataPanel(source.parse) : null,
    e("pre", { className: "sourceBody" }, source.body_text || "No source text extracted.")
  );
}

function metadataPanel(parse) {
  const entries = Object.entries(parse).filter(([key, value]) => value !== null && value !== "" && key !== "status");
  if (!entries.length) return null;
  return e("details", { className: "metadataPanel" },
    e("summary", null, "Source Metadata"),
    e("dl", null,
      entries.map(([key, value]) =>
        e(React.Fragment, { key },
          e("dt", null, humanize(key)),
          e("dd", null, typeof value === "object" ? JSON.stringify(value) : String(value))
        )
      )
    )
  );
}

function threadPanel(thread, openSource) {
  return e("section", { className: "threadPanel" },
    e("h3", null, `Group: ${thread.thread?.display_subject || thread.thread?.thread_id}`),
    e("div", { className: "timeline" },
      thread.emails.map((source) =>
        e("button", { key: source.id, onClick: () => openSource(source.id) },
          e("strong", null, formatDate(source.sent_at)),
          e("span", null, source.sender || "unknown"),
          e("small", null, source.subject || "(no subject)")
        )
      )
    )
  );
}

function metaItem(label, value) {
  return e("div", { className: "metaItem" }, e("span", null, label), e("strong", null, value || "unknown"));
}

function badge(label, className) {
  if (!label) return null;
  return e("span", { className: `tag ${className || ""}` }, label);
}

function sourceLabel(kind, sourceMeta = {}) {
  const source = sourceMeta[kind || "email"];
  return source?.label || humanize(kind || "source");
}

function qualityLabel(quality) {
  return humanize(quality || "");
}

function sourceClass(kind) {
  return `source-kind source-${(kind || "email").replace(/[^a-z0-9]+/gi, "-").toLowerCase()}`;
}

function qualityClass(quality) {
  return `quality-${(quality || "unknown").replace(/[^a-z0-9]+/gi, "-").toLowerCase()}`;
}

function humanize(value) {
  return String(value || "")
    .replace(/[_-]+/g, " ")
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function cleanFilters(filters) {
  const clean = {};
  for (const [key, value] of Object.entries(filters)) {
    if (value && value !== "any") clean[key] = value;
  }
  return clean;
}

function answerFilters(filters) {
  const clean = { ...filters };
  delete clean.source_type;
  delete clean.quality;
  delete clean.has_attachment;
  return clean;
}
async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  const text = await response.text();
  let data = {};
  try {
    data = text ? JSON.parse(text) : {};
  } catch {
    data = { error: text || `Request failed: ${response.status}` };
  }
  if (response.status === 401 && data.login_url) {
    window.location.href = data.login_url;
  }
  if (!response.ok || data.error) {
    throw new Error(data.error || `Request failed: ${response.status}`);
  }
  return data;
}

function formatDate(value) {
  if (!value) return "unknown date";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleString();
}

createRoot(document.getElementById("root")).render(e(App));
