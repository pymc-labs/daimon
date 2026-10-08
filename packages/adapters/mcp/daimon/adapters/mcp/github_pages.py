"""GitHub connection pages with a compact, responsive picker."""

from __future__ import annotations

import html

from daimon.adapters.mcp.branded_pages import branded_page
from starlette.responses import HTMLResponse

_CSS = """
body:has(.gh-flow) { --stage:#f3f6f3; --stage-raised:#fff; --accent:#276b46;
  --accent-press:#1d5638; --text:#17241e; --border:#dce5df; --rose:#a0413e;
  --shadow-card:0 12px 36px rgba(23,47,30,.06); justify-content:flex-start;
  padding:30px 18px 80px; color:var(--text); }
.card:has(.gh-flow) { max-width:680px; overflow:visible; border:0; box-shadow:none;
  background:transparent; }
.card:has(.gh-flow--picker) { max-width:960px; }
.card:has(.gh-flow) .card-body { padding:0; }
.card:has(.gh-flow) .status-bar { display:none; }
.gh-flow button,.gh-flow input,.gh-flow select,.gh-flow textarea { font:inherit; }
.gh-flow { color:var(--text); border:1px solid var(--border); border-radius:16px;
  background:white; box-shadow:var(--shadow-card); overflow:visible; }
.gh-brand { display:flex; align-items:center; gap:11px; padding:17px 24px;
  border-bottom:1px solid var(--border); font-weight:700; }
.gh-mark { display:grid; place-items:center; width:34px; height:34px; border-radius:9px;
  background:#1b5e3a; color:#fff; font-size:19px; }
.gh-brand small { display:block; color:#5c6c62; font-size:12px; font-weight:500; line-height:1.2; }
.gh-content { padding:30px 32px; }
.gh-content h1 { font-family:inherit; font-size:clamp(26px,3vw,32px); line-height:1.18;
  letter-spacing:-.035em; margin:0 0 12px; color:var(--text); }
.gh-content p { color:#52645a; margin:0 0 13px; }
.gh-actions { display:flex; align-items:center; flex-wrap:wrap; gap:12px 20px; margin-top:24px; }
.gh-primary { display:inline-flex; align-items:center; justify-content:center;
  min-height:46px; padding:10px 18px; border:1px solid var(--accent); border-radius:9px;
  background:var(--accent); color:#fff; font-family:inherit; font-size:15px; font-weight:700;
  text-decoration:none; cursor:pointer; }
.gh-primary:hover:not(:disabled) { background:var(--accent-press); }
.gh-primary:disabled { opacity:.55; cursor:not-allowed; }
.gh-link,.gh-link-button { display:inline-flex; align-items:center; min-height:40px;
  color:var(--accent); background:none; border:0; font-family:inherit; font-size:14px;
  font-weight:600;
  text-decoration:none; cursor:pointer; }
.gh-link:hover,.gh-link-button:hover { text-decoration:underline; }
.gh-link-button[hidden] { display:none; }
.gh-primary:focus-visible,.gh-link:focus-visible,.gh-link-button:focus-visible,
.gh-search:focus-visible,.gh-choice input:focus-visible,.gh-access input:focus-visible {
  outline:3px solid #9cceb0; outline-offset:2px; }
.gh-context { display:flex; flex-wrap:wrap; gap:8px; margin:0 0 14px; }
.gh-context span { padding:5px 9px; border:1px solid var(--border); border-radius:7px;
  color:#53675a; background:#f5f8f5; font-size:13px; }
.gh-warning { padding:10px 13px; border:1px solid #ecdcae; border-radius:9px;
  background:#fff8e7; color:#664c1b!important; font-size:14px; }
.gh-picker-grid { display:grid; grid-template-columns:minmax(0,1fr) 265px; gap:24px; }
.gh-search-wrap { margin:12px 0 8px; }
.gh-search { flex:1; min-width:0; min-height:46px; padding:10px 13px; color:var(--text);
  background:white; border:1px solid #c7d4ca; border-radius:9px; font:inherit; }
.gh-search { width:100%; }
.gh-search::-webkit-search-cancel-button { cursor:pointer; }
.gh-flow--picker .gh-picker-grid { padding-bottom:120px; }
.gh-list-toolbar { display:flex; justify-content:space-between; align-items:center; gap:12px;
  min-height:32px; margin-bottom:7px; color:#5c6c62; font-size:13px; }
.gh-list-toolbar .gh-link-button { min-height:32px; font-size:13px; }
.gh-repo-list { border:1px solid var(--border); border-radius:10px; overflow:hidden; }
.gh-repo-owner[hidden] { display:none; }
.gh-repo-group { padding:9px 14px; color:#65786b; background:#f5f8f5;
  font-size:12px; font-weight:700; text-transform:uppercase; letter-spacing:.06em; }
.gh-repo-group[hidden] { display:none; }
.gh-choice { display:flex; align-items:center; gap:12px; min-height:48px; padding:10px 14px;
  border-top:1px solid var(--border); cursor:pointer; overflow-wrap:anywhere; }
.gh-choice[hidden] { display:none; }
.gh-choice:hover { background:#f7faf8; }
.gh-choice input { width:18px; height:18px; margin:0; flex:none; accent-color:var(--accent); }
.gh-repo-prefix { color:#819087; font-size:14px; }
.gh-repo-name { color:var(--text); font-size:14px; font-weight:600; }
.gh-empty { padding:22px 14px; font-size:14px; }
.gh-empty[hidden] { display:none; }
.gh-side { align-self:start; position:sticky; top:18px; }
.gh-side-card { padding:17px; border:1px solid var(--border); border-radius:10px;
  background:#f9fbf9; }
.gh-side-card + .gh-side-card { margin-top:13px; }
.gh-side-card h2 { margin:0 0 9px; font-family:inherit; font-size:14px;
  font-weight:700; color:var(--text); }
.gh-access label { display:block; padding:8px 0; color:var(--text);
  font-size:14px; cursor:pointer; }
.gh-access label + label { border-top:1px solid var(--border); }
.gh-access input { margin:0 8px 0 0; accent-color:var(--accent); }
.gh-access small { display:block; margin:4px 0 0 24px; color:#5c6c62; line-height:1.35; }
.gh-selection-count { color:var(--text)!important; font-size:18px; font-weight:700; }
.gh-side-card .gh-link-button { min-height:28px; }
.gh-finish { position:sticky; bottom:0; z-index:2; display:flex; justify-content:space-between;
  align-items:center; gap:12px; padding:12px 32px; border-top:1px solid var(--border);
  border-radius:0 0 16px 16px; background:rgba(255,255,255,.98);
  box-shadow:0 -5px 18px rgba(23,47,30,.07); }
.gh-finish p { margin:0; color:#5c6c62; font-size:14px; }
.gh-finish-status { display:flex; align-items:center; flex-wrap:wrap; gap:4px 10px; }
.gh-finish-status .gh-link-button { min-height:24px; font-size:13px; }
.gh-finish-actions { display:flex; align-items:center; gap:14px; }
.gh-finish .gh-link { color:#697b70; }
.gh-flow.is-connecting .gh-finish .gh-link { display:none; }
.gh-flow.is-connecting .gh-finish-status .gh-link-button { display:none; }
.gh-flow.is-connecting .gh-primary { opacity:.75; cursor:wait; }
.gh-flow.is-connecting .gh-picker-grid { opacity:.55; pointer-events:none; }
.gh-flow--quick .gh-access { max-width:430px; margin-top:18px; }
.gh-flow--quick .gh-access label { padding:9px 0; }
.gh-flow--quick .gh-finish { margin-top:24px; }
.gh-status-note { padding:12px 14px; border:1px solid var(--border); border-radius:9px;
  background:#f9fbf9; }
.gh-step-list { margin:12px 0 0; padding-left:20px; color:#52645a; }
.gh-step-list li { padding:4px 0; }
@media(max-width:800px) {
  body:has(.gh-flow) { padding:20px 14px 70px; }
  .gh-picker-grid { grid-template-columns:1fr; gap:16px; }
  .gh-side { position:static; display:grid; grid-template-columns:1fr 1fr; gap:12px; }
  .gh-side-card + .gh-side-card { margin-top:0; }
  .gh-content { padding:25px; }
  .gh-finish { padding:12px 25px; }
}
@media(max-width:520px) {
  body:has(.gh-flow) { padding:8px 8px 60px; }
  .gh-brand { padding:12px 17px; }
  .gh-content { padding:19px 17px; }
  .gh-content h1 { font-size:26px; }
  .gh-context { gap:5px; }
  .gh-context span { font-size:12px; }
  .gh-warning { font-size:13px; }
  .gh-side { display:block; }
  .gh-side-card + .gh-side-card { margin-top:12px; }
  .gh-list-toolbar .gh-link-button { font-size:12px; }
  .gh-finish { padding:10px 17px; align-items:stretch; flex-direction:column; gap:4px; }
  .gh-finish p { font-size:12px; }
  .gh-finish-actions { width:100%; justify-content:space-between; }
  .gh-finish .gh-primary { flex:1; }
  .gh-finish .gh-link { min-width:62px; justify-content:center; }
  .gh-actions { align-items:stretch; flex-direction:column; }
  .gh-actions .gh-primary { width:100%; }
  .gh-actions .gh-link { justify-content:center; }
}
"""


def github_page(
    *,
    title: str,
    body_html: str,
    status: int = 200,
    error: bool = False,
    kind: str = "status",
) -> HTMLResponse:
    """Render one server-generated GitHub page with Daimon branding."""
    return branded_page(
        title=title,
        state_bar=" status-bar--rose" if error else "",
        body_html=(
            f"<style>{_CSS}</style>"
            f'<div class="gh-flow gh-flow--{html.escape(kind, quote=True)}"><div class="gh-brand">'
            '<span class="gh-mark" aria-hidden="true">D</span>'
            "<span>Daimon<small>by PyMC Labs</small></span></div>"
            f'<main class="gh-content"><h1>{html.escape(title)}</h1>{body_html}</main></div>'
        ),
        status=status,
    )
