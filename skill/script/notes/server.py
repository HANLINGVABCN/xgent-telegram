#!/usr/bin/env python3
import argparse
import base64
import hashlib
import hmac
import http.cookies
import http.server
import json
import os
import secrets
import socketserver
import time
import urllib.parse
from pathlib import Path


INDEX_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>便签</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f4f5f8;
      --bg-grad: radial-gradient(1200px 600px at 80% -10%, #eef2ff 0%, transparent 60%), radial-gradient(900px 500px at -10% 110%, #ecfeff 0%, transparent 55%);
      --panel: #ffffff;
      --text: #1f2937;
      --muted: #6b7280;
      --faint: #9aa3b2;
      --line: #e5e7eb;
      --line-strong: #d1d5db;
      --brand: #4f46e5;
      --brand-soft: #eef2ff;
      --brand-ink: #4338ca;
      --danger: #dc2626;
      --danger-soft: #fef2f2;
      --ok: #059669;
      --shadow: 0 1px 2px rgba(15,23,42,.04), 0 8px 24px rgba(15,23,42,.06);
      --shadow-lg: 0 10px 40px rgba(15,23,42,.12);
      --radius: 14px;
    }
    * { box-sizing: border-box; }
    html, body { height: 100%; }
    body {
      margin: 0;
      background: var(--bg);
      background-image: var(--bg-grad);
      background-attachment: fixed;
      color: var(--text);
      font: 15px/1.6 system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
      -webkit-font-smoothing: antialiased;
    }
    button, input, textarea { font: inherit; color: inherit; }
    a { color: inherit; text-decoration: none; }

    /* ===== 通用控件 ===== */
    .btn {
      display: inline-flex; align-items: center; justify-content: center; gap: 6px;
      height: 38px; padding: 0 14px; border-radius: 10px;
      border: 1px solid var(--line-strong); background: #fff; color: var(--text);
      cursor: pointer; transition: all .15s ease; white-space: nowrap;
    }
    .btn:hover { border-color: var(--brand); color: var(--brand-ink); }
    .btn.primary { background: var(--brand); border-color: var(--brand); color: #fff; box-shadow: 0 6px 16px rgba(79,70,229,.28); }
    .btn.primary:hover { background: var(--brand-ink); color: #fff; }
    .btn.danger { color: var(--danger); border-color: #fecaca; background: var(--danger-soft); }
    .btn.danger:hover { background: var(--danger); color: #fff; border-color: var(--danger); }
    .btn.ghost { background: transparent; border-color: transparent; }
    .btn.ghost:hover { background: var(--brand-soft); color: var(--brand-ink); }
    .btn.icon { width: 38px; padding: 0; }
    .btn:disabled { opacity: .5; cursor: not-allowed; }

    .field {
      width: 100%; height: 42px; padding: 0 14px;
      border: 1px solid var(--line-strong); border-radius: 10px;
      background: #fff; outline: none; transition: all .15s ease;
    }
    .field:focus { border-color: var(--brand); box-shadow: 0 0 0 4px rgba(79,70,229,.14); }

    /* ===== 登录页 ===== */
    .login-wrap { min-height: 100vh; display: grid; place-items: center; padding: 24px; }
    .login-card {
      width: min(380px, 100%); background: var(--panel);
      border: 1px solid var(--line); border-radius: 18px; padding: 30px 28px;
      box-shadow: var(--shadow-lg);
    }
    .login-card h1 { margin: 0 0 4px; font-size: 22px; letter-spacing: .5px; }
    .login-card .sub { margin: 0 0 22px; color: var(--muted); font-size: 13px; }
    .login-card label { display: block; margin: 14px 0 6px; color: var(--muted); font-size: 13px; }
    .login-card .field { height: 44px; }
    .login-card .btn.primary { width: 100%; height: 44px; margin-top: 22px; }
    .login-card .status { margin: 12px 0 0; min-height: 18px; color: var(--danger); font-size: 13px; }

    /* ===== 顶栏 ===== */
    .topbar {
      position: sticky; top: 0; z-index: 20;
      background: rgba(255,255,255,.82); backdrop-filter: saturate(180%) blur(12px);
      border-bottom: 1px solid var(--line);
    }
    .topbar-inner {
      max-width: 880px; margin: 0 auto; padding: 12px 18px;
      display: flex; align-items: center; gap: 12px;
    }
    .brand { font-size: 18px; font-weight: 750; letter-spacing: .5px; display: inline-flex; align-items: center; gap: 8px; }
    .brand .dot { width: 10px; height: 10px; border-radius: 50%; background: linear-gradient(135deg, var(--brand), #06b6d4); }
    .spacer { flex: 1; }

    /* ===== 容器 ===== */
    .container { max-width: 880px; margin: 0 auto; padding: 22px 18px 80px; }

    /* ===== 列表页 ===== */
    .search-row { display: flex; gap: 10px; align-items: center; margin-bottom: 18px; }
    .search-box { position: relative; flex: 1; }
    .search-box .field { padding-left: 40px; height: 44px; }
    .search-box .ico { position: absolute; left: 12px; top: 50%; transform: translateY(-50%); color: var(--faint); pointer-events: none; }
    .search-box .clear { position: absolute; right: 8px; top: 50%; transform: translateY(-50%); }
    .meta-line { color: var(--muted); font-size: 13px; margin: 4px 4px 14px; min-height: 18px; }
    .meta-line b { color: var(--text); }

    .list { display: grid; gap: 12px; }
    .card {
      display: block; width: 100%; text-align: left;
      background: var(--panel); border: 1px solid var(--line); border-radius: var(--radius);
      padding: 16px 18px; cursor: pointer; transition: all .18s ease; box-shadow: var(--shadow);
    }
    .card:hover { transform: translateY(-2px); border-color: #c7d2fe; box-shadow: var(--shadow-lg); }
    .card .c-title {
      font-size: 17px; font-weight: 700; line-height: 1.4;
      color: var(--text); margin: 0 0 6px;
      overflow: hidden; text-overflow: ellipsis; display: -webkit-box;
      -webkit-line-clamp: 1; -webkit-box-orient: vertical;
    }
    .card .c-preview {
      color: var(--muted); font-size: 13px; line-height: 1.5; margin: 0;
      overflow: hidden; text-overflow: ellipsis; display: -webkit-box;
      -webkit-line-clamp: 2; -webkit-box-orient: vertical;
    }
    .card .c-foot { margin-top: 10px; color: var(--faint); font-size: 12px; display: flex; gap: 10px; align-items: center; }
    .empty-state {
      margin-top: 8vh; text-align: center; color: var(--muted);
    }
    .empty-state .big { font-size: 46px; opacity: .5; margin-bottom: 10px; }
    .empty-state .t { font-size: 16px; margin-bottom: 4px; color: var(--text); font-weight: 600; }

    mark { background: #fde68a; color: inherit; border-radius: 3px; padding: 0 2px; }

    /* ===== 详情页 ===== */
    .detail-head { display: flex; align-items: center; gap: 10px; margin-bottom: 18px; }
    .detail-card {
      background: var(--panel); border: 1px solid var(--line); border-radius: 18px;
      padding: 28px 30px; box-shadow: var(--shadow);
    }
    .d-title {
      font-size: 26px; font-weight: 800; line-height: 1.3; letter-spacing: .3px;
      color: var(--text); margin: 0 0 18px; word-break: break-word; white-space: pre-wrap;
    }
    .d-title.muted { color: var(--faint); font-weight: 600; }
    .d-body {
      white-space: pre-wrap; word-break: break-word; color: #374151;
      font-size: 15px; line-height: 1.8; margin: 0;
    }
    .d-body.muted { color: var(--faint); }
    .editor-area {
      width: 100%; min-height: 56vh; resize: vertical;
      border: 1px solid var(--line-strong); border-radius: 12px; padding: 16px 18px;
      background: #fff; outline: none; line-height: 1.8; transition: all .15s ease;
    }
    .editor-area:focus { border-color: var(--brand); box-shadow: 0 0 0 4px rgba(79,70,229,.14); }
    .title-input {
      width: 100%; display: block; border: none; border-bottom: 1px dashed var(--line);
      background: transparent; outline: none; padding: 4px 0 16px; margin: 0 0 4px;
      font-size: 26px; font-weight: 800; line-height: 1.3; letter-spacing: .3px; color: var(--text);
      transition: border-color .15s ease;
    }
    .title-input::placeholder { color: #c7ccd6; font-weight: 800; }
    .title-input:focus { border-bottom-color: var(--brand); }
    .detail-card .editor-area { margin-top: 14px; }
    .editor-hint { color: var(--faint); font-size: 12px; margin: 8px 2px 16px; }
    .detail-foot { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-top: 18px; flex-wrap: wrap; }
    .detail-foot .time { color: var(--faint); font-size: 12px; }
    .detail-actions { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
    .detail-foot .status { font-size: 12px; color: var(--faint); min-width: 70px; }
    .detail-foot .status.saving { color: var(--brand); }
    .detail-foot .status.saved { color: var(--ok); }
    .detail-foot .status.unsaved { color: var(--muted); }
    .detail-foot .status.error { color: var(--danger); }
    .detail-foot .sep { width: 1px; height: 22px; background: var(--line); margin: 0 4px; }

    .back-btn { display: inline-flex; align-items: center; gap: 6px; color: var(--muted); }
    .back-btn:hover { color: var(--brand-ink); }

    @media (max-width: 600px) {
      .container { padding: 16px 14px 70px; }
      .topbar-inner { padding: 10px 14px; }
      .detail-card { padding: 20px 18px; }
      .d-title { font-size: 22px; }
      .search-row .btn span.label { display: none; }
    }
  </style>
</head>
<body>
  <div id="root"></div>
  <script>
    const root = document.getElementById('root');
    const state = { notes: [], current: null, query: '', loading: true };
    const editState = { history: [], index: 0, saveTimer: null, saving: false, pending: false, hasId: false };

    /* ---------- API ---------- */
    async function api(path, options = {}) {
      const res = await fetch(path, {
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
        ...options
      });
      if (res.status === 401) { renderLogin(); throw new Error('unauthorized'); }
      const text = await res.text();
      const data = text ? JSON.parse(text) : {};
      if (!res.ok) throw new Error(data.error || '请求失败');
      return data;
    }

    /* ---------- 工具 ---------- */
    function escapeHtml(s) {
      return String(s ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
    }
    function escapeAttr(s) { return escapeHtml(s).replace(/`/g, '&#96;'); }
    function formatTime(ts) { return ts ? new Date(ts * 1000).toLocaleString('zh-CN', { hour12: false }) : ''; }
    function firstLine(content) {
      const c = String(content ?? '');
      const idx = c.indexOf('\n');
      return idx === -1 ? c : c.slice(0, idx);
    }
    function restLines(content) {
      const c = String(content ?? '');
      const idx = c.indexOf('\n');
      return idx === -1 ? '' : c.slice(idx + 1);
    }
    function searchTerms(q) {
      return String(q || '').trim().toLowerCase().split(/\s+/).filter(Boolean);
    }
    function noteMatches(note, terms) {
      if (!terms.length) return true;
      const text = String(note.content || '').toLowerCase();
      return terms.every(t => text.includes(t));
    }
    function highlight(text, terms) {
      const esc = escapeHtml(text);
      if (!terms.length) return esc;
      const pattern = terms.map(t => t.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|');
      const re = new RegExp('(' + pattern + ')', 'gi');
      return esc.replace(re, '<mark>$1</mark>');
    }

    /* ---------- 路由 ---------- */
    function parseHash() {
      const h = location.hash.replace(/^#/, '');
      if (h === '/new') return { name: 'new' };
      const m = h.match(/^\/n\/(.+)$/);
      if (m) return { name: 'note', id: decodeURIComponent(m[1]) };
      return { name: 'list' };
    }
    function go(hash) { location.hash = hash; }

    /* ---------- 登录页 ---------- */
    function renderLogin(message = '') {
      root.innerHTML = `<div class="login-wrap"><div class="login-card">
        <h1>便签</h1>
        <p class="sub">私有网页便签 · 登录</p>
        <form id="loginForm">
          <label>账号</label>
          <input class="field" name="username" autocomplete="username" required>
          <label>密码</label>
          <input class="field" name="password" type="password" autocomplete="current-password" required>
          <p class="status">${escapeHtml(message)}</p>
          <button class="btn primary" type="submit">登录</button>
        </form>
      </div></div>`;
      document.getElementById('loginForm').onsubmit = async (e) => {
        e.preventDefault();
        const fd = new FormData(e.target);
        try {
          await api('/api/login', { method: 'POST', body: JSON.stringify({ username: fd.get('username'), password: fd.get('password') }) });
          await loadNotes();
          render();
        } catch (err) {
          renderLogin(err.message);
        }
      };
    }

    /* ---------- 顶栏 ---------- */
    function topbar(rightHtml) {
      return `<div class="topbar"><div class="topbar-inner">
        <span class="brand"><span class="dot"></span>便签</span>
        <span class="spacer"></span>
        ${rightHtml || ''}
      </div></div>`;
    }

    /* ---------- 列表页 ---------- */
    function renderList() {
      const right = `<button class="btn ghost" id="logoutBtn">退出登录</button>
        <button class="btn primary" id="newBtn">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M12 5v14M5 12h14"/></svg>
          <span class="label">新建</span>
        </button>`;
      root.innerHTML = topbar(right) + `<div class="container">
        <div class="search-row">
          <div class="search-box">
            <span class="ico"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/></svg></span>
            <input class="field" id="search" placeholder="搜索标题或正文（多个关键词用空格分隔）" value="${escapeAttr(state.query)}" autocomplete="off">
            <button class="btn ghost icon clear" id="clearBtn" title="清除" style="${state.query ? '' : 'display:none'}"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M18 6 6 18M6 6l12 12"/></svg></button>
          </div>
        </div>
        <div class="meta-line" id="metaLine"></div>
        <div id="listArea"></div>
      </div>`;

      document.getElementById('newBtn').onclick = () => go('/new');
      document.getElementById('logoutBtn').onclick = async () => { await api('/api/logout', { method: 'POST' }); renderLogin(); };
      const searchInput = document.getElementById('search');
      const clearBtn = document.getElementById('clearBtn');
      searchInput.oninput = () => {
        state.query = searchInput.value;
        clearBtn.style.display = state.query ? '' : 'none';
        updateListArea();
      };
      clearBtn.onclick = () => {
        state.query = '';
        searchInput.value = '';
        clearBtn.style.display = 'none';
        updateListArea();
        searchInput.focus();
      };
      updateListArea();
    }

    function updateListArea() {
      const terms = searchTerms(state.query);
      const filtered = state.notes.filter(n => noteMatches(n, terms));
      const metaLine = document.getElementById('metaLine');
      if (metaLine) {
        metaLine.innerHTML = state.query
          ? `找到 <b>${filtered.length}</b> 条结果（共 ${state.notes.length} 条）`
          : `共 <b>${state.notes.length}</b> 条便签`;
      }
      const listArea = document.getElementById('listArea');
      if (!listArea) return;
      if (!filtered.length) {
        const empty = state.query
          ? { big: '🔍', t: '没有匹配的便签', s: '换个关键词试试' }
          : { big: '📝', t: '还没有便签', s: '点击右上角“新建”开始记录' };
        listArea.innerHTML = `<div class="empty-state"><div class="big">${empty.big}</div><div class="t">${empty.t}</div><div>${empty.s}</div></div>`;
        return;
      }
      listArea.innerHTML = `<div class="list">` + filtered.map(n => {
        const title = firstLine(n.content);
        const titleHtml = title ? highlight(title, terms) : '<span style="color:var(--faint)">无标题</span>';
        const preview = restLines(n.content).replace(/\s+/g, ' ').trim();
        return `<button class="card" data-id="${escapeAttr(n.id)}">
          <div class="c-title">${titleHtml}</div>
          ${preview ? `<div class="c-preview">${highlight(preview.slice(0, 160), terms)}</div>` : ''}
          <div class="c-foot"><span>${formatTime(n.updated_at)}</span></div>
        </button>`;
      }).join('') + `</div>`;
      listArea.querySelectorAll('.card').forEach(btn => btn.onclick = () => go('/n/' + encodeURIComponent(btn.dataset.id)));
    }

    /* ---------- 详情/编辑页 ---------- */
    function splitContent(content) {
      const c = String(content ?? '');
      const idx = c.indexOf('\n');
      return idx === -1 ? { title: c, body: '' } : { title: c.slice(0, idx), body: c.slice(idx + 1) };
    }

    function currentSnapshot() {
      return { title: document.getElementById('title').value, body: document.getElementById('content').value };
    }
    function applySnapshot(snap) {
      document.getElementById('title').value = snap.title;
      document.getElementById('content').value = snap.body;
    }
    function recordSnapshot() {
      const snap = currentSnapshot();
      const top = editState.history[editState.index];
      if (top && top.title === snap.title && top.body === snap.body) return;
      editState.history = editState.history.slice(0, editState.index + 1);
      editState.history.push(snap);
      editState.index = editState.history.length - 1;
      updateUndoRedo();
    }
    function updateUndoRedo() {
      const u = document.getElementById('undoBtn'), r = document.getElementById('redoBtn');
      if (u) u.disabled = editState.index <= 0;
      if (r) r.disabled = editState.index >= editState.history.length - 1;
    }
    function setStatus(text, cls) {
      const el = document.getElementById('status');
      if (!el) return;
      el.textContent = text;
      el.className = 'status' + (cls ? ' ' + cls : '');
    }

    function onEdit() {
      setStatus('编辑中…', 'unsaved');
      clearTimeout(editState.saveTimer);
      editState.saveTimer = setTimeout(() => {
        recordSnapshot();
        autoSave();
      }, 800);
    }
    function onKey(e) {
      const k = e.key.toLowerCase();
      if ((e.ctrlKey || e.metaKey) && k === 's') { e.preventDefault(); flushSave(); }
      else if ((e.ctrlKey || e.metaKey) && !e.shiftKey && k === 'z') { e.preventDefault(); undo(); }
      else if ((e.ctrlKey || e.metaKey) && (k === 'y' || (e.shiftKey && k === 'z'))) { e.preventDefault(); redo(); }
    }
    function undo() {
      if (editState.index <= 0) return;
      editState.index--;
      applySnapshot(editState.history[editState.index]);
      updateUndoRedo();
      flushSave();
    }
    function redo() {
      if (editState.index >= editState.history.length - 1) return;
      editState.index++;
      applySnapshot(editState.history[editState.index]);
      updateUndoRedo();
      flushSave();
    }
    function flushSave() {
      clearTimeout(editState.saveTimer);
      autoSave();
    }
    async function autoSave() {
      if (editState.saving) { editState.pending = true; return; }
      const snap = currentSnapshot();
      const content = snap.title + '\n' + snap.body;
      // 新便签且内容为空时不创建
      if (!editState.hasId && !snap.title && !snap.body) return;
      editState.saving = true;
      setStatus('保存中…', 'saving');
      try {
        let saved;
        if (editState.hasId) {
          saved = await api('/api/notes/' + encodeURIComponent(state.current.id), { method: 'PUT', body: JSON.stringify({ content }) });
          const idx = state.notes.findIndex(n => n.id === saved.id);
          if (idx >= 0) state.notes[idx] = saved;
          state.current = saved;
        } else {
          saved = await api('/api/notes', { method: 'POST', body: JSON.stringify({ content }) });
          state.notes.unshift(saved);
          state.current = saved;
          editState.hasId = true;
          // 不触发 hashchange，避免重渲染丢焦点
          history.replaceState({}, '', '#/n/' + encodeURIComponent(saved.id));
        }
        setStatus('已保存 ' + formatTime(saved.updated_at), 'saved');
      } catch (e) {
        setStatus('保存失败', 'error');
      }
      editState.saving = false;
      if (editState.pending) { editState.pending = false; autoSave(); }
    }

    function renderDetail(isNew) {
      const note = isNew ? null : state.current;
      const parts = splitContent(note ? note.content : '');
      // 初始化编辑历史
      editState.history = [{ title: parts.title, body: parts.body }];
      editState.index = 0;
      editState.hasId = !!(note && note.id);
      editState.saving = false; editState.pending = false;
      clearTimeout(editState.saveTimer);

      const right = `<button class="btn ghost" id="logoutBtn">退出</button>`;
      const back = `<button class="btn ghost back-btn" id="backBtn">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M15 18l-6-6 6-6"/></svg>返回
      </button>`;

      const body = `<div class="detail-card">
        <input class="title-input" id="title" placeholder="标题" autocomplete="off" value="${escapeAttr(parts.title)}">
        <textarea class="editor-area" id="content" placeholder="写点正文…" autofocus>${escapeHtml(parts.body)}</textarea>
      </div>`;

      const actions = `<div class="detail-actions">
        <button class="btn icon" id="undoBtn" title="撤回 (Ctrl+Z)" disabled><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7v6h6"/><path d="M21 17a9 9 0 0 0-15-6.7L3 13"/></svg></button>
        <button class="btn icon" id="redoBtn" title="恢复 (Ctrl+Y)" disabled><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 7v6h-6"/><path d="M3 17a9 9 0 0 1 15-6.7L21 13"/></svg></button>
        <span class="sep"></span>
        <button class="btn danger" id="deleteBtn">删除</button>
        <button class="btn primary" id="doneBtn">完成</button>
      </div>`;

      root.innerHTML = topbar(right) + `<div class="container">
        <div class="detail-head">${back}</div>
        ${body}
        <div class="detail-foot">
          <span class="status" id="status">${editState.hasId ? '已保存 ' + formatTime(note.updated_at) : '新便签'}</span>
          ${actions}
        </div>
      </div>`;

      const titleEl = document.getElementById('title');
      const bodyEl = document.getElementById('content');
      titleEl.oninput = onEdit; bodyEl.oninput = onEdit;
      titleEl.onkeydown = onKey; bodyEl.onkeydown = onKey;
      document.getElementById('backBtn').onclick = () => { flushSave(); go('/'); };
      document.getElementById('logoutBtn').onclick = async () => { await api('/api/logout', { method: 'POST' }); renderLogin(); };
      document.getElementById('undoBtn').onclick = undo;
      document.getElementById('redoBtn').onclick = redo;
      document.getElementById('deleteBtn').onclick = deleteDetail;
      document.getElementById('doneBtn').onclick = () => { flushSave(); go('/'); };
      if (isNew) titleEl.focus();
      updateUndoRedo();
    }

    async function deleteDetail() {
      if (!editState.hasId) { go('/'); return; }
      if (!confirm('删除这条便签？此操作不可撤销。')) return;
      try {
        await api('/api/notes/' + encodeURIComponent(state.current.id), { method: 'DELETE' });
        state.notes = state.notes.filter(n => n.id !== state.current.id);
        state.current = null;
        go('/');
      } catch (e) {
        alert(e.message);
      }
    }

    /* ---------- 主渲染 ---------- */
    function render() {
      const route = parseHash();
      if (route.name === 'new') {
        state.current = null;
        renderDetail(true);
        return;
      }
      if (route.name === 'note') {
        const note = state.notes.find(n => n.id === route.id);
        if (!note) { go('/'); return; }
        state.current = note;
        renderDetail(false);
        return;
      }
      // list
      renderList();
    }

    async function loadNotes() {
      const data = await api('/api/notes');
      state.notes = (data.notes || []).slice().sort((a, b) => (b.updated_at || 0) - (a.updated_at || 0));
      state.loading = false;
    }

    window.addEventListener('hashchange', render);

    (async function init() {
      try {
        await loadNotes();
        render();
      } catch (e) {
        renderLogin();
      }
    })();
  </script>
</body>
</html>"""


class NotesStore:
    def __init__(self, data_dir):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.notes_file = self.data_dir / "notes.json"
        self.auth_file = self.data_dir / "auth.json"

    def load_notes(self):
        if not self.notes_file.exists():
            return []
        try:
            with self.notes_file.open("r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, list):
                return []
        except Exception:
            return []
        for n in data:
            if not isinstance(n, dict):
                continue
            # 兼容旧数据：旧版有独立 title 字段，新版只用 content
            if "content" not in n:
                n["content"] = n.get("title", "")
            n.pop("title", None)
        return data

    def save_notes(self, notes):
        tmp = self.notes_file.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(notes, f, ensure_ascii=False, indent=2)
        tmp.replace(self.notes_file)

    def write_auth(self, username, password):
        salt = secrets.token_hex(16)
        password_hash = hash_password(password, salt)
        secret = secrets.token_hex(32)
        data = {"username": username, "salt": salt, "password_hash": password_hash, "secret": secret}
        with self.auth_file.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.chmod(self.auth_file, 0o600)

    def load_auth(self):
        with self.auth_file.open("r", encoding="utf-8") as f:
            return json.load(f)


def hash_password(password, salt):
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000)
    return base64.b64encode(digest).decode()


# 会话有效期（秒）：Cookie 内签发时间超过该值即视为过期，
# 避免旧签名无限期有效（服务端此前从不校验时间戳）。
SESSION_TTL_SECONDS = 7 * 24 * 60 * 60


def make_cookie(username, secret):
    ts = str(int(time.time()))
    payload = f"{username}:{ts}"
    sig = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{payload}:{sig}".encode()).decode()


def verify_cookie(value, auth):
    try:
        raw = base64.urlsafe_b64decode(value.encode()).decode()
        username, ts, sig = raw.rsplit(":", 2)
        if username != auth["username"]:
            return False
        payload = f"{username}:{ts}"
        expected = hmac.new(auth["secret"].encode(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return False
        # 签发时间超过 TTL 视为过期，避免旧 Cookie 永久有效
        if int(time.time()) - int(ts) > SESSION_TTL_SECONDS:
            return False
        return True
    except Exception:
        return False


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "SimpleNotes/1.0"

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/?"):
            self.send_html(INDEX_HTML)
            return
        if self.path.startswith("/api/notes"):
            if not self.require_auth():
                return
            self.handle_get_notes()
            return
        self.send_error(404)

    def do_POST(self):
        if self.path == "/api/login":
            self.handle_login()
            return
        if self.path == "/api/logout":
            self.send_json({"ok": True}, headers={"Set-Cookie": "notes_session=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax"})
            return
        if self.path == "/api/notes":
            if not self.require_auth():
                return
            self.handle_create_note()
            return
        self.send_error(404)

    def do_PUT(self):
        if self.path.startswith("/api/notes/"):
            if not self.require_auth():
                return
            self.handle_update_note()
            return
        self.send_error(404)

    def do_DELETE(self):
        if self.path.startswith("/api/notes/"):
            if not self.require_auth():
                return
            self.handle_delete_note()
            return
        self.send_error(404)

    def parse_json(self):
        length = int(self.headers.get("Content-Length") or "0")
        if length <= 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def note_id_from_path(self):
        return urllib.parse.unquote(self.path.split("/api/notes/", 1)[1].split("?", 1)[0])

    def require_auth(self):
        cookie = http.cookies.SimpleCookie(self.headers.get("Cookie", ""))
        value = cookie.get("notes_session")
        try:
            auth = self.server.store.load_auth()
        except Exception:
            self.send_json({"error": "auth is not configured"}, status=500)
            return False
        if value and verify_cookie(value.value, auth):
            return True
        self.send_json({"error": "unauthorized"}, status=401)
        return False

    def handle_login(self):
        data = self.parse_json()
        auth = self.server.store.load_auth()
        username = str(data.get("username") or "")
        password = str(data.get("password") or "")
        password_hash = hash_password(password, auth["salt"])
        if username != auth["username"] or not hmac.compare_digest(password_hash, auth["password_hash"]):
            self.send_json({"error": "账号或密码错误"}, status=403)
            return
        session = make_cookie(username, auth["secret"])
        cookie = f"notes_session={session}; Path=/; Max-Age={SESSION_TTL_SECONDS}; HttpOnly; SameSite=Lax"
        self.send_json({"ok": True}, headers={"Set-Cookie": cookie})

    def handle_get_notes(self):
        parsed = urllib.parse.urlparse(self.path)
        parts = parsed.path.rstrip("/").split("/")
        notes = self.server.store.load_notes()
        if len(parts) == 4 and parts[-1]:
            note_id = parts[-1]
            for note in notes:
                if note.get("id") == note_id:
                    self.send_json(note)
                    return
            self.send_json({"error": "not found"}, status=404)
            return
        q = urllib.parse.parse_qs(parsed.query).get("q", [""])[0].strip().lower()
        if q:
            terms = [t for t in q.split() if t]
            if terms:
                def match(n):
                    text = str(n.get("content", "")).lower()
                    return all(t in text for t in terms)
                notes = [n for n in notes if match(n)]
        notes.sort(key=lambda n: n.get("updated_at", 0), reverse=True)
        self.send_json({"notes": notes})

    def handle_create_note(self):
        data = self.parse_json()
        now = time.time()
        note = {
            "id": secrets.token_hex(8),
            "content": str(data.get("content") or ""),
            "created_at": now,
            "updated_at": now,
        }
        notes = self.server.store.load_notes()
        notes.append(note)
        self.server.store.save_notes(notes)
        self.send_json(note, status=201)

    def handle_update_note(self):
        note_id = self.note_id_from_path()
        data = self.parse_json()
        notes = self.server.store.load_notes()
        for note in notes:
            if note.get("id") == note_id:
                note["content"] = str(data.get("content") or "")
                note["updated_at"] = time.time()
                note.pop("title", None)
                self.server.store.save_notes(notes)
                self.send_json(note)
                return
        self.send_json({"error": "not found"}, status=404)

    def handle_delete_note(self):
        note_id = self.note_id_from_path()
        notes = self.server.store.load_notes()
        new_notes = [n for n in notes if n.get("id") != note_id]
        if len(new_notes) == len(notes):
            self.send_json({"error": "not found"}, status=404)
            return
        self.server.store.save_notes(new_notes)
        self.send_json({"ok": True})

    def send_html(self, body):
        raw = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def send_json(self, data, status=200, headers=None):
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, fmt, *args):
        print("[%s] %s" % (self.log_date_time_string(), fmt % args))


class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


def main():
    parser = argparse.ArgumentParser(description="Simple private notes web app")
    parser.add_argument("-H", "--host", default="0.0.0.0")
    parser.add_argument("-p", "--port", type=int, default=8899)
    parser.add_argument("-d", "--data-dir", default="./data")
    parser.add_argument("-u", "--username")
    parser.add_argument("-P", "--password")
    parser.add_argument("--init-auth", action="store_true")
    args = parser.parse_args()

    store = NotesStore(args.data_dir)
    if args.username and args.password:
        store.write_auth(args.username, args.password)
        if args.init_auth:
            return
    if not store.auth_file.exists():
        raise SystemExit("auth is not configured; run with --username and --password once")

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.store = store
    print(f"Simple Notes listening on http://{args.host}:{args.port}/ data={store.data_dir}")
    server.serve_forever()


if __name__ == "__main__":
    main()
