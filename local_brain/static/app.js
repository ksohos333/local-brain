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
    renderTree(data.tree, treeEl);
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

async function openNote(path) {
    const data = await api(`/api/notes/${encodeURIComponent(path)}`);
    currentPath = path;
    await setEditorContent(data.content);
    notePath.textContent = path;
    loadBacklinks(path);
    updateTreeActive(path);
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

async function createNewNote() {
    const name = prompt('New note name (e.g. ideas/project-x):');
    if (!name) return;
    let path = name.endsWith('.md') ? name : `${name}.md`;
    if (!path.includes('/')) path = `pages/${path}`;
    try {
        await api('/api/notes', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ path }),
        });
    } catch (e) {
        // might exist, open anyway
    }
    await loadTree();
    await openNote(path);
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
    const chatActions = document.getElementById('chat-actions');

    if (tab === 'editor') {
        editorToolbar.style.display = 'flex';
        editorActions.style.display = 'flex';
        chatActions.style.display = 'none';
    } else if (tab === 'chat') {
        editorToolbar.style.display = 'none';
        editorActions.style.display = 'none';
        chatActions.style.display = 'flex';
        loadModels();
    } else {
        editorToolbar.style.display = 'none';
        editorActions.style.display = 'none';
        chatActions.style.display = 'none';
        if (tab === 'graph') renderGraph();
    }
}

// --- Graph ---

async function renderGraph() {
    const data = await api('/api/graph');
    const container = document.getElementById('graph-view');
    container.innerHTML = '';

    const width = container.clientWidth;
    const height = container.clientHeight;

    const svg = d3.select('#graph-view')
        .append('svg')
        .attr('width', width)
        .attr('height', height);

    const simulation = d3.forceSimulation(data.nodes)
        .force('link', d3.forceLink(data.links).id(d => d.id).distance(100))
        .force('charge', d3.forceManyBody().strength(-300))
        .force('center', d3.forceCenter(width / 2, height / 2));

    const link = svg.append('g')
        .selectAll('line')
        .data(data.links)
        .join('line')
        .attr('stroke', '#475569')
        .attr('stroke-width', 1.5);

    const node = svg.append('g')
        .selectAll('g')
        .data(data.nodes)
        .join('g')
        .call(d3.drag()
            .on('start', dragstarted)
            .on('drag', dragged)
            .on('end', dragended));

    node.append('circle')
        .attr('r', 6)
        .attr('fill', d => d.group === 'pages' ? '#38bdf8' : '#a78bfa');

    node.append('text')
        .text(d => d.title)
        .attr('x', 10)
        .attr('y', 4)
        .attr('fill', '#e2e8f0')
        .attr('font-size', '12px');

    node.on('click', (_event, d) => openNote(d.path));

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
document.getElementById('new-note-btn').onclick = createNewNote;
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

// --- Init ---

loadTree();
openNote('meta/about.md').catch(() => {});
