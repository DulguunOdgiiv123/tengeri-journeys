// Live preview for the admin: the preview pane shows the real website with the unsaved changes.
// The site, opened as /?preview, sends { type: "gj-ready" } when it starts; we answer with
// { type: "gj-draft", file, data, focus } and it builds the page from that instead of the saved file.
// `focus` tells it what to open: the tour and day being edited, or the section a file belongs to.
// Each edit reloads the site frame after a short pause.
(() => {
  const { useEffect, useRef } = CMS.React;
  const ORIGIN = location.origin;
  const SECTION = { posts: "reviews", reviews: "reviews", faq: "faq", seasons: "seasons", settings: "contact" };

  const firstDiff = (a, b) => {
    for (let i = 0; i < Math.max(a.length, b.length); i++) if (JSON.stringify(a[i]) !== JSON.stringify(b[i])) return i;
    return -1;
  };

  // A photo picked but not saved yet only exists in the admin; hand the site its in-browser copy.
  const withUploads = (file, data, getAsset) => {
    const swap = v => { const a = v && getAsset ? getAsset(v) : null; return a && a.url && a.url.startsWith("blob:") ? a.url : v; };
    if (file === "tours") return { ...data, tours: (data.tours || []).map(t => ({ ...t, img: swap(t.img) })) };
    if (file === "seasons") return { ...data, seasons: (data.seasons || []).map(s => ({ ...s, img: swap(s.img) })) };
    return data;
  };

  const sitePreview = file => function SitePreview({ entry, getAsset }) {
    const frame = useRef(null), prev = useRef(null), focus = useRef(null), draft = useRef(null), timer = useRef(0), mounted = useRef(false), sent = useRef("");
    const data = entry.get("data").toJS();

    if (file === "tours") {
      // open the tour (and day) that just changed; keep showing it on later reloads
      if (prev.current) {
        const a = prev.current.tours || [], b = data.tours || [], i = firstDiff(a, b);
        if (i >= 0) {
          const j = firstDiff((a[i] || {}).schedule || [], (b[i] || {}).schedule || []);
          focus.current = { index: i, day: j >= 0 ? j : null };
        }
      }
    } else if (file === "policy") focus.current = { index: 0, sub: "m-s-incl" };
    else focus.current = { section: SECTION[file] };
    prev.current = data;
    draft.current = { type: "gj-draft", file, data: withUploads(file, data, getAsset), focus: focus.current };

    useEffect(() => {
      const answer = e => {
        if (e.origin !== ORIGIN || !e.data || e.data.type !== "gj-ready" || !frame.current || e.source !== frame.current.contentWindow) return;
        e.source.postMessage(draft.current, ORIGIN);
      };
      addEventListener("message", answer);
      return () => { removeEventListener("message", answer); clearTimeout(timer.current); };
    }, []);

    // Reload only when the content really changed: the admin also re-renders the preview for other reasons.
    const json = JSON.stringify(data);
    useEffect(() => {
      if (!mounted.current) { mounted.current = true; sent.current = json; return; }   // the first load asks for the draft by itself
      if (json === sent.current) return;
      sent.current = json;
      clearTimeout(timer.current);
      timer.current = setTimeout(() => { if (frame.current) frame.current.contentWindow.location.reload(); }, 700);
    });

    return h("iframe", { ref: frame, src: ORIGIN + "/?preview", title: "Сайтын харагдах байдал", style: { border: 0, width: "100%", height: "100vh", display: "block" } });
  };

  CMS.registerPreviewStyle("html, body { margin: 0; height: 100%; overflow: hidden; }", { raw: true });
  for (const f of ["tours", "posts", "faq", "policy", "seasons", "reviews", "settings"]) CMS.registerPreviewTemplate(f, sitePreview(f));
})();
