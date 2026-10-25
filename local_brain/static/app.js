import { Crepe } from '@milkdown/crepe';

let currentPath = null;
let sourceMode = false;
let crepeEditor = null;
let currentTab = 'editor';
let chatSessionId = null;

const sourceEditor = document.getElementById('source-editor');
const notePath = document.getElementById('note-path');
const treeEl = document.getElementById('tree');
const searchInput = document.getElementById('search-input');
const searchResults = document.getElementById('search-results');
const backlinksEl = document.getElementById('backlinks');
const tagsEl = document.getElementById('tags');
const modelSelect = document.getElementById('model-select');
const contextIndicator = document.getElementById('context-indicator');
const chatMessages = document.getElementById('chat-messages');
const chatInput = document.getElementById('chat-input');
const chatContext = document.getElementById('chat-context');
const chatSession = document.getElementById('chat-session');

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text == null ? '' : text;
    return div.innerHTML;
}

function escapeAttr(text) {
    return escapeHtml(text).replace(/"/g, '&quot;');
}

/** Transient status message at the bottom of the window. */
function showToast(message, isError = false) {
    let toast = document.getElementById('toast');
    if (!toast) {
        toast = document.createElement('div');
        toast.id = 'toast';
        document.getElementById('app').appendChild(toast);
    }
    toast.textContent = message;
    toast.classList.toggle('error', isError);
    toast.classList.add('visible');
    clearTimeout(toast._timer);
    toast._timer = setTimeout(() => toast.classList.remove('visible'), 4000);
}

async function api(url, opts = {}) {
    const res = await fetch(url, opts);
    if (!res.ok) {
        const err = await res.text();
        throw new Error(err);
    }
    return res.json();
}

// --- Milkdown editor ---

async function initMilkdown(content = '') {
    if (crepeEditor) {
        await crepeEditor.destroy();
    }
    const container = document.getElementById('milkdown-editor');
    container.innerHTML = '';
    crepeEditor = new Crepe({
        root: container,
        defaultValue: content,
    });
    await crepeEditor.create();
}

async function getEditorContent() {
    if (sourceMode) {
        return sourceEditor.value;
    }
    if (!crepeEditor) return '';
    return await crepeEditor.getMarkdown();
}

async function setEditorContent(content) {
    sourceEditor.value = content;
    if (!sourceMode) {
        await initMilkdown(content);
    }
}

function toggleSourceMode() {
    sourceMode = !sourceMode;
    const milkdownEl = document.getElementById('milkdown-editor');
    if (sourceMode) {
        milkdownEl.classList.add('hidden');
        sourceEditor.classList.remove('hidden');
    } else {
        sourceEditor.classList.add('hidden');
        milkdownEl.classList.remove('hidden');
        initMilkdown(sourceEditor.value);
    }
}

// --- Tree and notes ---

async function loadTree() {
    const data = await api('/api/tree');
    treeEl.innerHTML = '';
    // The API returns a synthetic "local-brain" root whose path is empty. It
    // duplicates the sidebar title and its row sat off-alignment with the
    // search field above it, so render its children directly instead.
    const [root, ...roots] = data.tree;
    const nodes = root && root.is_dir && root.children.length ? root.children : data.tree;
    renderTree(nodes, treeEl);
}

function renderTree(nodes, container) {
    nodes.forEach(node => {
        const el = document.createElement('div');
        el.className = `tree-item ${node.is_dir ? 'dir' : ''}`;
        el.textContent = node.name;
        el.dataset.path = node.path;
        if (!node.is_dir) {
            el.onclick = () => openNote(node.path);
        }
        container.appendChild(el);

        if (node.children && node.children.length > 0) {
            const childContainer = document.createElement('div');
            childContainer.className = 'tree-children';
            renderTree(node.children, childContainer);
            container.appendChild(childContainer);
        }
    });
}

/**
 * Open a note in the editor.
 *
 * Callers can fire this from the tree, search results, backlinks, or the graph
 * sidebar while another tab is showing, so this always switches to the editor
 * first and reports failures in place instead of leaving a half-open note.
 */
async function openNote(path) {
    if (!path) return;
    switchTab('editor');
    try {
        const data = await api(`/api/notes/${encodeURIComponent(path)}`);
        currentPath = path;
        await setEditorContent(data.content);
        updateTags(data.content);
        notePath.textContent = path;
        // Backlinks are a slow follow-up; do not block opening the note.
        loadBacklinks(path).catch(() => {});
        updateTreeActive(path);
    } catch (err) {
        showNoteError(path, err);
    }
}

function showNoteError(path, err) {
    currentPath = null;
    notePath.textContent = `Could not open ${path}`;
    backlinksEl.innerHTML = '—';
    tagsEl.innerHTML = '—';
    setEditorContent('');
    showToast(`Could not open ${path}: ${err.message}`);
}

function updateTags(content) {
    const tags = [...content.matchAll(/(?<!\w)#([\w\-]+)/g)].map(m => m[1]);
    const unique = [...new Set(tags)];
    tagsEl.innerHTML = unique.length
        ? unique.map(t => `<span class="tag">#${t}</span>`).join(' ')
        : '—';
}

async function loadBacklinks(path) {
    const data = await api(`/api/backlinks/${encodeURIComponent(path)}`);
    backlinksEl.innerHTML = data.backlinks.length
        ? data.backlinks.map(b => `<span class="backlink" data-path="${b.path}">${b.title}</span>`).join('')
        : 'No backlinks.';
    backlinksEl.querySelectorAll('.backlink').forEach(el => {
        el.onclick = () => openNote(el.dataset.path);
    });
}

function updateTreeActive(path) {
    document.querySelectorAll('.tree-item').forEach(el => {
        el.classList.toggle('active', el.dataset.path === path);
    });
}

async function saveCurrentNote() {
    if (!currentPath) return;
    const content = await getEditorContent();
    await api(`/api/notes/${encodeURIComponent(currentPath)}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content }),
    });
    updateTags(content);
}

/** Normalise user input into a vault-relative .md path under pages/. */
function normaliseNotePath(input) {
    let path = input.trim().replace(/^[/\\]+|[/\\]+$/g, '');
    if (!path) return null;
    if (!/\.md$/i.test(path)) path += '.md';
    // A bare name belongs in pages/; anything with a separator is taken as given.
    if (!path.includes('/')) path = `pages/${path}`;
    return path;
}

const newNoteModal = document.getElementById('new-note-modal');
const newNoteForm = document.getElementById('new-note-form');
const nnTarget = document.getElementById('nn-target');
const nnTitle = document.getElementById('nn-title');
const nnTags = document.getElementById('nn-tags');
const nnContent = document.getElementById('nn-content');
const nnTitleLabel = document.getElementById('nn-title-label');
const nnHint = document.getElementById('nn-hint');
let logseqStatus = null;

/** Show which fields make sense for the selected destination. */
function syncNewNoteFields() {
    const target = nnTarget.value;
    const logseq = target.startsWith('logseq');
    document.querySelectorAll('.nn-logseq-only').forEach(el => {
        el.style.display = logseq ? '' : 'none';
    });
    if (target === 'logseq-journal') {
        nnTitleLabel.textContent = 'Note';
        nnTitle.placeholder = 'What happened today?';
        nnHint.textContent = logseqStatus && !logseqStatus.reachable
            ? `Logseq CLI unavailable: ${logseqStatus.error || 'unknown error'}. Vault notes still work.`
            : `Appended to ${(logseqStatus && logseqStatus.journal_title) || "today's journal"} in the Logseq vault.`;
    } else if (target === 'logseq-page') {
        nnTitleLabel.textContent = 'Page title';
        nnTitle.placeholder = 'e.g. Project Phoenix';
        nnHint.textContent = 'Creates a real page in the Logseq vault. Tags are created automatically.';
    } else {
        nnTitleLabel.textContent = 'Title';
        nnTitle.placeholder = 'e.g. ideas/project-x';
        nnHint.textContent = 'Saves a markdown file in the local-brain vault.';
    }
}

/** Warm the Logseq health payload so the dialog can warn before saving. */
async function loadLogseqStatus() {
    try {
        logseqStatus = await api('/api/logseq/status');
    } catch (err) {
        logseqStatus = { available: false, reachable: false, error: err.message };
    }
    if (newNoteModal.open) syncNewNoteFields();
}

function openNewNoteModal() {
    newNoteForm.reset();
    loadLogseqStatus();
    syncNewNoteFields();
    newNoteModal.showModal();
    nnTitle.focus();
}

/** Create the note in the vault, then open it. */
async function createVaultNote(title) {
    const path = normaliseNotePath(title);
    if (!path) return;
    try {
        await api('/api/notes', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ path }),
        });
    } catch (err) {
        // 409 means it already exists -- opening it is still the useful outcome,
        // but anything else is a real failure the user needs to see.
        if (!/already exists/i.test(err.message)) {
            showToast(`Could not create ${path}: ${err.message}`, true);
            return;
        }
        showToast(`${path} already exists, opening it.`);
    }
    await loadTree();
    await openNote(path);
}

/** Create the note in Logseq through the backend's CLI wrapper. */
async function createLogseqNote(kind) {
    const title = nnTitle.value.trim();
    const tags = nnTags.value.trim();
    const content = nnContent.value.trim();

    if (kind === 'logseq-page' && !title) {
        showToast('A Logseq page needs a title.', true);
        nnTitle.focus();
        return;
    }
    if (kind === 'logseq-journal' && !title && !content) {
        showToast('Enter a note before saving to the journal.', true);
        nnTitle.focus();
        return;
    }

    const btn = document.getElementById('nn-create');
    btn.disabled = true;
    try {
        const res = await api('/api/logseq/notes', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                title,
                kind: kind === 'logseq-journal' ? 'journal' : 'page',
                tags,
                content,
            }),
        });
        showToast(kind === 'logseq-journal'
            ? `Added to ${res.page} in Logseq.`
            : `${res.page_existed ? 'Updated' : 'Created'} Logseq page ${res.page}.`);
    } catch (err) {
        showToast(`Logseq: ${err.message}`, true);
    } finally {
        btn.disabled = false;
    }
}

async function submitNewNote(ev) {
    ev.preventDefault();
    const target = nnTarget.value;
    const title = nnTitle.value.trim();

    if (target === 'vault') {
        if (!title) {
            showToast('Give the note a title.', true);
            nnTitle.focus();
            return;
        }
        newNoteModal.close();
        await createVaultNote(title);
        return;
    }
    await createLogseqNote(target);
    newNoteModal.close();
}

async function deleteCurrentNote() {
    if (!currentPath) return;
    if (!confirm(`Delete ${currentPath}?`)) return;
    await api(`/api/notes/${encodeURIComponent(currentPath)}`, { method: 'DELETE' });
    currentPath = null;
    await setEditorContent('');
    notePath.textContent = 'No note open';
    backlinksEl.innerHTML = 'Open a note to see backlinks.';
    tagsEl.innerHTML = '—';
    await loadTree();
}

async function performSearch(query) {
    if (!query.trim()) {
        searchResults.innerHTML = '';
        treeEl.style.display = '';
        return;
    }
    treeEl.style.display = 'none';
    const data = await api(`/api/search?q=${encodeURIComponent(query)}`);
    searchResults.innerHTML = data.results.map(r => `
        <div class="search-result" data-path="${r.path}">
            <div class="search-result-title">${r.title}</div>
            <div class="search-result-preview">${r.preview}</div>
        </div>
    `).join('');
    searchResults.querySelectorAll('.search-result').forEach(el => {
        el.onclick = () => openNote(el.dataset.path);
    });
}

// --- Tabs ---

function switchTab(tab) {
    currentTab = tab;
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.toggle('active', b.dataset.tab === tab));
    document.querySelectorAll('.tab-pane').forEach(p => p.classList.toggle('active', p.id === `${tab}-tab`));

    const editorToolbar = document.getElementById('editor-toolbar');
    const editorActions = document.getElementById('editor-actions');
    // Each tab owns one right-hand toolbar block; hide them all, then reveal
    // the one that belongs to the active tab.
    const actionBlocks = {
        chat: 'chat-actions',
        jobs: 'jobs-actions',
        leads: 'leads-actions',
        todos: 'todos-actions',
        calendar: 'calendar-actions',
    };
    Object.values(actionBlocks).forEach(id => {
        const el = document.getElementById(id);
        if (el) el.style.display = 'none';
    });

    editorToolbar.style.display = 'none';
    editorActions.style.display = 'none';
    if (actionBlocks[tab]) {
        document.getElementById(actionBlocks[tab]).style.display = 'flex';
    }

    // App health polling only matters on the chat tab; leaving it running after
    // opening a note is wasted background requests.
    stopAppsPolling();

    if (tab === 'editor') {
        editorToolbar.style.display = 'flex';
        editorActions.style.display = 'flex';
    } else if (tab === 'chat') {
        loadModels();
        startAppsPolling();
    } else if (tab === 'graph') {
        renderGraph();
    } else if (tab === 'jobs') {
        loadJobs();
    } else if (tab === 'leads') {
        loadLeads();
    } else if (tab === 'todos') {
        loadTodos();
    } else if (tab === 'calendar') {
        loadCalendar();
    }
}

// --- Local apps ---

const appsStatus = document.getElementById('apps-status');
let appsTimer = null;

function renderApps(data) {
    if (!appsStatus) return;

    const rows = data.apps.map(app => `
        <div class="app-row ${app.up ? 'up' : 'down'}">
            <span class="app-dot"></span>
            <span class="app-name">${escapeHtml(app.name)}</span>
            ${app.launchable ? `<span class="app-launch" data-launch="${escapeAttr(app.launch)}">launch</span>` : ''}
        </div>
        <div class="app-detail">${escapeHtml(app.note || app.detail)}</div>
    `).join('');

    appsStatus.innerHTML = `<div class="apps-summary">${data.up} of ${data.total} online</div>${rows}`;

    appsStatus.querySelectorAll('.app-launch').forEach(el => {
        el.addEventListener('click', () => launchApp(el.dataset.launch));
    });
}

async function loadApps() {
    if (!appsStatus) return;
    try {
        renderApps(await api('/api/apps'));
    } catch (err) {
        appsStatus.textContent = `could not reach local-brain: ${err.message}`;
    }
}

/**
 * Launching is done by the desktop shell, not the browser: there is no
 * backend endpoint that spawns other apps, so a web-served local-brain cannot
 * start them. The .desktop entries are the supported path.
 */
function launchApp(command) {
    const message = `Run: ${command}\n\nThese apps are launched from your desktop entries, so a browser-served local-brain cannot start them for you.`;
    if (window.localBrainLaunch) {
        window.localBrainLaunch(command);
    } else {
        alert(message);
    }
}

function startAppsPolling() {
    stopAppsPolling();
    loadApps();
    appsTimer = setInterval(loadApps, 15000);

    const refresh = document.getElementById('apps-refresh');
    if (refresh && !refresh.dataset.wired) {
        refresh.dataset.wired = '1';
        refresh.addEventListener('click', loadApps);
    }
}

function stopAppsPolling() {
    if (appsTimer) {
        clearInterval(appsTimer);
        appsTimer = null;
    }
}

// --- Graph ---

let graphZoom = null;       // d3 zoom behaviour, kept so the buttons can reuse it
let graphSvg = null;
let graphNodes = null;
let graphSimulation = null; // stopped on re-render, otherwise ticks pile up
let graphOpenNode = null;   // focused node, so Reset can return to it

const graphNoteFilter = document.getElementById('graph-note-filter');
const graphNoteList = document.getElementById('graph-note-list');

/** Populate the "Open a note" list beside the graph. */
function renderGraphNoteList(nodes) {
    const needle = (graphNoteFilter.value || '').toLowerCase();
    const matches = nodes
        .filter(n => !needle
            || n.title.toLowerCase().includes(needle)
            || n.path.toLowerCase().includes(needle))
        .sort((a, b) => a.title.localeCompare(b.title));

    const count = document.getElementById('graph-count');
    if (count) {
        count.textContent = needle
            ? `${matches.length} of ${nodes.length}`
            : `${nodes.length} notes`;
    }

    graphNoteList.innerHTML = matches
        .map(n => `<div class="graph-note-item" data-path="${escapeAttr(n.path)}">
                       ${escapeHtml(n.title)}
                       <span class="graph-note-path">${escapeHtml(n.path)}</span>
                   </div>`)
        .join('');

    graphNoteList.querySelectorAll('.graph-note-item').forEach(el => {
        el.addEventListener('click', () => {
            switchTab('editor');
            openNote(el.dataset.path);
        });
    });
}

async function renderGraph() {
    // Switching tabs re-enters this function. Stop the previous simulation so
    // its tick handler does not keep running against a detached DOM.
    if (graphSimulation) {
        graphSimulation.stop();
        graphSimulation = null;
    }

    const data = await api('/api/graph');
    const container = document.getElementById('graph-view');
    container.innerHTML = '';

    renderGraphNoteList(data.nodes);

    // The pane has no size until the tab is laid out, so fall back to the
    // window rather than drawing a 0x0 graph.
    const width = container.clientWidth || container.parentElement.clientWidth || 900;
    const height = container.clientHeight || 600;

    const svg = d3.select('#graph-view')
        .append('svg')
        .attr('width', width)
        .attr('height', height);
    graphSvg = svg;

    // Everything that moves lives under one <g> so a single transform can
    // scale and pan the whole graph.
    const root = svg.append('g');

    const simulation = d3.forceSimulation(data.nodes)
        .force('link', d3.forceLink(data.links).id(d => d.id).distance(100))
        .force('charge', d3.forceManyBody().strength(-300))
        .force('center', d3.forceCenter(width / 2, height / 2));

    const link = root.append('g')
        .selectAll('line')
        .data(data.links)
        .join('line')
        .attr('stroke', '#475569')
        .attr('stroke-width', 1.5);

    const node = root.append('g')
        .selectAll('g')
        .data(data.nodes)
        .join('g')
        .style('cursor', 'pointer')
        .call(d3.drag()
            .on('start', dragstarted)
            .on('drag', dragged)
            .on('end', dragended));
    graphNodes = node;

    node.append('circle')
        .attr('r', 6)
        .attr('fill', d => d.group === 'pages' ? '#38bdf8' : '#a78bfa');

    node.append('text')
        .text(d => d.title)
        .attr('x', 10)
        .attr('y', 4)
        .attr('fill', '#e2e8f0')
        .attr('font-size', '12px');

    // Double-click opens: a single click is too easy to fire while panning, and
    // on a dense graph an accidental click should not throw away the view.
    node.on('click', (_event, d) => {
        graphOpenNode = d;
        node.select('circle')
            .attr('stroke', n => (n === d ? '#f8fafc' : 'none'))
            .attr('stroke-width', 2);
    });

    node.on('dblclick', (event, d) => {
        event.stopPropagation();
        switchTab('editor');
        openNote(d.path);
    });

    // Wheel zoom and drag-to-pan. scaleExtent keeps the nodes from becoming
    // invisible or filling the pane when the buttons are held down.
    graphZoom = d3.zoom()
        .scaleExtent([0.1, 8])
        .on('zoom', (event) => {
            root.attr('transform', event.transform);
        });

    svg.call(graphZoom)
        .on('dblclick.zoom', null)   // keep node double-click working
        .on('click', (event) => {    // click on empty space clears the focus ring
            if (event.target.tagName === 'svg') graphOpenNode = null;
        });

    document.getElementById('graph-zoom-in').onclick = () =>
        svg.transition().duration(200).call(graphZoom.scaleBy, 1.4);
    document.getElementById('graph-zoom-out').onclick = () =>
        svg.transition().duration(200).call(graphZoom.scaleBy, 1 / 1.4);
    document.getElementById('graph-zoom-reset').onclick = () => resetGraphView(simulation);

    graphNoteFilter.oninput = () => renderGraphNoteList(data.nodes);
    graphSimulation = simulation;

    simulation.on('tick', () => {
        link
            .attr('x1', d => d.source.x)
            .attr('y1', d => d.source.y)
            .attr('x2', d => d.target.x)
            .attr('y2', d => d.target.y);
        node.attr('transform', d => `translate(${d.x},${d.y})`);
    });

    function dragstarted(event, d) {
        if (!event.active) simulation.alphaTarget(0.3).restart();
        d.fx = d.x;
        d.fy = d.y;
    }

    function dragged(event, d) {
        d.fx = event.x;
        d.fy = event.y;
    }

    function dragended(event, d) {
        if (!event.active) simulation.alphaTarget(0);
        d.fx = null;
        d.fy = null;
    }
}

/**
 * Reset the view: identity transform, or centred on the node the user last
 * clicked so a big graph can be re-found after zooming away from it.
 */
function resetGraphView(simulation) {
    if (!graphSvg || !graphZoom) return;

    if (graphOpenNode && graphNodes) {
        graphNodes.select('circle')
            .attr('stroke', n => (n === graphOpenNode ? '#f8fafc' : 'none'))
            .attr('stroke-width', 2);
        graphSvg.transition().duration(300).call(
            graphZoom.transform,
            d3.zoomIdentity.translate(-graphOpenNode.x, -graphOpenNode.y).scale(1.6)
        );
        return;
    }

    if (simulation) {
        simulation.alpha(0.6).restart();
        simulation.force('center').x(widthOfGraph() / 2).y(heightOfGraph() / 2);
    }
    graphSvg.transition().duration(300).call(graphZoom.transform, d3.zoomIdentity);
}

function widthOfGraph() {
    const el = document.getElementById('graph-view');
    return (el && el.clientWidth) || 900;
}

function heightOfGraph() {
    const el = document.getElementById('graph-view');
    return (el && el.clientHeight) || 600;
}

// --- Chat ---

async function loadModels() {
    try {
        const data = await api('/api/models');
        modelSelect.innerHTML = data.models.map(m =>
            `<option value="${m.id}">${m.name} ${m.local ? '(local)' : ''}</option>`
        ).join('');
    } catch (e) {
        modelSelect.innerHTML = '<option>No models</option>';
    }
}

async function fetchContext(message) {
    try {
        const data = await api(`/api/context?message=${encodeURIComponent(message)}&k=4`);
        chatContext.innerHTML = data.chunks.map(c => `
            <div class="context-chunk">
                <small>[${c.source}] ${c.title || ''}</small><br>
                ${c.content.slice(0, 200)}${c.content.length > 200 ? '...' : ''}
            </div>
        `).join('') || '—';
        contextIndicator.textContent = data.chunks.length ? `retrieved ${data.chunks.length} chunks` : 'no context';
        return data.chunks;
    } catch (e) {
        chatContext.innerHTML = '—';
        contextIndicator.textContent = 'context unavailable';
        return [];
    }
}

function appendMessage(role, content) {
    const div = document.createElement('div');
    div.className = `chat-message ${role}`;
    div.innerHTML = `<div class="role">${role}</div><div class="content"></div>`;
    div.querySelector('.content').textContent = content;
    chatMessages.appendChild(div);
    chatMessages.scrollTop = chatMessages.scrollHeight;
    return div.querySelector('.content');
}

async function sendChatMessage() {
    const message = chatInput.value.trim();
    if (!message) return;

    appendMessage('user', message);
    chatInput.value = '';

    await fetchContext(message);

    const model = modelSelect.value;
    const assistantContent = document.createElement('div');
    const msgDiv = document.createElement('div');
    msgDiv.className = 'chat-message assistant';
    msgDiv.innerHTML = `<div class="role">assistant</div>`;
    msgDiv.appendChild(assistantContent);
    chatMessages.appendChild(msgDiv);

    try {
        const resp = await fetch('/api/chat', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ message, model, session_id: chatSessionId }),
        });

        if (!resp.ok) {
            assistantContent.textContent = `Error: ${await resp.text()}`;
            return;
        }

        const reader = resp.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';

        while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            buffer += decoder.decode(value, { stream: true });
            const lines = buffer.split('\n');
            buffer = lines.pop();
            for (const line of lines) {
                const trimmed = line.trim();
                if (!trimmed.startsWith('data:')) continue;
                const data = trimmed.slice(5).trim();
                if (!data) continue;
                try {
                    const event = JSON.parse(data);
                    if (event.type === 'delta' && event.content) {
                        assistantContent.textContent += event.content;
                        chatMessages.scrollTop = chatMessages.scrollHeight;
                    } else if (event.type === 'session') {
                        chatSessionId = event.session_id;
                        chatSession.textContent = chatSessionId ? chatSessionId.slice(0, 16) + '...' : '—';
                    } else if (event.type === 'tool_calls') {
                        assistantContent.textContent += '\n[tool calls]\n' + JSON.stringify(event.tool_calls, null, 2);
                    }
                } catch (e) {
                    // ignore malformed SSE
                }
            }
        }
    } catch (e) {
        assistantContent.textContent = `Error: ${e.message}`;
    }
}

// --- Event listeners ---

document.getElementById('save-btn').onclick = saveCurrentNote;
document.getElementById('new-note-btn').onclick = openNewNoteModal;
document.getElementById('nn-cancel').onclick = () => newNoteModal.close();
nnTarget.onchange = syncNewNoteFields;
newNoteForm.onsubmit = submitNewNote;
document.getElementById('delete-btn').onclick = deleteCurrentNote;
document.getElementById('source-toggle').onclick = toggleSourceMode;
document.getElementById('chat-send').onclick = sendChatMessage;

sourceEditor.addEventListener('input', () => {
    updateTags(sourceEditor.value);
});

searchInput.addEventListener('input', (e) => performSearch(e.target.value));

document.querySelectorAll('.tab-btn').forEach(btn => {
    btn.onclick = () => switchTab(btn.dataset.tab);
});

chatInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        sendChatMessage();
    }
});

document.addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === 's') {
        e.preventDefault();
        saveCurrentNote();
    }
});

// --- Jobs ---

const jobsState = { limit: 25, offset: 0, sourcesLoaded: false };

function jobsParams() {
    const params = new URLSearchParams({
        limit: jobsState.limit,
        offset: jobsState.offset,
        sort: document.getElementById('jobs-sort').value,
    });
    const q = document.getElementById('jobs-search').value.trim();
    if (q) params.set('q', q);
    const source = document.getElementById('jobs-source').value;
    if (source) params.set('source', source);
    if (document.getElementById('jobs-remote').checked) params.set('remote', 'true');
    if (document.getElementById('jobs-salary').checked) params.set('salary_known', 'true');
    return params;
}

function formatSalary(job) {
    if (!job.salary_is_known || !job.salary_eur_yr) return '';
    const amount = Math.round(job.salary_eur_yr).toLocaleString();
    const period = job.salary_period === 'month' ? '/mo' : '/yr';
    return `€${amount}${period}`;
}

function formatSeen(iso) {
    if (!iso) return '';
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return '';
    return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}

function renderJobs(data) {
    const list = document.getElementById('jobs-list');
    const count = document.getElementById('jobs-count');
    const page = document.getElementById('jobs-page');

    count.textContent = `${data.total.toLocaleString()} jobs`;
    const from = data.total === 0 ? 0 : jobsState.offset + 1;
    const to = Math.min(jobsState.offset + data.jobs.length, data.total);
    page.textContent = `${from}–${to} of ${data.total.toLocaleString()}`;

    document.getElementById('jobs-prev').disabled = jobsState.offset === 0;
    document.getElementById('jobs-next').disabled = to >= data.total;

    if (!data.jobs.length) {
        list.innerHTML = '<div class="empty">No jobs match these filters.</div>';
        return;
    }

    list.innerHTML = data.jobs.map(job => {
        const salary = formatSalary(job);
        const badges = [
            job.remote ? '<span class="badge">remote</span>' : '',
            job.employment_type ? `<span class="badge muted">${escapeHtml(job.employment_type)}</span>` : '',
            job.seniority ? `<span class="badge muted">${escapeHtml(job.seniority)}</span>` : '',
            salary ? `<span class="badge salary">${escapeHtml(salary)}</span>` : '',
        ].join('');
        const meta = [job.company, job.location, job.source, `seen ${formatSeen(job.last_seen)}`]
            .filter(Boolean).map(escapeHtml).join(' &middot; ');

        return `
            <a class="job-card" href="${escapeAttr(job.url)}" target="_blank" rel="noopener">
                <div class="job-head">
                    <span class="job-title">${escapeHtml(job.title)}</span>
                    <span class="job-company">${escapeHtml(job.company || '—')}</span>
                </div>
                <div class="job-meta">${meta}</div>
                <div class="job-badges">${badges}</div>
            </a>`;
    }).join('');
}

async function loadJobs() {
    const list = document.getElementById('jobs-list');
    list.innerHTML = '<div class="empty">Loading jobs…</div>';
    try {
        const data = await api(`/api/jobs?${jobsParams()}`);
        renderJobs(data);

        const sourceSelect = document.getElementById('jobs-source');
        if (!jobsState.sourcesLoaded) {
            sourceSelect.innerHTML = '<option value="">All sources</option>' +
                data.sources.map(s => `<option value="${escapeAttr(s)}">${escapeHtml(s)}</option>`).join('');
            jobsState.sourcesLoaded = true;
        }
    } catch (err) {
        list.innerHTML = `<div class="empty error">Could not load jobs: ${escapeHtml(err.message)}</div>`;
    }

    try {
        const stats = await api('/api/jobs/stats');
        document.getElementById('jobs-stats').innerHTML = stats.available ? `
            <div class="stat"><strong>${stats.total.toLocaleString()}</strong> jobs</div>
            <div class="stat"><strong>${stats.remote.toLocaleString()}</strong> remote</div>
            <div class="stat"><strong>${stats.with_salary.toLocaleString()}</strong> with salary</div>
            <div class="stat muted">newest ${escapeHtml(formatSeen(stats.newest_seen) || '—')}</div>
        ` : `<div class="empty error">${escapeHtml(stats.error)}</div>`;

        document.getElementById('jobs-sources').innerHTML = stats.available
            ? Object.entries(stats.by_source).map(([src, n]) =>
                `<div class="stat"><span>${escapeHtml(src)}</span><strong>${n.toLocaleString()}</strong></div>`).join('')
            : '—';
    } catch (err) {
        document.getElementById('jobs-stats').innerHTML = `<div class="empty error">${escapeHtml(err.message)}</div>`;
    }
}

function resetJobsPage() {
    jobsState.offset = 0;
    loadJobs();
}

document.getElementById('jobs-refresh').addEventListener('click', () => loadJobs());
document.getElementById('jobs-prev').addEventListener('click', () => {
    jobsState.offset = Math.max(0, jobsState.offset - jobsState.limit);
    loadJobs();
});
document.getElementById('jobs-next').addEventListener('click', () => {
    jobsState.offset += jobsState.limit;
    loadJobs();
});
document.getElementById('jobs-source').addEventListener('change', resetJobsPage);
document.getElementById('jobs-sort').addEventListener('change', resetJobsPage);
document.getElementById('jobs-remote').addEventListener('change', resetJobsPage);
document.getElementById('jobs-salary').addEventListener('change', resetJobsPage);

// Debounce so each keystroke does not hit Radicale/FTS with a partial query.
let jobsSearchTimer = null;
document.getElementById('jobs-search').addEventListener('input', () => {
    clearTimeout(jobsSearchTimer);
    jobsSearchTimer = setTimeout(resetJobsPage, 250);
});

// --- Leads (Kalo Concierge research pipeline) ---

const leadsState = { limit: 25, offset: 0, filtersLoaded: false };

function leadsParams() {
    const params = new URLSearchParams({
        limit: leadsState.limit,
        offset: leadsState.offset,
        sort: document.getElementById('leads-sort').value,
    });
    const q = document.getElementById('leads-search').value.trim();
    if (q) params.set('q', q);
    const status = document.getElementById('leads-status').value;
    if (status) params.set('status', status);
    const area = document.getElementById('leads-area').value;
    if (area) params.set('area', area);
    if (document.getElementById('leads-contacted').checked) params.set('contacted', 'true');
    return params;
}

function formatDay(iso) {
    if (!iso) return '';
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return '';
    return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}

// Status pills are colour-coded so a glance shows the funnel; `contacted` also
// renders its timestamp, which is how the leadops write-back becomes visible.
const LEAD_STATUS_CLASS = {
    new: 'muted', queued: '', contacted: 'done',
    interested: 'salary', not_now: '', no_answer: '', rejected: 'rejected',
};

function leadStatusBadge(lead) {
    const status = lead.status || 'new';
    const cls = LEAD_STATUS_CLASS[status] || '';
    let label = escapeHtml(status.replace(/_/g, ' '));
    if (status === 'contacted' && lead.contacted_at) label += ` ${formatDay(lead.contacted_at)}`;
    return `<span class="badge ${cls}">${label}</span>`;
}

function renderLeads(data) {
    const list = document.getElementById('leads-list');
    const count = document.getElementById('leads-count');
    const page = document.getElementById('leads-page');

    count.textContent = `${data.total.toLocaleString()} leads`;
    const from = data.total === 0 ? 0 : leadsState.offset + 1;
    const to = Math.min(leadsState.offset + data.leads.length, data.total);
    page.textContent = `${from}–${to} of ${data.total.toLocaleString()}`;

    document.getElementById('leads-prev').disabled = leadsState.offset === 0;
    document.getElementById('leads-next').disabled = to >= data.total;

    if (!data.leads.length) {
        list.innerHTML = '<div class="empty">No leads match these filters.</div>';
        return;
    }

    list.innerHTML = data.leads.map(lead => {
        const badges = [leadStatusBadge(lead)].join('');
        const replies = lead.replies && lead.replies.total
            ? `<span class="badge">${lead.replies.total} repl${lead.replies.total === 1 ? 'y' : 'ies'}</span>`
            : '';
        const hook = lead.hook
            ? `<div class="job-meta">${escapeHtml(lead.hook)}</div>` : '';
        // Headline shows the city, falling back to the area when the lead has
        // no city recorded. Only add the area to the meta line when the
        // headline is showing something else, so it never repeats.
        const headline = lead.addr_city || lead.area_name || '';
        const place = [lead.addr_city ? lead.area_name : '', lead.owner_name]
            .filter(Boolean).map(escapeHtml).join(' &middot; ');
        const link = lead.website
            ? `<a class="job-title" href="${escapeAttr(lead.website)}" target="_blank" rel="noopener">${escapeHtml(lead.name)}</a>`
            : `<span class="job-title">${escapeHtml(lead.name)}</span>`;

        return `
            <div class="job-card lead-card" data-lead="${lead.id}">
                <div class="job-head">
                    ${link}
                    <span class="job-company">${escapeHtml(lead.addr_city || lead.area_name || '—')}</span>
                </div>
                ${place ? `<div class="job-meta">${place}</div>` : ''}
                ${hook}
                <div class="job-badges">${badges}${replies}</div>
            </div>`;
    }).join('');
}

async function loadLeads() {
    const list = document.getElementById('leads-list');
    list.innerHTML = '<div class="empty">Loading leads…</div>';
    try {
        const data = await api(`/api/leads?${leadsParams()}`);
        renderLeads(data);

        // Populate the filter dropdowns once, preserving whatever is selected.
        if (!leadsState.filtersLoaded) {
            const statusSelect = document.getElementById('leads-status');
            const areaSelect = document.getElementById('leads-area');
            statusSelect.innerHTML = '<option value="">All statuses</option>' +
                data.statuses.map(s => `<option value="${escapeAttr(s)}">${escapeHtml(s.replace(/_/g, ' '))}</option>`).join('');
            areaSelect.innerHTML = '<option value="">All areas</option>' +
                data.areas.map(a => `<option value="${escapeAttr(a)}">${escapeHtml(a)}</option>`).join('');
            leadsState.filtersLoaded = true;
        }
    } catch (err) {
        list.innerHTML = `<div class="empty error">Could not load leads: ${escapeHtml(err.message)}</div>`;
    }

    try {
        const stats = await api('/api/leads/stats');
        document.getElementById('leads-stats').innerHTML = stats.available ? `
            <div class="stat"><strong>${stats.total.toLocaleString()}</strong> leads</div>
            <div class="stat"><strong>${stats.contacted.toLocaleString()}</strong> contacted</div>
            <div class="stat"><strong>${stats.with_phone.toLocaleString()}</strong> with phone</div>
            <div class="stat"><strong>${stats.replies.toLocaleString()}</strong> replies</div>
        ` : `<div class="empty error">${escapeHtml(stats.error)}</div>`;

        document.getElementById('leads-areas').innerHTML = stats.available
            ? Object.entries(stats.by_area).map(([area, n]) =>
                `<div class="stat"><span>${escapeHtml(area)}</span><strong>${n.toLocaleString()}</strong></div>`).join('')
            : '—';
    } catch (err) {
        document.getElementById('leads-stats').innerHTML = `<div class="empty error">${escapeHtml(err.message)}</div>`;
    }
}

function resetLeadsPage() {
    leadsState.offset = 0;
    loadLeads();
}

document.getElementById('leads-refresh').addEventListener('click', () => loadLeads());
document.getElementById('leads-prev').addEventListener('click', () => {
    leadsState.offset = Math.max(0, leadsState.offset - leadsState.limit);
    loadLeads();
});
document.getElementById('leads-next').addEventListener('click', () => {
    leadsState.offset += leadsState.limit;
    loadLeads();
});
document.getElementById('leads-status').addEventListener('change', resetLeadsPage);
document.getElementById('leads-area').addEventListener('change', resetLeadsPage);
document.getElementById('leads-sort').addEventListener('change', resetLeadsPage);
document.getElementById('leads-contacted').addEventListener('change', resetLeadsPage);

let leadsSearchTimer = null;
document.getElementById('leads-search').addEventListener('input', () => {
    clearTimeout(leadsSearchTimer);
    leadsSearchTimer = setTimeout(resetLeadsPage, 250);
});

// --- To-dos ---

const PRIORITY_LABELS = { 1: 'High', 2: 'High', 3: 'Medium', 4: 'Medium', 5: 'Medium', 6: 'Low', 7: 'Low', 8: 'Low', 9: 'Low' };

function formatDue(due) {
    if (!due) return '';
    const d = new Date(`${due}T00:00:00`);
    if (Number.isNaN(d.getTime())) return due;
    const today = new Date();
    today.setHours(0, 0, 0, 0);
    const diffDays = Math.round((d - today) / 86400000);
    if (diffDays === 0) return 'today';
    if (diffDays === 1) return 'tomorrow';
    if (diffDays === -1) return 'yesterday';
    return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}

function renderTodos(todos) {
    const list = document.getElementById('todo-list');
    const open = todos.filter(t => !t.done);
    const done = todos.filter(t => t.done);

    document.getElementById('todos-counts').innerHTML = `
        <div class="stat"><strong>${open.length}</strong> open</div>
        <div class="stat"><strong>${done.length}</strong> done</div>`;

    if (!todos.length) {
        list.innerHTML = '<div class="empty">Nothing to do. Add something above.</div>';
        return;
    }

    const render = items => items.map(t => {
        const due = formatDue(t.due);
        const prio = t.priority ? PRIORITY_LABELS[t.priority] || `p${t.priority}` : '';
        return `
            <div class="todo-row${t.done ? ' done' : ''}" data-resource="${escapeAttr(t.resource)}">
                <input type="checkbox" class="todo-check" ${t.done ? 'checked' : ''}>
                <div class="todo-body">
                    <div class="todo-summary">${escapeHtml(t.summary)}</div>
                    <div class="todo-meta">
                        ${due ? `<span class="due">${escapeHtml(due)}</span>` : ''}
                        ${prio ? `<span class="badge">${escapeHtml(prio)}</span>` : ''}
                    </div>
                </div>
                <button class="todo-delete danger" title="Delete">&times;</button>
            </div>`;
    }).join('');

    list.innerHTML =
        (open.length ? `<div class="todo-group"><h3>Open</h3>${render(open)}</div>` : '') +
        (done.length ? `<div class="todo-group"><h3>Done</h3>${render(done)}</div>` : '');
}

async function loadTodos() {
    const list = document.getElementById('todo-list');
    list.innerHTML = '<div class="empty">Loading…</div>';
    try {
        const [todos, status] = await Promise.all([api('/api/todos'), api('/api/calendar/status')]);
        renderTodos(todos.todos);
        document.getElementById('todos-status').innerHTML = status.ok
            ? `<div class="stat"><span>Radicale</span><strong class="ok">connected</strong></div>
               <div class="stat muted">${escapeHtml(status.todo_calendar || '')}</div>`
            : `<div class="empty error">${escapeHtml(status.error || 'unreachable')}</div>`;
    } catch (err) {
        list.innerHTML = `<div class="empty error">Could not load to-dos: ${escapeHtml(err.message)}</div>`;
    }
}

async function addTodo() {
    const summaryInput = document.getElementById('todo-summary');
    const summary = summaryInput.value.trim();
    if (!summary) return;

    const due = document.getElementById('todo-due').value;
    const priority = document.getElementById('todo-priority').value;
    try {
        await api('/api/todos', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ summary, due: due || null, priority: priority ? Number(priority) : null }),
        });
        summaryInput.value = '';
        document.getElementById('todo-due').value = '';
        document.getElementById('todo-priority').value = '';
        showToast('Added to-dos');
        loadTodos();
    } catch (err) {
        showToast(`Could not add: ${err.message}`, true);
    }
}

document.getElementById('todo-add').addEventListener('click', addTodo);
document.getElementById('todo-summary').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') addTodo();
});
document.getElementById('todos-refresh').addEventListener('click', () => loadTodos());

document.getElementById('todo-list').addEventListener('change', async (e) => {
    if (!e.target.classList.contains('todo-check')) return;
    const row = e.target.closest('.todo-row');
    try {
        await api(`/api/todos/${row.dataset.resource}`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ done: e.target.checked }),
        });
        loadTodos();
    } catch (err) {
        e.target.checked = !e.target.checked;
        showToast(`Could not update: ${err.message}`, true);
    }
});

document.getElementById('todo-list').addEventListener('click', async (e) => {
    if (!e.target.classList.contains('todo-delete')) return;
    const row = e.target.closest('.todo-row');
    try {
        await api(`/api/todos/${row.dataset.resource}`, { method: 'DELETE' });
        showToast('Deleted');
        loadTodos();
    } catch (err) {
        showToast(`Could not delete: ${err.message}`, true);
    }
});

// --- Calendar ---

const calState = { year: null, month: null, events: [] };

function monthKey(year, month) {
    return `${year}-${String(month + 1).padStart(2, '0')}`;
}

function renderCalendarGrid(events) {
    const grid = document.getElementById('calendar-grid');
    const year = calState.year;
    const month = calState.month;

    document.getElementById('calendar-label').textContent =
        new Date(year, month, 1).toLocaleDateString(undefined, { month: 'long', year: 'numeric' });

    // Monday-first grid.
    const firstDay = new Date(year, month, 1);
    const lead = (firstDay.getDay() + 6) % 7;
    const daysInMonth = new Date(year, month + 1, 0).getDate();

    const byDay = {};
    events.forEach(ev => {
        const day = ev.start.slice(0, 10);
        (byDay[day] = byDay[day] || []).push(ev);
    });

    let html = ['<div class="cal-head">'].concat(
        ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
            .map(d => `<div class="cal-dow">${d}</div>`)
    ).concat(['</div><div class="cal-body">']).join('');

    const todayKey = new Date().toISOString().slice(0, 10);
    for (let i = 0; i < lead; i++) html += '<div class="cal-cell empty"></div>';
    for (let day = 1; day <= daysInMonth; day++) {
        const key = `${monthKey(year, month)}-${String(day).padStart(2, '0')}`;
        const items = byDay[key] || [];
        html += `<div class="cal-cell${key === todayKey ? ' today' : ''}">
            <div class="cal-daynum">${day}</div>
            <div class="cal-items">${items.map(ev =>
                `<div class="cal-item" title="${escapeAttr(ev.summary)}">${escapeHtml(ev.summary)}</div>`
            ).join('')}</div>
        </div>`;
    }
    grid.innerHTML = html + '</div>';

    document.getElementById('calendar-events').innerHTML = events.length
        ? events.map(ev => {
            const date = new Date(ev.start);
            const time = ev.all_day ? 'all day' : date.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
            return `<div class="cal-event">
                <div class="cal-event-time">${escapeHtml(date.toLocaleDateString(undefined, { month: 'short', day: 'numeric' }))} &middot; ${escapeHtml(time)}</div>
                <div class="cal-event-title">${escapeHtml(ev.summary)}</div>
                <div class="cal-event-cal muted">${escapeHtml(ev.calendar)}</div>
            </div>`;
        }).join('')
        : '<div class="empty">No events this month.</div>';
}

async function loadCalendar() {
    const grid = document.getElementById('calendar-grid');
    grid.innerHTML = '<div class="empty">Loading…</div>';

    const start = `${monthKey(calState.year, calState.month)}-01`;
    const lastDay = new Date(calState.year, calState.month + 1, 0).getDate();
    const end = `${monthKey(calState.year, calState.month)}-${String(lastDay).padStart(2, '0')}`;

    try {
        const [data, status] = await Promise.all([
            api(`/api/calendar/events?start=${start}&end=${end}`),
            api('/api/calendar/status'),
        ]);
        calState.events = data.events;
        renderCalendarGrid(data.events);
        document.getElementById('calendar-list').innerHTML = status.ok
            ? status.calendars.map(c => `<div class="stat"><span>${escapeHtml(c)}</span></div>`).join('')
            : `<div class="empty error">${escapeHtml(status.error || 'unreachable')}</div>`;
    } catch (err) {
        grid.innerHTML = `<div class="empty error">Could not load events: ${escapeHtml(err.message)}</div>`;
    }
}

function shiftMonth(delta) {
    const d = new Date(calState.year, calState.month + delta, 1);
    calState.year = d.getFullYear();
    calState.month = d.getMonth();
    loadCalendar();
}

document.getElementById('calendar-prev').addEventListener('click', () => shiftMonth(-1));
document.getElementById('calendar-next').addEventListener('click', () => shiftMonth(1));
document.getElementById('calendar-today').addEventListener('click', () => {
    const now = new Date();
    calState.year = now.getFullYear();
    calState.month = now.getMonth();
    loadCalendar();
});

// --- Init ---

const nowDate = new Date();
calState.year = nowDate.getFullYear();
calState.month = nowDate.getMonth();

loadTree();
openNote('meta/about.md').catch(() => {});
