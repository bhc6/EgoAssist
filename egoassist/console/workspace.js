/* Navigation keeps the media connection and background job polling alive. */
function showWorkspaceView() {
  const requested = location.hash.slice(2) || "conversation";
  const view = ["conversation", "reports", "tasks", "audit"].includes(requested) ? requested : "conversation";
  document.querySelectorAll("[data-view]").forEach(node => { node.hidden = node.dataset.view !== view; });
  document.querySelectorAll(".workspace-nav a").forEach(link => {
    if (link.hash === "#/" + view) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  });
}
addEventListener("hashchange", showWorkspaceView);
showWorkspaceView();

// Polling must not reset disclosure, scroll or keyboard focus while reading.
function updateDisclosureHTML(root, html, context = "") {
  if (root._renderHTML === html && root._renderContext === context) return;
  const sameContext = root._renderContext === context;
  const open = new Map([...root.querySelectorAll("details[data-disclosure]")].map(node => [node.dataset.disclosure, node.open]));
  const scroll = sameContext ? root.scrollTop : 0;
  const focused = root.contains(document.activeElement) ? document.activeElement.closest("[data-disclosure]")?.dataset.disclosure : null;
  root.innerHTML = html;
  root._renderHTML = html;
  root._renderContext = context;
  if (sameContext) root.querySelectorAll("details[data-disclosure]").forEach(node => {
    if (open.has(node.dataset.disclosure)) node.open = open.get(node.dataset.disclosure);
    if (focused === node.dataset.disclosure) node.querySelector("summary")?.focus({preventScroll: true});
  });
  root.scrollTop = scroll;
}
