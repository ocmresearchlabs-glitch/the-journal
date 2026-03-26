(function(){
'use strict';
const { useState, useEffect, useMemo, useRef } = React;
const FALLBACK_CATS = [
    { id: 1, slug: 'foundations', name: 'Foundations of Physics', emoji: '🌌' },
    { id: 2, slug: 'math-physics', name: 'Mathematical Physics', emoji: '📐' },
    { id: 3, slug: 'nonlinear', name: 'Nonlinear Dynamics', emoji: '🌀' },
    { id: 4, slug: 'stat-mech', name: 'Statistical Mechanics', emoji: '⚛️' },
    { id: 5, slug: 'complex', name: 'Complex Systems', emoji: '🕸️' },
    { id: 6, slug: 'experimental', name: 'Experimental & Observational', emoji: '🔬' }
];
const DEFAULT_USER = { id: 0, email: 'guest', display_name: 'Guest', initials: 'GU', bio: '', orcid: '', reputation_score: 0, paper_count: 0, review_count: 0, follower_count: 0, avatar_color: '#5ea8ff', joined: null, role: 'guest' };
const TOOL_DEFS = [
    { id: 'desk_review', title: 'AI Desk Review', emoji: '✦', color: '#38bdf8', desc: 'Run the 8-criterion rubric on manuscript text before you submit.' },
    { id: 'ocm', title: 'OCM Stability Analysis', emoji: '📈', color: '#5ea8ff', desc: 'Contraction-rate analysis for time-series data.' },
    { id: 'er', title: 'ER Topology Mapping', emoji: '🌉', color: '#a78bfa', desc: 'Coherence graph mapping and bridge detection.' },
    { id: 'icm', title: 'ICM Invariant Detection', emoji: '🧪', color: '#f0a030', desc: 'Disruption morphology and admissibility gates.' },
    { id: 'clm', title: 'CLM Coherence Field Lab', emoji: 'ψ', color: '#c084fc', desc: 'Coherence-field simulation under structured forcing.' }
];
const API = {
    get(url) { return new Promise(function (resolve) { const x = new XMLHttpRequest(); x.open('GET', url, true); x.withCredentials = true; x.onload = function () { try {
        resolve(JSON.parse(x.responseText));
    }
    catch (e) {
        resolve({});
    } }; x.onerror = function () { resolve({ error: 'Network error' }); }; x.send(); }); },
    post(url, body) { return new Promise(function (resolve) { const x = new XMLHttpRequest(); x.open('POST', url, true); x.withCredentials = true; x.setRequestHeader('Content-Type', 'application/json'); x.onload = function () { try {
        resolve(JSON.parse(x.responseText));
    }
    catch (e) {
        resolve({});
    } }; x.onerror = function () { resolve({ error: 'Network error' }); }; x.send(JSON.stringify(body || {})); }); },
    put(url, body) { return new Promise(function (resolve) { const x = new XMLHttpRequest(); x.open('PUT', url, true); x.withCredentials = true; x.setRequestHeader('Content-Type', 'application/json'); x.onload = function () { try {
        resolve(JSON.parse(x.responseText));
    }
    catch (e) {
        resolve({});
    } }; x.onerror = function () { resolve({ error: 'Network error' }); }; x.send(JSON.stringify(body || {})); }); },
    del(url) { return new Promise(function (resolve) { const x = new XMLHttpRequest(); x.open('DELETE', url, true); x.withCredentials = true; x.onload = function () { try {
        resolve(JSON.parse(x.responseText));
    }
    catch (e) {
        resolve({});
    } }; x.onerror = function () { resolve({ error: 'Network error' }); }; x.send(); }); },
    postForm(url, fd) { return new Promise(function (resolve) { const x = new XMLHttpRequest(); x.open('POST', url, true); x.withCredentials = true; x.onload = function () { try {
        resolve(JSON.parse(x.responseText));
    }
    catch (e) {
        resolve({ error: 'Parse error' });
    } }; x.onerror = function () { resolve({ error: 'Network error' }); }; x.send(fd); }); }
};
function timeAgo(d) { if (!d)
    return ''; const s = Math.floor((Date.now() - new Date(d)) / 1000); if (s < 60)
    return 'just now'; if (s < 3600)
    return Math.floor(s / 60) + 'm ago'; if (s < 86400)
    return Math.floor(s / 3600) + 'h ago'; if (s < 604800)
    return Math.floor(s / 86400) + 'd ago'; return new Date(d).toLocaleDateString(); }
function formatDate(d) { if (!d)
    return ''; return new Date(d).toLocaleDateString('en-US', { year: 'numeric', month: 'short', day: 'numeric' }); }
function initialsForUser(user) { const name = (user && user.display_name) || '??'; return name.split(' ').map(function (w) { return w[0] || ''; }).join('').toUpperCase().slice(0, 2) || '??'; }
function normalizeOrcid(orcid) { if (!orcid)
    return ''; const clean = String(orcid).trim().replace(/https?:\/\/orcid\.org\//i, '').replace(/[^0-9Xx-]/g, ''); return clean; }
function orcidUrl(orcid) { const n = normalizeOrcid(orcid); return n ? 'https://orcid.org/' + n : ''; }
function copyText(text) { if (navigator.clipboard && navigator.clipboard.writeText) {
    return navigator.clipboard.writeText(text);
} const ta = document.createElement('textarea'); ta.value = text; document.body.appendChild(ta); ta.select(); document.execCommand('copy'); document.body.removeChild(ta); return Promise.resolve(); }
function formatBibTeX(p) { const author = (p.author && p.author.display_name) || 'Anonymous'; const year = (p.published_at || p.created_at || '').slice(0, 4) || new Date().getFullYear(); const key = (author.split(' ')[0] || 'journal').toLowerCase() + year; return '@article{' + key + ',\n  title = {' + (p.title || 'Untitled') + '},\n  author = {' + author + '},\n  journal = {The Journal},\n  year = {' + year + '},\n  url = {' + window.location.origin + '}\n}'; }
function similarityScore(a, b) { const words = function (x) { return String(x || '').toLowerCase().replace(/[^a-z0-9\s]/g, ' ').split(/\s+/).filter(Boolean); }; const aw = new Set(words((a.title || '') + ' ' + (a.abstract || ''))); const bw = words((b.title || '') + ' ' + (b.abstract || '')); let hit = 0; bw.forEach(function (w) { if (aw.has(w))
    hit++; }); return hit; }
function HIcon({ d, s = 18, f }) { return React.createElement("svg", { width: s, height: s, viewBox: "0 0 24 24", fill: f ? 'currentColor' : 'none', stroke: "currentColor", strokeWidth: "2", strokeLinecap: "round", strokeLinejoin: "round" },
    React.createElement("path", { d: d })); }
function Av({ user, size = 36 }) { const u = user || DEFAULT_USER; const text = u.initials || initialsForUser(u); const c = u.avatar_color || '#5ea8ff'; return React.createElement("div", { style: { width: size, height: size, borderRadius: '50%', background: 'linear-gradient(135deg,' + c + ',' + c + '88)', display: 'flex', alignItems: 'center', justifyContent: 'center', color: '#fff', fontWeight: 700, fontSize: size * .36, flexShrink: 0 } }, text); }
function Badge({ label, color }) { const c = color || '#6b7db3'; return React.createElement("span", { style: { display: 'inline-flex', alignItems: 'center', gap: 4, padding: '2px 8px', borderRadius: 12, fontSize: 10, fontWeight: 700, background: c + '15', color: c, border: '1px solid ' + c + '30' } },
    React.createElement("span", { style: { width: 5, height: 5, borderRadius: '50%', background: c } }),
    label); }
function MetricCard({ label, value, color }) { return React.createElement("div", { style: { padding: '12px 14px', borderRadius: 12, background: 'rgba(255,255,255,0.03)', border: '1px solid rgba(255,255,255,0.07)', flex: '1 1 140px', minWidth: 120 } },
    React.createElement("div", { style: { fontSize: 10, fontWeight: 700, color: color || '#5a6a94', textTransform: 'uppercase', letterSpacing: '0.06em', marginBottom: 4 } }, label),
    React.createElement("div", { style: { fontSize: 22, fontWeight: 800, color: '#edf1ff' } }, value)); }
function GateIndicator({ label, passed }) { return React.createElement("div", { style: { display: 'flex', alignItems: 'center', gap: 8, padding: '4px 0' } },
    React.createElement("div", { style: { width: 16, height: 16, borderRadius: '50%', background: passed ? 'rgba(74,222,128,0.14)' : 'rgba(239,68,68,0.14)', border: '1.5px solid ' + (passed ? '#4ade80' : '#ef4444'), display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: 10, color: passed ? '#4ade80' : '#ef4444' } }, passed ? '✓' : '✕'),
    React.createElement("span", { style: { fontSize: 12, color: passed ? '#a0d8b0' : '#d49a9a' } }, label)); }
function SparkLine({ data, height = 80, stroke = '#5ea8ff' }) { if (!data || !data.length)
    return null; const width = 320; const min = Math.min.apply(null, data); const max = Math.max.apply(null, data); const range = (max - min) || 1; const pts = data.map(function (v, i) { const x = (i / Math.max(1, data.length - 1)) * width; const y = height - 6 - ((v - min) / range) * (height - 12); return x + ',' + y; }).join(' '); return React.createElement("svg", { width: "100%", viewBox: '0 0 ' + width + ' ' + height, style: { display: 'block' } },
    React.createElement("polyline", { points: pts, fill: "none", stroke: stroke, strokeWidth: "2", strokeLinejoin: "round", strokeLinecap: "round" })); }
function DualSpark({ a, b, height = 90 }) { if (!a || !a.length)
    return null; const width = 320; const joined = (a || []).concat(b || []); const min = Math.min.apply(null, joined); const max = Math.max.apply(null, joined); const range = (max - min) || 1; const mk = function (data) { return data.map(function (v, i) { const x = (i / Math.max(1, data.length - 1)) * width; const y = height - 6 - ((v - min) / range) * (height - 12); return x + ',' + y; }).join(' '); }; return React.createElement("svg", { width: "100%", viewBox: '0 0 ' + width + ' ' + height, style: { display: 'block' } },
    React.createElement("polyline", { points: mk(a), fill: "none", stroke: "#5ea8ff", strokeWidth: "2", strokeLinejoin: "round", strokeLinecap: "round" }),
    React.createElement("polyline", { points: mk(b || []), fill: "none", stroke: "#a78bfa", strokeWidth: "1.5", strokeLinejoin: "round", strokeLinecap: "round", opacity: "0.9" })); }
function ToolResultView({ result }) {
    if (!result)
        return null;
    if (result.error)
        return React.createElement("div", { style: { fontSize: 13, color: '#ef4444' } }, result.error);
    const d = result.details || {};
    const cls = result.classification || '';
    const clsColor = cls === 'DRIVEN-DISSIPATIVE' || cls === 'ADMISSIBLE' || cls === 'ELIGIBLE' || cls === 'PASS' ? '#4ade80' : cls === 'AUTONOMOUS CHAOTIC' || cls === 'INADMISSIBLE' || cls === 'SURGE' || cls === 'BLOCK' ? '#ef4444' : cls === 'BRIDGED' || cls === 'CONNECTED' ? '#5ea8ff' : '#f0a030';
    const isDesk = d.scores && typeof d.scores.scope === 'number';
    return React.createElement("div", null,
        cls && React.createElement("div", { style: { marginBottom: 12 } },
            React.createElement(Badge, { label: cls, color: clsColor })),
        result.summary && React.createElement("div", { style: { fontSize: 13, color: '#c7d2f0', lineHeight: 1.6, marginBottom: 14 } }, result.summary),
        typeof d.contraction_rate === 'number' && React.createElement("div", null,
            React.createElement("div", { style: { display: 'flex', gap: 8, flexWrap: 'wrap', marginBottom: 12 } },
                React.createElement(MetricCard, { label: "Contraction Rate", value: d.contraction_rate.toFixed(6), color: "#5ea8ff" }),
                React.createElement(MetricCard, { label: "Z Statistic", value: String(d.z_statistic), color: "#a78bfa" }),
                React.createElement(MetricCard, { label: "Points", value: String(d.points), color: "#6b7db3" })),
            React.createElement("div", { style: { padding: '10px 12px', borderRadius: 12, background: 'rgba(255,255,255,0.02)', border: '1px solid rgba(255,255,255,0.05)', marginBottom: 12 } },
                React.createElement("div", { style: { fontSize: 11, fontWeight: 700, color: '#6b7db3', marginBottom: 6 } }, "TRACE VS BASELINE"),
                React.createElement(DualSpark, { a: d.series_preview || [], b: d.baseline_preview || [] })),
            React.createElement("div", { style: { fontSize: 12, color: '#8a9ac4' } },
                "Threshold: ",
                d.threshold)),
        typeof d.nodes === 'number' && React.createElement("div", null,
            React.createElement("div", { style: { display: 'flex', gap: 8, flexWrap: 'wrap', marginBottom: 12 } },
                React.createElement(MetricCard, { label: "Nodes", value: String(d.nodes), color: "#a78bfa" }),
                React.createElement(MetricCard, { label: "Edges", value: String(d.edges), color: "#5ea8ff" }),
                React.createElement(MetricCard, { label: "Bridges", value: String(d.bridges), color: d.bridges > 0 ? '#f0a030' : '#6b7db3' }),
                React.createElement(MetricCard, { label: "Density", value: d.density.toFixed(4), color: "#6b7db3" })),
            React.createElement("div", { style: { display: 'flex', gap: 8, flexWrap: 'wrap', marginBottom: 12 } },
                React.createElement(MetricCard, { label: "Min Coherence", value: d.min_coherence.toFixed(3), color: "#6b7db3" }),
                React.createElement(MetricCard, { label: "Mean Coherence", value: d.mean_coherence.toFixed(3), color: "#5ea8ff" })),
            React.createElement("div", { style: { padding: '10px 12px', borderRadius: 12, background: 'rgba(255,255,255,0.02)', border: '1px solid rgba(255,255,255,0.05)' } },
                React.createElement(SparkLine, { data: d.series_preview || [], stroke: "#a78bfa" }))),
        typeof d.events === 'number' && React.createElement("div", null,
            React.createElement("div", { style: { display: 'flex', gap: 8, flexWrap: 'wrap', marginBottom: 12 } },
                React.createElement(MetricCard, { label: "Events", value: String(d.events), color: "#f0a030" }),
                React.createElement(MetricCard, { label: "Morphology", value: d.morphology, color: "#a78bfa" }),
                React.createElement(MetricCard, { label: "Regime", value: d.regime, color: "#5ea8ff" })),
            React.createElement("div", { style: { display: 'flex', gap: 8, flexWrap: 'wrap', marginBottom: 12 } },
                React.createElement(MetricCard, { label: "Mean Amplitude", value: String(d.mean_amplitude), color: "#6b7db3" }),
                React.createElement(MetricCard, { label: "Mean Recovery", value: String(d.mean_recovery), color: "#6b7db3" }),
                React.createElement(MetricCard, { label: "Resonance R", value: String(d.resonance_R), color: "#5ea8ff" })),
            d.gates && React.createElement("div", { style: { padding: '12px 14px', borderRadius: 12, background: 'rgba(255,255,255,0.02)', border: '1px solid rgba(255,255,255,0.05)', marginBottom: 12 } },
                React.createElement("div", { style: { fontSize: 11, fontWeight: 700, color: '#5ea8ff', marginBottom: 6 } }, "ADMISSIBILITY GATES"),
                React.createElement(GateIndicator, { label: "A1: Events detected", passed: d.gates.A1 }),
                React.createElement(GateIndicator, { label: "A2: Regime is weak or resonant", passed: d.gates.A2 }),
                React.createElement(GateIndicator, { label: "A3: Recovery time bounded", passed: d.gates.A3 }),
                React.createElement(GateIndicator, { label: "A4: Amplitude CV bounded", passed: d.gates.A4 })),
            React.createElement("div", { style: { padding: '10px 12px', borderRadius: 12, background: 'rgba(255,255,255,0.02)', border: '1px solid rgba(255,255,255,0.05)' } },
                React.createElement(SparkLine, { data: d.series_preview || [], stroke: "#f0a030" }))),
        typeof d.kappa_w === 'number' && React.createElement("div", null,
            React.createElement("div", { style: { display: 'flex', gap: 8, flexWrap: 'wrap', marginBottom: 12 } },
                React.createElement(MetricCard, { label: "Kappa-W", value: d.kappa_w.toFixed(4), color: "#c084fc" }),
                React.createElement(MetricCard, { label: "Glue Error", value: d.glue_error.toFixed(4), color: "#5ea8ff" }),
                React.createElement(MetricCard, { label: "Stability", value: d.stability.toFixed(4), color: d.stability >= 0.5 ? '#4ade80' : '#f0a030' })),
            React.createElement("div", { style: { display: 'flex', gap: 8, flexWrap: 'wrap', marginBottom: 12 } },
                React.createElement(MetricCard, { label: "P-RMS", value: d.prms.toFixed(4), color: "#6b7db3" }),
                React.createElement(MetricCard, { label: "Recov. Integral", value: d.ri.toFixed(4), color: "#a78bfa" })),
            React.createElement("div", { style: { display: 'grid', gridTemplateColumns: '1fr', gap: 10 } },
                React.createElement("div", { style: { padding: '10px 12px', borderRadius: 12, background: 'rgba(255,255,255,0.02)', border: '1px solid rgba(255,255,255,0.05)' } },
                    React.createElement("div", { style: { fontSize: 11, fontWeight: 700, color: '#6b7db3', marginBottom: 6 } }, "KAPPA-W HISTORY"),
                    React.createElement(SparkLine, { data: d.hist_kw || [], stroke: "#c084fc" })),
                React.createElement("div", { style: { padding: '10px 12px', borderRadius: 12, background: 'rgba(255,255,255,0.02)', border: '1px solid rgba(255,255,255,0.05)' } },
                    React.createElement("div", { style: { fontSize: 11, fontWeight: 700, color: '#6b7db3', marginBottom: 6 } }, "GLUE ERROR HISTORY"),
                    React.createElement(SparkLine, { data: d.hist_ge || [], stroke: "#5ea8ff" })))),
        isDesk && React.createElement("div", null,
            React.createElement("div", { style: { display: 'flex', gap: 8, flexWrap: 'wrap', marginBottom: 12 } }, Object.entries(d.scores).map(function (entry) { const label = entry[0].replace(/_/g, ' ').toUpperCase(); const v = entry[1]; const color = v >= 4 ? '#4ade80' : v >= 3 ? '#5ea8ff' : v >= 2 ? '#f0a030' : '#ef4444'; return React.createElement(MetricCard, { key: entry[0], label: label, value: String(v) + '/5', color: color }); })),
            typeof d.overall_score === 'number' && React.createElement("div", { style: { padding: '10px 14px', borderRadius: 12, background: 'rgba(94,168,255,0.06)', border: '1px solid rgba(94,168,255,0.12)', fontSize: 13, color: '#c7d2f0', marginBottom: 10 } },
                "Overall: ",
                React.createElement("strong", null,
                    d.overall_score,
                    "%"),
                " | Recommendation: ",
                React.createElement("strong", null, String(d.recommendation || '').toUpperCase())),
            d.strengths && d.strengths.length > 0 && React.createElement("div", { style: { marginBottom: 10 } },
                React.createElement("div", { style: { fontSize: 11, fontWeight: 700, color: '#4ade80', marginBottom: 4 } }, "Strengths"),
                d.strengths.map(function (s, i) { return React.createElement("div", { key: i, style: { fontSize: 12, color: '#a0d8b0', marginBottom: 2 } }, s); })),
            d.suggestions && d.suggestions.length > 0 && React.createElement("div", { style: { marginBottom: 10 } },
                React.createElement("div", { style: { fontSize: 11, fontWeight: 700, color: '#5ea8ff', marginBottom: 4 } }, "Suggestions"),
                d.suggestions.map(function (s, i) { return React.createElement("div", { key: i, style: { fontSize: 12, color: '#8fb6e6', marginBottom: 2 } }, s); })),
            d.encouragement && React.createElement("div", { style: { fontSize: 12, color: '#6b7db3', fontStyle: 'italic' } }, d.encouragement)),
        !isDesk && typeof d === 'object' && !d.contraction_rate && !d.nodes && !d.events && !d.kappa_w && Object.keys(d).length > 0 && React.createElement("pre", { style: { fontSize: 12, color: '#8a9ac4', whiteSpace: 'pre-wrap' } }, JSON.stringify(d, null, 2)));
}
function AuthScreen({ onAuth }) {
    const [mode, setMode] = useState('login');
    const [email, setEmail] = useState('');
    const [password, setPassword] = useState('');
    const [displayName, setDisplayName] = useState('');
    const [error, setError] = useState('');
    const [loading, setLoading] = useState(false);
    function submit() {
        setError('');
        setLoading(true);
        const url = mode === 'login' ? '/api/auth/login' : '/api/auth/register';
        const payload = mode === 'login' ? { email: email.trim(), password: password } : { email: email.trim(), password: password, display_name: displayName.trim() || email.split('@')[0] };
        API.post(url, payload).then(function (res) {
            setLoading(false);
            if (res && res.user) {
                onAuth(res.user, true);
                return;
            }
            setError((res && res.error) || 'Authentication failed');
        });
    }
    return React.createElement("div", { style: { minHeight: '100vh', minHeight: '100dvh', background: 'linear-gradient(180deg,#080c18,#0d1225 30%,#0a0f1e)', color: '#edf1ff', display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', padding: 'max(24px, env(safe-area-inset-top)) max(18px, env(safe-area-inset-right)) 40px max(18px, env(safe-area-inset-left))' } },
        React.createElement("div", { style: { width: 52, height: 52, borderRadius: 13, background: 'linear-gradient(135deg,#5ea8ff,#3d70b8)', display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: 26, fontWeight: 800, color: '#fff', marginBottom: 20 } }, "\u29C7"),
        React.createElement("h1", { style: { fontSize: 24, fontWeight: 800, marginBottom: 4 } }, "The Journal"),
        React.createElement("p", { style: { fontSize: 13, color: '#5ea8ff', marginBottom: 24 } }, "Independent Physics Platform"),
        React.createElement("div", { style: { width: '100%', maxWidth: 440, margin: '0 auto' } },
            React.createElement("div", { style: { display: 'flex', gap: 3, padding: 3, borderRadius: 10, background: 'rgba(255,255,255,0.03)', marginBottom: 16 } },
                React.createElement("button", { onClick: () => setMode('login'), style: { flex: 1, padding: '8px', borderRadius: 8, border: 'none', cursor: 'pointer', fontSize: 12, fontWeight: 700, background: mode === 'login' ? 'rgba(94,168,255,0.12)' : 'transparent', color: mode === 'login' ? '#5ea8ff' : '#4a5a7e' } }, "Sign In"),
                React.createElement("button", { onClick: () => setMode('register'), style: { flex: 1, padding: '8px', borderRadius: 8, border: 'none', cursor: 'pointer', fontSize: 12, fontWeight: 700, background: mode === 'register' ? 'rgba(94,168,255,0.12)' : 'transparent', color: mode === 'register' ? '#5ea8ff' : '#4a5a7e' } }, "Create Account")),
            React.createElement("div", { style: { display: 'flex', flexDirection: 'column', gap: 10 } },
                mode === 'register' && React.createElement("input", { value: displayName, onChange: (e) => setDisplayName(e.target.value), placeholder: 'Display name', style: { width: '100%', padding: '11px 14px', borderRadius: 10, border: '1px solid rgba(255,255,255,0.08)', background: 'rgba(255,255,255,0.03)', color: '#edf1ff', fontSize: 14, boxSizing: 'border-box' } }),
                React.createElement("input", { value: email, onChange: (e) => setEmail(e.target.value), placeholder: 'Email', type: 'email', style: { width: '100%', padding: '11px 14px', borderRadius: 10, border: '1px solid rgba(255,255,255,0.08)', background: 'rgba(255,255,255,0.03)', color: '#edf1ff', fontSize: 14, boxSizing: 'border-box' } }),
                React.createElement("input", { value: password, onChange: (e) => setPassword(e.target.value), placeholder: 'Password', type: 'password', onKeyDown: (e) => { if (e.key === 'Enter')
                        submit(); }, style: { width: '100%', padding: '11px 14px', borderRadius: 10, border: '1px solid rgba(255,255,255,0.08)', background: 'rgba(255,255,255,0.03)', color: '#edf1ff', fontSize: 14, boxSizing: 'border-box' } }),
                error && React.createElement("div", { style: { fontSize: 12, color: '#ef4444', padding: '6px 10px', borderRadius: 8, background: 'rgba(239,68,68,0.08)' } }, error),
                React.createElement("button", { onClick: submit, disabled: loading || !email || !password || (mode === 'register' && !displayName.trim()), style: { padding: '12px', borderRadius: 10, border: 'none', fontSize: 14, fontWeight: 700, cursor: 'pointer', background: 'linear-gradient(135deg,#5ea8ff,#3d8be0)', color: '#071120' } }, loading ? '...' : (mode === 'login' ? 'Sign In' : 'Create Account'))),
            React.createElement("button", { onClick: () => onAuth(DEFAULT_USER, false), style: { display: 'block', width: '100%', marginTop: 12, padding: '10px', borderRadius: 10, border: '1px solid rgba(255,255,255,0.06)', background: 'transparent', color: '#4a5a7e', fontSize: 12, fontWeight: 600, cursor: 'pointer' } }, "Continue as guest"),
            React.createElement("p", { style: { fontSize: 11, color: '#3a4a6e', textAlign: 'center', marginTop: 16, lineHeight: 1.5 } }, "No degree required. No institution needed. Just curiosity.")));
}
function NotificationDropdown({ items, onClose, onMarkRead, onOpenProfile }) {
    return React.createElement("div", { style: { position: 'absolute', right: 0, top: 'calc(100% + 10px)', width: 'min(310px, calc(100vw - 24px))', maxHeight: 360, overflowY: 'auto', background: '#0d1225', border: '1px solid rgba(255,255,255,0.1)', borderRadius: 14, boxShadow: '0 18px 40px rgba(0,0,0,0.35)', padding: 10, zIndex: 200 } },
        React.createElement("div", { style: { display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '4px 6px 10px' } },
            React.createElement("div", { style: { fontSize: 13, fontWeight: 800 } }, "Notifications"),
            React.createElement("button", { onClick: onMarkRead, style: { background: 'none', border: 'none', color: '#5ea8ff', fontSize: 11, fontWeight: 700, cursor: 'pointer' } }, "Mark all read")),
        items.length === 0 && React.createElement("div", { style: { padding: '14px 10px', fontSize: 12, color: '#6b7db3' } }, "No notifications yet."),
        items.map(function (n) { return React.createElement("button", { key: n.id, onClick: function () { if (!n.is_read && onMarkRead)
                onMarkRead(n.id); onClose(); }, style: { width: '100%', textAlign: 'left', padding: '10px 12px', borderRadius: 12, border: '1px solid ' + (n.is_read ? 'rgba(255,255,255,0.05)' : 'rgba(94,168,255,0.14)'), background: n.is_read ? 'rgba(255,255,255,0.02)' : 'rgba(94,168,255,0.06)', color: '#edf1ff', cursor: 'pointer', marginBottom: 8 } },
            React.createElement("div", { style: { fontSize: 12, fontWeight: 700, marginBottom: 2 } }, n.title),
            n.body && React.createElement("div", { style: { fontSize: 11, color: '#8a9ac4', lineHeight: 1.4 } }, n.body),
            React.createElement("div", { style: { fontSize: 10, color: '#4a5a7e', marginTop: 4 } }, timeAgo(n.created_at))); }));
}
function PaperCard({ paper, onOpen, onProfile, onLike }) {
    const [liked, setLiked] = useState(!!paper.user_liked);
    const [likeCount, setLikeCount] = useState(paper.like_count || 0);
    const author = paper.author || {};
    return React.createElement("div", { onClick: () => onOpen && onOpen(paper), style: { background: 'rgba(255,255,255,0.03)', border: '1px solid rgba(255,255,255,0.07)', borderRadius: 14, padding: '16px 18px', cursor: 'pointer', marginBottom: 10 } },
        React.createElement("div", { style: { display: 'flex', alignItems: 'center', gap: 10, marginBottom: 10 } },
            React.createElement("div", { onClick: (e) => { e.stopPropagation(); onProfile && onProfile(author); }, style: { cursor: 'pointer' } },
                React.createElement(Av, { user: author, size: 32 })),
            React.createElement("div", { style: { flex: 1, minWidth: 0 } },
                React.createElement("div", { style: { display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap' } },
                    React.createElement("span", { onClick: (e) => { e.stopPropagation(); onProfile && onProfile(author); }, style: { fontWeight: 600, fontSize: 13, cursor: 'pointer' } }, author.display_name || 'Anonymous'),
                    author.orcid && React.createElement("a", { href: orcidUrl(author.orcid), target: "_blank", rel: "noreferrer", onClick: (e) => e.stopPropagation(), style: { fontSize: 10, fontWeight: 700, color: '#4ade80', textDecoration: 'none', border: '1px solid rgba(74,222,128,0.25)', padding: '1px 6px', borderRadius: 999 } }, "ORCID"),
                    React.createElement("span", { style: { color: '#5a6a94', fontSize: 11 } }, timeAgo(paper.created_at))),
                paper.category && React.createElement("span", { style: { fontSize: 11, color: '#6b7db3' } },
                    paper.category.emoji,
                    " ",
                    paper.category.name)),
            React.createElement(Badge, { label: paper.status_label || 'Submitted', color: paper.status_color || '#6b7db3' })),
        React.createElement("h3", { style: { fontSize: 16, fontWeight: 700, lineHeight: 1.3, margin: '0 0 6px', color: '#f0f4ff' } }, paper.title),
        React.createElement("p", { style: { fontSize: 13, lineHeight: 1.55, color: '#8a9ac4', margin: '0 0 10px', overflow: 'hidden', display: '-webkit-box', WebkitLineClamp: 3, WebkitBoxOrient: 'vertical' } }, paper.abstract),
        paper.tags && paper.tags.length > 0 && React.createElement("div", { style: { display: 'flex', gap: 4, flexWrap: 'wrap', marginBottom: 10 } }, paper.tags.map(function (t) { return React.createElement("span", { key: t, style: { padding: '1px 7px', borderRadius: 6, fontSize: 10, fontWeight: 600, background: 'rgba(141,193,255,0.08)', color: '#7da8d4' } }, t); })),
        React.createElement("div", { style: { display: 'flex', alignItems: 'center', gap: 16, borderTop: '1px solid rgba(255,255,255,0.05)', paddingTop: 10 } },
            React.createElement("button", { onClick: (e) => { e.stopPropagation(); if (onLike) {
                    onLike(paper, liked, likeCount, function (ok, nextLiked, nextCount) { if (ok) {
                        setLiked(nextLiked);
                        setLikeCount(nextCount);
                    } });
                } }, style: { display: 'flex', alignItems: 'center', gap: 4, background: 'none', border: 'none', cursor: 'pointer', color: liked ? '#ff4d6a' : '#5a6a94', fontSize: 12, fontWeight: 600 } },
                React.createElement(HIcon, { d: 'M20.84 4.61a5.5 5.5 0 0 0-7.78 0L12 5.67l-1.06-1.06a5.5 5.5 0 0 0-7.78 7.78l1.06 1.06L12 21.23l7.78-7.78 1.06-1.06a5.5 5.5 0 0 0 0-7.78z', s: 15, f: liked }),
                " ",
                likeCount),
            React.createElement("span", { style: { display: 'flex', alignItems: 'center', gap: 4, color: '#5a6a94', fontSize: 12, fontWeight: 600 } },
                React.createElement(HIcon, { d: 'M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z', s: 15 }),
                " ",
                paper.comment_count || 0),
            React.createElement("span", { style: { display: 'flex', alignItems: 'center', gap: 4, color: '#5a6a94', fontSize: 12, fontWeight: 600 } },
                React.createElement(HIcon, { d: 'M12 2l3.09 6.26L22 9.27l-5 4.87 1.18 6.88L12 17.77l-6.18 3.25L7 14.14l-5-4.87 6.91-1.01L12 2', s: 15 }),
                " ",
                paper.review_count || 0)));
}
function FeedScreen({ tab, push, useApi, categories, onLike, onOpenProfile, onPool }) {
    const [papers, setPapers] = useState([]);
    const [loading, setLoading] = useState(true);
    const [search, setSearch] = useState('');
    const [categoryId, setCategoryId] = useState(null);
    const [suggestions, setSuggestions] = useState([]);
    const [showSuggestions, setShowSuggestions] = useState(false);
    useEffect(function () {
        setLoading(true);
        const params = [];
        if (categoryId)
            params.push('category_id=' + encodeURIComponent(categoryId));
        if (search.trim())
            params.push('q=' + encodeURIComponent(search.trim()));
        const url = (tab === 'published' ? '/api/feed/published' : '/api/feed/discovery') + (params.length ? '?' + params.join('&') : '');
        API.get(url).then(function (res) { var next = (res && res.papers) || []; setPapers(next); if (onPool)
            onPool(next); setLoading(false); });
    }, [tab, categoryId, search]);
    useEffect(function () {
        if (search.trim().length < 2) {
            setSuggestions([]);
            return;
        }
        const id = setTimeout(function () { API.get('/api/search/suggest?q=' + encodeURIComponent(search.trim())).then(function (res) { setSuggestions((res && res.suggestions) || []); setShowSuggestions(true); }); }, 180);
        return function () { clearTimeout(id); };
    }, [search]);
    return React.createElement("div", { style: { animation: 'fadeIn .25s' } },
        React.createElement("div", { style: { marginBottom: 14, padding: '14px 16px', borderRadius: 12, background: 'linear-gradient(135deg,rgba(94,168,255,0.05),rgba(94,168,255,0.02))', border: '1px solid rgba(94,168,255,0.08)' } },
            React.createElement("p", { style: { fontSize: 13, color: '#8a9ac4', margin: 0, lineHeight: 1.5 } },
                React.createElement("span", { style: { color: '#5ea8ff', fontWeight: 700 } }, tab === 'published' ? 'Published research.' : 'Discovery feed.'),
                " ",
                tab === 'published' ? 'Peer-vetted papers promoted by the editorial process.' : 'Community-facing papers that have cleared the admin desk and are in discovery or active review.')),
        React.createElement("div", { style: { position: 'relative', marginBottom: 12 } },
            React.createElement("input", { value: search, onChange: (e) => setSearch(e.target.value), onFocus: () => setShowSuggestions(true), placeholder: 'Search papers, abstracts, or authors...', style: { width: '100%', padding: '10px 12px 10px 34px', borderRadius: 10, border: '1px solid rgba(255,255,255,0.07)', background: 'rgba(255,255,255,0.02)', color: '#edf1ff', fontSize: 13 } }),
            React.createElement("div", { style: { position: 'absolute', left: 10, top: '50%', transform: 'translateY(-50%)', color: '#3a4a6e' } },
                React.createElement(HIcon, { d: 'M21 21l-4.35-4.35M11 19a8 8 0 1 0 0-16 8 8 0 0 0 0 16z', s: 14 })),
            showSuggestions && suggestions.length > 0 && React.createElement("div", { style: { position: 'absolute', left: 0, right: 0, top: 'calc(100% + 6px)', background: '#0d1225', border: '1px solid rgba(255,255,255,0.08)', borderRadius: 12, overflow: 'hidden', zIndex: 40 } }, suggestions.map(function (s) { return React.createElement("button", { key: s.blind_id, onClick: () => { setShowSuggestions(false); push('paper', s.blind_id); }, style: { display: 'block', width: '100%', textAlign: 'left', padding: '10px 12px', background: 'transparent', border: 'none', borderBottom: '1px solid rgba(255,255,255,0.04)', color: '#edf1ff', cursor: 'pointer' } },
                React.createElement("div", { style: { fontSize: 12, fontWeight: 700 } }, s.title),
                React.createElement("div", { style: { fontSize: 11, color: '#6b7db3' } }, s.author_name)); }))),
        React.createElement("div", { style: { display: 'flex', gap: 5, overflowX: 'auto', marginBottom: 12, WebkitOverflowScrolling: 'touch' } },
            React.createElement("button", { onClick: () => setCategoryId(null), style: { padding: '5px 10px', borderRadius: 8, border: '1px solid', fontSize: 11, fontWeight: 700, cursor: 'pointer', whiteSpace: 'nowrap', flexShrink: 0, background: !categoryId ? 'rgba(94,168,255,0.1)' : 'transparent', borderColor: !categoryId ? 'rgba(94,168,255,0.2)' : 'rgba(255,255,255,0.06)', color: !categoryId ? '#5ea8ff' : '#4a5a7e' } }, "All"),
            categories.map(function (c) { return React.createElement("button", { key: c.id, onClick: () => setCategoryId(c.id), style: { padding: '5px 10px', borderRadius: 8, border: '1px solid', fontSize: 11, fontWeight: 700, cursor: 'pointer', whiteSpace: 'nowrap', flexShrink: 0, background: categoryId === c.id ? 'rgba(94,168,255,0.1)' : 'transparent', borderColor: categoryId === c.id ? 'rgba(94,168,255,0.2)' : 'rgba(255,255,255,0.06)', color: categoryId === c.id ? '#5ea8ff' : '#4a5a7e' } },
                c.emoji,
                " ",
                c.name); })),
        loading && React.createElement("div", { style: { padding: '18px 0', color: '#5a6a94', fontSize: 13 } }, "Loading..."),
        !loading && papers.map(function (p, i) { return React.createElement("div", { key: p.blind_id || p.id, style: { animation: 'fadeIn .25s ease ' + (i * 0.04) + 's both' } },
            React.createElement(PaperCard, { paper: p, onOpen: (paper) => push('paper', paper.blind_id), onProfile: onOpenProfile, onLike: onLike })); }),
        !loading && papers.length === 0 && React.createElement("div", { style: { textAlign: 'center', padding: 30, color: '#4a5a7e' } },
            React.createElement("p", null, "No papers found.")));
}
function PaperDetail({ blindId, onBack, onProfile, useApi, onToast, relatedPool }) {
    const [paper, setPaper] = useState(null);
    const [loading, setLoading] = useState(true);
    const [comment, setComment] = useState('');
    const [posting, setPosting] = useState(false);
    useEffect(function () {
        const url = useApi ? ('/api/submissions/' + blindId) : ('/api/submissions/' + blindId + '/public');
        API.get(url).then(function (res) { if (res && res.submission)
            setPaper(res.submission); setLoading(false); });
    }, [blindId, useApi]);
    const related = useMemo(function () { if (!paper || !relatedPool)
        return []; return relatedPool.filter(function (x) { return x.blind_id !== paper.blind_id; }).map(function (x) { return { paper: x, score: similarityScore(paper, x) }; }).filter(function (x) { return x.score > 0; }).sort(function (a, b) { return b.score - a.score; }).slice(0, 3).map(function (x) { return x.paper; }); }, [paper, relatedPool]);
    function submitComment() { if (!comment.trim() || !paper)
        return; setPosting(true); API.post('/api/submissions/' + paper.blind_id + '/comments', { body: comment.trim(), comment_type: 'note' }).then(function (res) { setPosting(false); if (res && res.comment) {
        const next = Object.assign({}, paper, { comments: (paper.comments || []).concat([res.comment]), comment_count: (paper.comment_count || 0) + 1 });
        setPaper(next);
        setComment('');
    } }); }
    if (loading)
        return React.createElement("div", { style: { padding: '18px 0', color: '#5a6a94', fontSize: 13 } }, "Loading paper...");
    if (!paper)
        return React.createElement("div", { style: { padding: '18px 0', color: '#ef4444', fontSize: 13 } }, "Paper not found.");
    return React.createElement("div", { style: { width: '100%', maxWidth: 900, margin: '0 auto' } },
        React.createElement("button", { onClick: onBack, style: { display: 'flex', alignItems: 'center', gap: 5, background: 'none', border: 'none', color: '#5ea8ff', cursor: 'pointer', fontSize: 13, fontWeight: 600, marginBottom: 10 } }, "\u2190 Back"),
        React.createElement("div", { style: { background: 'rgba(255,255,255,0.03)', border: '1px solid rgba(255,255,255,0.07)', borderRadius: 14, padding: 20, marginBottom: 14 } },
            React.createElement("div", { style: { display: 'flex', alignItems: 'center', gap: 10, marginBottom: 12 } },
                React.createElement("div", { onClick: () => onProfile && onProfile(paper.author), style: { cursor: 'pointer' } },
                    React.createElement(Av, { user: paper.author, size: 40 })),
                React.createElement("div", { style: { flex: 1 } },
                    React.createElement("div", { style: { display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap' } },
                        React.createElement("span", { onClick: () => onProfile && onProfile(paper.author), style: { fontWeight: 700, fontSize: 14, cursor: 'pointer' } }, paper.author && paper.author.display_name),
                        paper.author && paper.author.orcid && React.createElement("a", { href: orcidUrl(paper.author.orcid), target: "_blank", rel: "noreferrer", style: { fontSize: 10, fontWeight: 700, color: '#4ade80', textDecoration: 'none', border: '1px solid rgba(74,222,128,0.25)', padding: '1px 6px', borderRadius: 999 } }, "ORCID")),
                    React.createElement("div", { style: { fontSize: 11, color: '#5a6a94' } },
                        paper.category && paper.category.emoji,
                        " ",
                        paper.category && paper.category.name,
                        " \u00B7 ",
                        formatDate(paper.created_at))),
                React.createElement(Badge, { label: paper.status_label || 'Submitted', color: paper.status_color || '#6b7db3' })),
            React.createElement("h1", { style: { fontSize: 22, fontWeight: 800, lineHeight: 1.28, margin: '0 0 10px' } }, paper.title),
            React.createElement("div", { style: { display: 'flex', gap: 8, flexWrap: 'wrap', marginBottom: 12 } },
                paper.status === 'published' && React.createElement("button", { onClick: () => copyText(formatBibTeX(paper)).then(() => onToast && onToast('Citation copied')), style: { padding: '7px 12px', borderRadius: 10, border: '1px solid rgba(94,168,255,0.2)', background: 'rgba(94,168,255,0.08)', color: '#5ea8ff', fontWeight: 700, fontSize: 11, cursor: 'pointer' } }, "Cite"),
                paper.status === 'published' && React.createElement("button", { onClick: () => copyText(window.location.href).then(() => onToast && onToast('Link copied')), style: { padding: '7px 12px', borderRadius: 10, border: '1px solid rgba(255,255,255,0.08)', background: 'rgba(255,255,255,0.03)', color: '#c7d2f0', fontWeight: 700, fontSize: 11, cursor: 'pointer' } }, "Copy Link")),
            React.createElement("div", { style: { fontSize: 10, fontWeight: 700, color: '#5ea8ff', textTransform: 'uppercase', letterSpacing: '0.08em', marginBottom: 6 } }, "Abstract"),
            React.createElement("p", { style: { fontSize: 14, lineHeight: 1.6, color: '#a0aed0', margin: '0 0 14px' } }, paper.abstract),
            React.createElement("div", { style: { fontSize: 10, fontWeight: 700, color: '#5ea8ff', textTransform: 'uppercase', letterSpacing: '0.08em', marginBottom: 6 } }, "Paper"),
            React.createElement("div", { style: { fontSize: 13, lineHeight: 1.7, color: '#c7d2f0', whiteSpace: 'pre-wrap' } }, paper.body_text || 'No full paper text provided.')),
        React.createElement("div", { style: { background: 'rgba(255,255,255,0.03)', border: '1px solid rgba(255,255,255,0.07)', borderRadius: 14, padding: 18, marginBottom: 14 } },
            React.createElement("h3", { style: { fontSize: 14, fontWeight: 700, margin: '0 0 12px' } },
                "Discussion (",
                (paper.comments || []).length,
                ")"),
            (paper.comments || []).map(function (c) { return React.createElement("div", { key: c.id, style: { display: 'flex', gap: 10, marginBottom: 14, paddingBottom: 14, borderBottom: '1px solid rgba(255,255,255,0.04)' } },
                React.createElement(Av, { user: c.author, size: 28 }),
                React.createElement("div", { style: { flex: 1 } },
                    React.createElement("div", { style: { display: 'flex', alignItems: 'center', gap: 6, marginBottom: 3 } },
                        React.createElement("span", { style: { fontWeight: 600, fontSize: 12 } }, c.author && c.author.display_name),
                        React.createElement("span", { style: { fontSize: 10, color: '#4a5a7e' } }, timeAgo(c.created_at))),
                    React.createElement("p", { style: { fontSize: 13, lineHeight: 1.5, color: '#8a9ac4', margin: 0 } }, c.body))); }),
            useApi && React.createElement("div", { style: { display: 'flex', gap: 8, alignItems: 'flex-end' } },
                React.createElement(Av, { user: DEFAULT_USER, size: 28 }),
                React.createElement("div", { style: { flex: 1, position: 'relative' } },
                    React.createElement("textarea", { value: comment, onChange: (e) => setComment(e.target.value), placeholder: 'Join the discussion...', onKeyDown: (e) => { if (e.key === 'Enter' && !e.shiftKey) {
                            e.preventDefault();
                            submitComment();
                        } }, style: { width: '100%', padding: '9px 40px 9px 12px', borderRadius: 10, border: '1px solid rgba(255,255,255,0.08)', background: 'rgba(255,255,255,0.03)', color: '#edf1ff', fontSize: 13, resize: 'none', minHeight: 38, fontFamily: 'inherit' } }),
                    React.createElement("button", { onClick: submitComment, style: { position: 'absolute', right: 6, bottom: 6, background: comment.trim() ? '#5ea8ff' : 'transparent', border: 'none', borderRadius: 6, padding: 5, cursor: 'pointer', color: comment.trim() ? '#071120' : '#3a4a6e' } }, posting ? '…' : '▶')))),
        related.length > 0 && React.createElement("div", { style: { background: 'rgba(255,255,255,0.03)', border: '1px solid rgba(255,255,255,0.07)', borderRadius: 14, padding: 18 } },
            React.createElement("h3", { style: { fontSize: 14, fontWeight: 700, margin: '0 0 12px' } }, "Papers Like This"),
            related.map(function (r) { return React.createElement("div", { key: r.blind_id, style: { padding: '10px 0', borderBottom: '1px solid rgba(255,255,255,0.05)' } },
                React.createElement("div", { style: { fontSize: 13, fontWeight: 700, color: '#edf1ff' } }, r.title),
                React.createElement("div", { style: { fontSize: 11, color: '#6b7db3' } }, r.author && r.author.display_name)); })));
}
function BuilderScreen({ categories, useApi, onToast, onOpenTool, pushProfile }) {
    const [tab, setTab] = useState('drafts');
    const [drafts, setDrafts] = useState([]);
    const [loading, setLoading] = useState(false);
    const [editingId, setEditingId] = useState(null);
    const [title, setTitle] = useState('');
    const [abstract, setAbstract] = useState('');
    const [body, setBody] = useState('');
    const [categoryId, setCategoryId] = useState(categories[0] ? categories[0].id : 1);
    const [error, setError] = useState('');
    const [selfReview, setSelfReview] = useState(null);
    function loadDrafts() { if (!useApi)
        return; setLoading(true); API.get('/api/builder/drafts').then(function (res) { setDrafts((res && res.papers) || []); setLoading(false); }); }
    useEffect(function () { if (useApi)
        loadDrafts(); }, [useApi]);
    function resetCompose() { setEditingId(null); setTitle(''); setAbstract(''); setBody(''); setCategoryId(categories[0] ? categories[0].id : 1); setError(''); setSelfReview(null); }
    function editDraft(d) { setEditingId(d.blind_id); setTitle(d.title || ''); setAbstract(d.abstract || ''); setBody(d.body_text || ''); setCategoryId(d.category && d.category.id ? d.category.id : (categories[0] ? categories[0].id : 1)); setTab('compose'); setSelfReview(null); }
    function saveDraft() { if (!useApi) {
        setError('Sign in to use the Builder.');
        return;
    } setError(''); const payload = { title: title.trim() || 'Untitled Draft', abstract: abstract, body_text: body, category_id: categoryId, is_draft: true }; const req = editingId ? API.put('/api/submissions/' + editingId, payload) : API.post('/api/submissions', payload); req.then(function (res) { if (res && res.submission) {
        onToast && onToast('Draft saved');
        loadDrafts();
        if (!editingId)
            setEditingId(res.submission.blind_id);
    }
    else {
        setError((res && res.error) || 'Save failed');
    } }); }
    function submitForReview() { if (!useApi) {
        setError('Sign in to submit for review.');
        return;
    } setError(''); const payload = { title: title.trim(), abstract: abstract.trim(), body_text: body, category_id: categoryId, is_draft: false }; const req = editingId ? API.put('/api/submissions/' + editingId, Object.assign({}, payload, { submit_for_review: true })) : API.post('/api/submissions', payload); req.then(function (res) { if (res && res.submission) {
        onToast && onToast('Sent to admin queue');
        loadDrafts();
        setTab('drafts');
        resetCompose();
    }
    else {
        setError((res && res.error) || 'Submission failed');
    } }); }
    function deleteDraft(bid) { if (!confirm('Delete this draft?'))
        return; API.del('/api/submissions/' + bid).then(function (res) { if (res && res.ok) {
        onToast && onToast('Draft deleted');
        loadDrafts();
        if (editingId === bid)
            resetCompose();
    } }); }
    function runSelfReview() { const text = [title, abstract, body].join('\n\n').trim(); if (!text) {
        setError('Add manuscript text first.');
        return;
    } const fd = new FormData(); fd.append('tool', 'desk_review'); fd.append('input', text); API.postForm('/api/tools/run', fd).then(function (res) { setSelfReview(res); setTab('compose'); }); }
    const examples = [
        { title: 'OCM Sample Output', body: 'Use the tools page to run contraction-rate analysis on time series. Sample traces let new users learn the output before they upload data.' },
        { title: 'Desk Review Example', body: 'Draft a paper privately, run the desk rubric, improve weak categories, then submit only when you are ready for the admin queue.' },
        { title: 'Builder Workflow', body: 'Compose, self-evaluate, revise, save as draft, and only then send to admin review. Nothing public appears in Discovery until an editor promotes it.' }
    ];
    if (!useApi)
        return React.createElement("div", { style: { width: '100%', maxWidth: 900, margin: '0 auto' } },
            React.createElement("div", { style: { padding: '18px 16px', borderRadius: 14, background: 'rgba(255,255,255,0.03)', border: '1px solid rgba(255,255,255,0.07)' } },
                React.createElement("h2", { style: { fontSize: 20, fontWeight: 800, margin: '0 0 8px' } }, "Builder"),
                React.createElement("p", { style: { fontSize: 13, color: '#8a9ac4', lineHeight: 1.6 } }, "The Builder is a private workbench. Sign in to save drafts, run self-evaluations, and submit to the admin queue.")));
    return React.createElement("div", { style: { width: '100%', maxWidth: 900, margin: '0 auto', animation: 'fadeIn .25s' } },
        React.createElement("div", { style: { marginBottom: 14, padding: '14px 16px', borderRadius: 12, background: 'linear-gradient(135deg,rgba(94,168,255,0.05),rgba(94,168,255,0.02))', border: '1px solid rgba(94,168,255,0.08)' } },
            React.createElement("p", { style: { fontSize: 13, color: '#8a9ac4', lineHeight: 1.6 } },
                React.createElement("span", { style: { color: '#5ea8ff', fontWeight: 700 } }, "Builder."),
                " Quiet, private, and tool-rich. Draft here, run self-evaluation here, and submit only when the paper is ready for the admin desk.")),
        React.createElement("div", { style: { display: 'flex', gap: 3, padding: 3, borderRadius: 10, background: 'rgba(255,255,255,0.03)', marginBottom: 12 } },
            React.createElement("button", { onClick: () => setTab('drafts'), style: { flex: 1, padding: '8px', borderRadius: 8, border: 'none', cursor: 'pointer', fontSize: 12, fontWeight: 700, background: tab === 'drafts' ? 'rgba(94,168,255,0.12)' : 'transparent', color: tab === 'drafts' ? '#5ea8ff' : '#4a5a7e' } }, "Drafts"),
            React.createElement("button", { onClick: () => setTab('compose'), style: { flex: 1, padding: '8px', borderRadius: 8, border: 'none', cursor: 'pointer', fontSize: 12, fontWeight: 700, background: tab === 'compose' ? 'rgba(94,168,255,0.12)' : 'transparent', color: tab === 'compose' ? '#5ea8ff' : '#4a5a7e' } }, "Compose"),
            React.createElement("button", { onClick: () => setTab('examples'), style: { flex: 1, padding: '8px', borderRadius: 8, border: 'none', cursor: 'pointer', fontSize: 12, fontWeight: 700, background: tab === 'examples' ? 'rgba(94,168,255,0.12)' : 'transparent', color: tab === 'examples' ? '#5ea8ff' : '#4a5a7e' } }, "Examples")),
        tab === 'drafts' && React.createElement("div", null,
            React.createElement("div", { style: { display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 10 } },
                React.createElement("h2", { style: { fontSize: 18, fontWeight: 800 } }, "My Builder Queue"),
                React.createElement("button", { onClick: () => { resetCompose(); setTab('compose'); }, style: { padding: '8px 12px', borderRadius: 10, border: 'none', background: 'linear-gradient(135deg,#5ea8ff,#3d8be0)', color: '#071120', fontWeight: 700, fontSize: 12, cursor: 'pointer' } }, "+ New Draft")),
            loading && React.createElement("div", { style: { padding: '18px 0', fontSize: 13, color: '#5a6a94' } }, "Loading drafts..."),
            !loading && drafts.length === 0 && React.createElement("div", { style: { padding: '18px 16px', borderRadius: 12, background: 'rgba(255,255,255,0.02)', border: '1px solid rgba(255,255,255,0.05)', color: '#6b7db3', fontSize: 13 } }, "No drafts yet. Start a draft, run self-review, and submit when ready."),
            !loading && drafts.map(function (d) { return React.createElement("div", { key: d.blind_id, style: { padding: '14px 16px', borderRadius: 12, background: 'rgba(255,255,255,0.03)', border: '1px solid rgba(255,255,255,0.07)', marginBottom: 10 } },
                React.createElement("div", { style: { display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', gap: 12, marginBottom: 8 } },
                    React.createElement("div", { style: { flex: 1 } },
                        React.createElement("div", { style: { fontWeight: 700, fontSize: 15, marginBottom: 3 } }, d.title),
                        React.createElement("div", { style: { fontSize: 12, color: '#6b7db3' } },
                            d.category && d.category.name,
                            " \u00B7 updated ",
                            timeAgo(d.updated_at || d.created_at))),
                    React.createElement(Badge, { label: d.status_label || 'Draft', color: d.status_color || '#6b7db3' })),
                React.createElement("div", { style: { fontSize: 12, color: '#8a9ac4', lineHeight: 1.5, marginBottom: 10 } }, d.abstract || 'No abstract yet.'),
                React.createElement("div", { style: { display: 'flex', gap: 6, flexWrap: 'wrap' } },
                    React.createElement("button", { onClick: () => editDraft(d), style: { padding: '7px 12px', borderRadius: 10, border: 'none', background: 'rgba(94,168,255,0.12)', color: '#5ea8ff', fontWeight: 700, fontSize: 11, cursor: 'pointer' } }, "Open in Builder"),
                    React.createElement("button", { onClick: () => { editDraft(d); runSelfReview(); }, style: { padding: '7px 12px', borderRadius: 10, border: 'none', background: 'rgba(56,189,248,0.12)', color: '#38bdf8', fontWeight: 700, fontSize: 11, cursor: 'pointer' } }, "Self Review"),
                    React.createElement("button", { onClick: () => deleteDraft(d.blind_id), style: { padding: '7px 12px', borderRadius: 10, border: 'none', background: 'rgba(239,68,68,0.12)', color: '#ef4444', fontWeight: 700, fontSize: 11, cursor: 'pointer' } }, "Delete"))); })),
        tab === 'compose' && React.createElement("div", { style: { background: 'rgba(255,255,255,0.03)', border: '1px solid rgba(255,255,255,0.07)', borderRadius: 14, padding: 18 } },
            React.createElement("div", { style: { display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 } },
                React.createElement("h2", { style: { fontSize: 18, fontWeight: 800 } }, editingId ? 'Edit Draft' : 'Compose'),
                React.createElement("button", { onClick: runSelfReview, style: { padding: '7px 12px', borderRadius: 10, border: '1px solid rgba(56,189,248,0.22)', background: 'rgba(56,189,248,0.08)', color: '#38bdf8', fontWeight: 700, fontSize: 11, cursor: 'pointer' } }, "Run Self Review")),
            React.createElement("div", { style: { display: 'flex', flexDirection: 'column', gap: 10 } },
                React.createElement("input", { value: title, onChange: (e) => setTitle(e.target.value), placeholder: 'Title \u2014 what did you investigate?', style: { width: '100%', padding: '10px 12px', borderRadius: 10, border: '1px solid rgba(255,255,255,0.08)', background: 'rgba(255,255,255,0.03)', color: '#edf1ff', fontSize: 14, fontWeight: 600 } }),
                React.createElement("select", { value: categoryId, onChange: (e) => setCategoryId(parseInt(e.target.value, 10)), style: { width: '100%', padding: '10px 12px', borderRadius: 10, border: '1px solid rgba(255,255,255,0.08)', background: 'rgba(255,255,255,0.03)', color: '#edf1ff', fontSize: 12 } }, categories.map(function (c) { return React.createElement("option", { key: c.id, value: c.id },
                    c.emoji,
                    " ",
                    c.name); })),
                React.createElement("textarea", { value: abstract, onChange: (e) => setAbstract(e.target.value), rows: 4, placeholder: 'Abstract \u2014 summarize the main claim, method, and result.', style: { width: '100%', padding: '10px 12px', borderRadius: 10, border: '1px solid rgba(255,255,255,0.08)', background: 'rgba(255,255,255,0.03)', color: '#edf1ff', fontSize: 12, resize: 'vertical', fontFamily: 'inherit' } }),
                React.createElement("textarea", { value: body, onChange: (e) => setBody(e.target.value), rows: 14, placeholder: 'Full paper text', style: { width: '100%', padding: '10px 12px', borderRadius: 10, border: '1px solid rgba(255,255,255,0.08)', background: 'rgba(255,255,255,0.03)', color: '#edf1ff', fontSize: 12, resize: 'vertical', fontFamily: 'inherit', lineHeight: 1.55 } }),
                error && React.createElement("div", { style: { fontSize: 12, color: '#ef4444', padding: '8px 10px', borderRadius: 8, background: 'rgba(239,68,68,0.08)' } }, error),
                React.createElement("div", { style: { display: 'flex', gap: 8, flexWrap: 'wrap' } },
                    React.createElement("button", { onClick: saveDraft, style: { padding: '12px 16px', borderRadius: 10, border: 'none', background: 'rgba(255,255,255,0.08)', color: '#d7def7', fontWeight: 700, fontSize: 12, cursor: 'pointer' } }, "Save Draft"),
                    React.createElement("button", { onClick: submitForReview, style: { padding: '12px 16px', borderRadius: 10, border: 'none', background: 'linear-gradient(135deg,#5ea8ff,#3d8be0)', color: '#071120', fontWeight: 800, fontSize: 12, cursor: 'pointer' } }, "Submit for Review"),
                    React.createElement("button", { onClick: () => onOpenTool && onOpenTool('desk_review', title + '\n\n' + abstract + '\n\n' + body), style: { padding: '12px 16px', borderRadius: 10, border: 'none', background: 'rgba(56,189,248,0.12)', color: '#38bdf8', fontWeight: 700, fontSize: 12, cursor: 'pointer' } }, "Open Desk Tool"))),
            selfReview && React.createElement("div", { style: { marginTop: 16, padding: '14px 16px', borderRadius: 12, background: 'rgba(255,255,255,0.02)', border: '1px solid rgba(255,255,255,0.05)' } },
                React.createElement(ToolResultView, { result: selfReview }))),
        tab === 'examples' && React.createElement("div", null, examples.map(function (ex, i) { return React.createElement("div", { key: i, style: { padding: '16px 18px', borderRadius: 14, background: 'rgba(255,255,255,0.03)', border: '1px solid rgba(255,255,255,0.07)', marginBottom: 10 } },
            React.createElement("div", { style: { fontSize: 15, fontWeight: 800, marginBottom: 6 } }, ex.title),
            React.createElement("div", { style: { fontSize: 13, color: '#8a9ac4', lineHeight: 1.6, marginBottom: 10 } }, ex.body),
            React.createElement("button", { onClick: () => setTab('compose'), style: { padding: '7px 12px', borderRadius: 10, border: 'none', background: 'rgba(94,168,255,0.12)', color: '#5ea8ff', fontWeight: 700, fontSize: 11, cursor: 'pointer' } }, "Build from here")); })));
}
function ToolHub({ onOpen }) {
    return React.createElement("div", { style: { width: '100%', maxWidth: 900, margin: '0 auto', animation: 'fadeIn .25s' } },
        React.createElement("div", { style: { padding: '14px 16px', borderRadius: 12, background: 'linear-gradient(135deg,rgba(94,168,255,0.05),rgba(94,168,255,0.02))', border: '1px solid rgba(94,168,255,0.08)', marginBottom: 14 } },
            React.createElement("p", { style: { fontSize: 13, color: '#8a9ac4', lineHeight: 1.6 } },
                React.createElement("span", { style: { color: '#5ea8ff', fontWeight: 700 } }, "Research tools."),
                " Full-strength outputs with metrics and visual traces. Use the Builder to prepare papers; use these engines to stress-test ideas.")),
        React.createElement("div", { style: { display: 'flex', flexDirection: 'column', gap: 10 } }, TOOL_DEFS.map(function (t) { return React.createElement("div", { key: t.id, onClick: () => onOpen(t.id, ''), style: { padding: '14px 16px', borderRadius: 12, background: 'rgba(255,255,255,0.03)', border: '1px solid rgba(255,255,255,0.07)', cursor: 'pointer', display: 'flex', alignItems: 'center', gap: 12 } },
            React.createElement("div", { style: { width: 40, height: 40, borderRadius: 10, background: t.color + '15', border: '1px solid ' + t.color + '30', display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: 20, flexShrink: 0 } }, t.emoji),
            React.createElement("div", { style: { flex: 1 } },
                React.createElement("div", { style: { fontWeight: 700, fontSize: 14 } }, t.title),
                React.createElement("div", { style: { fontSize: 12, color: '#6b7db3' } }, t.desc)),
            React.createElement("span", { style: { color: '#3a4a6e', fontSize: 18 } }, "\u203A")); })));
}
function ToolRunner({ toolId, seedInput, onBack, useApi }) {
    const def = TOOL_DEFS.find(function (t) { return t.id === toolId; }) || TOOL_DEFS[0];
    const [input, setInput] = useState(seedInput || '');
    const [file, setFile] = useState(null);
    const [loading, setLoading] = useState(false);
    const [result, setResult] = useState(null);
    useEffect(function () { setInput(seedInput || ''); }, [seedInput, toolId]);
    function run() { if (!useApi) {
        setResult({ error: 'Sign in to use tools.' });
        return;
    } setLoading(true); const fd = new FormData(); fd.append('tool', toolId); fd.append('input', input); if (file)
        fd.append('file', file); API.postForm('/api/tools/run', fd).then(function (res) { setLoading(false); setResult(res); }); }
    return React.createElement("div", { style: { width: '100%', maxWidth: 900, margin: '0 auto' } },
        React.createElement("button", { onClick: onBack, style: { display: 'flex', alignItems: 'center', gap: 5, background: 'none', border: 'none', color: '#5ea8ff', cursor: 'pointer', fontSize: 13, fontWeight: 600, marginBottom: 12 } }, "\u2190 Back"),
        React.createElement("div", { style: { background: 'rgba(255,255,255,0.03)', border: '1px solid rgba(255,255,255,0.07)', borderRadius: 14, padding: 18 } },
            React.createElement("h2", { style: { fontSize: 20, fontWeight: 800, margin: '0 0 6px' } }, def.title),
            React.createElement("p", { style: { fontSize: 13, color: '#6b7db3', margin: '0 0 14px' } }, def.desc),
            React.createElement("div", { style: { display: 'flex', flexDirection: 'column', gap: 10 } },
                React.createElement("textarea", { value: input, onChange: (e) => setInput(e.target.value), rows: 8, placeholder: toolId === 'desk_review' ? 'Paste manuscript text here...' : 'Paste numbers, CSV text, or a short note. If you do not supply a dataset, a built-in sample will run.', style: { width: '100%', padding: '10px 12px', borderRadius: 10, border: '1px solid rgba(255,255,255,0.08)', background: 'rgba(255,255,255,0.03)', color: '#edf1ff', fontSize: 12, fontFamily: 'inherit', resize: 'vertical', boxSizing: 'border-box' } }),
                React.createElement("label", { style: { padding: '12px 14px', borderRadius: 10, border: '1px dashed rgba(255,255,255,0.12)', background: 'rgba(255,255,255,0.02)', fontSize: 12, color: '#8a9ac4', cursor: 'pointer' } },
                    file ? file.name : 'Attach a file (CSV, TXT, TEX, PDF)',
                    React.createElement("input", { type: 'file', style: { display: 'none' }, onChange: (e) => setFile(e.target.files && e.target.files[0] ? e.target.files[0] : null) })),
                React.createElement("button", { onClick: run, disabled: loading, style: { padding: '12px', borderRadius: 10, border: 'none', fontSize: 13, fontWeight: 700, cursor: 'pointer', background: 'linear-gradient(135deg,#5ea8ff,#3d8be0)', color: '#071120' } }, loading ? 'Running...' : 'Run Analysis')),
            result && React.createElement("div", { style: { marginTop: 16, padding: '14px 16px', borderRadius: 12, background: 'rgba(255,255,255,0.02)', border: '1px solid rgba(255,255,255,0.05)' } },
                React.createElement(ToolResultView, { result: result }))));
}
function ProfileScreen({ user, isSelf, useApi, onLogout, onToast, onProfileSaved }) {
    const [editing, setEditing] = useState(false);
    const [displayName, setDisplayName] = useState(user.display_name || '');
    const [bio, setBio] = useState(user.bio || '');
    const [avatarColor, setAvatarColor] = useState(user.avatar_color || '#5ea8ff');
    const [orcid, setOrcid] = useState(user.orcid || '');
    const [saving, setSaving] = useState(false);
    const palette = ['#5ea8ff', '#22c55e', '#f0a030', '#ef4444', '#a855f7', '#ec4899', '#14b8a6', '#f97316'];
    function save() { setSaving(true); API.post('/api/auth/profile', { display_name: displayName, bio: bio, avatar_color: avatarColor, orcid: orcid }).then(function (res) { setSaving(false); if (res && res.user) {
        onProfileSaved && onProfileSaved(res.user);
        onToast && onToast('Profile saved');
        setEditing(false);
    } }); }
    return React.createElement("div", { style: { width: '100%', maxWidth: 760, margin: '0 auto', animation: 'fadeIn .25s' } },
        React.createElement("div", { style: { background: 'rgba(255,255,255,0.03)', border: '1px solid rgba(255,255,255,0.07)', borderRadius: 14, padding: 24, textAlign: 'center' } },
            React.createElement("div", { style: { display: 'flex', justifyContent: 'center' } },
                React.createElement(Av, { user: { display_name: displayName || user.display_name, avatar_color: avatarColor, initials: initialsForUser({ display_name: displayName || user.display_name }) }, size: 68 })),
            !editing && React.createElement(React.Fragment, null,
                React.createElement("h2", { style: { fontSize: 20, fontWeight: 800, margin: '12px 0 4px' } }, user.display_name),
                user.orcid && React.createElement("a", { href: orcidUrl(user.orcid), target: '_blank', rel: 'noreferrer', style: { display: 'inline-flex', alignItems: 'center', gap: 6, fontSize: 11, fontWeight: 700, color: '#4ade80', textDecoration: 'none', border: '1px solid rgba(74,222,128,0.25)', padding: '4px 8px', borderRadius: 999, marginBottom: 8 } },
                    "ORCID ",
                    normalizeOrcid(user.orcid)),
                user.bio && React.createElement("p", { style: { fontSize: 13, color: '#6b7db3', margin: '0 0 10px' } }, user.bio),
                React.createElement("div", { style: { display: 'flex', justifyContent: 'center', gap: 28, marginBottom: 14 } },
                    React.createElement("div", null,
                        React.createElement("div", { style: { fontSize: 20, fontWeight: 800 } }, user.paper_count || 0),
                        React.createElement("div", { style: { fontSize: 10, color: '#4a5a7e', fontWeight: 600 } }, "Papers")),
                    React.createElement("div", null,
                        React.createElement("div", { style: { fontSize: 20, fontWeight: 800 } }, user.review_count || 0),
                        React.createElement("div", { style: { fontSize: 10, color: '#4a5a7e', fontWeight: 600 } }, "Reviews")),
                    React.createElement("div", null,
                        React.createElement("div", { style: { fontSize: 20, fontWeight: 800, color: '#5ea8ff' } }, typeof user.reputation_score === 'number' ? user.reputation_score.toFixed(1) : '0.0'),
                        React.createElement("div", { style: { fontSize: 10, color: '#4a5a7e', fontWeight: 600 } }, "Rep"))),
                isSelf && useApi && React.createElement("button", { onClick: () => setEditing(true), style: { padding: '8px 20px', borderRadius: 8, border: '1px solid rgba(255,255,255,0.08)', background: 'transparent', color: '#5ea8ff', fontWeight: 700, fontSize: 12, cursor: 'pointer' } }, "Edit Profile")),
            editing && React.createElement("div", { style: { display: 'flex', flexDirection: 'column', gap: 10, marginTop: 12, textAlign: 'left' } },
                React.createElement("input", { value: displayName, onChange: (e) => setDisplayName(e.target.value), placeholder: 'Display name', style: { width: '100%', padding: '10px 12px', borderRadius: 10, border: '1px solid rgba(255,255,255,0.08)', background: 'rgba(255,255,255,0.03)', color: '#edf1ff', fontSize: 14 } }),
                React.createElement("input", { value: orcid, onChange: (e) => setOrcid(e.target.value), placeholder: 'ORCID (optional)', style: { width: '100%', padding: '10px 12px', borderRadius: 10, border: '1px solid rgba(255,255,255,0.08)', background: 'rgba(255,255,255,0.03)', color: '#edf1ff', fontSize: 13 } }),
                React.createElement("textarea", { value: bio, onChange: (e) => setBio(e.target.value), rows: 3, placeholder: 'Bio', style: { width: '100%', padding: '10px 12px', borderRadius: 10, border: '1px solid rgba(255,255,255,0.08)', background: 'rgba(255,255,255,0.03)', color: '#edf1ff', fontSize: 13, resize: 'vertical' } }),
                React.createElement("div", null,
                    React.createElement("div", { style: { fontSize: 11, fontWeight: 700, color: '#6b7db3', marginBottom: 6 } }, "Avatar Color"),
                    React.createElement("div", { style: { display: 'flex', gap: 8, flexWrap: 'wrap' } }, palette.map(function (c) { return React.createElement("button", { key: c, onClick: () => setAvatarColor(c), style: { width: 30, height: 30, borderRadius: '50%', background: c, border: avatarColor === c ? '3px solid #fff' : '3px solid transparent', cursor: 'pointer' } }); }))),
                React.createElement("div", { style: { display: 'flex', gap: 8 } },
                    React.createElement("button", { onClick: save, disabled: saving, style: { flex: 1, padding: '10px', borderRadius: 8, border: 'none', background: '#5ea8ff', color: '#071120', fontWeight: 700, fontSize: 13, cursor: 'pointer' } }, saving ? 'Saving...' : 'Save'),
                    React.createElement("button", { onClick: () => setEditing(false), style: { flex: 1, padding: '10px', borderRadius: 8, border: '1px solid rgba(255,255,255,0.08)', background: 'transparent', color: '#5a6a94', fontWeight: 700, fontSize: 13, cursor: 'pointer' } }, "Cancel"))),
            isSelf && useApi && React.createElement("button", { onClick: onLogout, style: { marginTop: 24, padding: '10px 24px', borderRadius: 10, border: '1px solid rgba(239,68,68,0.18)', background: 'rgba(239,68,68,0.08)', color: '#ef4444', fontWeight: 700, fontSize: 12, cursor: 'pointer' } }, "Sign Out")));
}
function AdminDash({ onBack, onToast }) {
    const [tab, setTab] = useState('submissions');
    const [subs, setSubs] = useState([]);
    const [users, setUsers] = useState([]);
    const [loading, setLoading] = useState(true);
    const [reviewing, setReviewing] = useState({});
    const [reviewResult, setReviewResult] = useState({});
    function load() { setLoading(true); if (tab === 'submissions') {
        API.get('/api/admin/submissions?scope=queue').then(function (res) { setSubs((res && res.submissions) || []); setLoading(false); });
    }
    else {
        API.get('/api/admin/users').then(function (res) { setUsers((res && res.users) || []); setLoading(false); });
    } }
    useEffect(function () { load(); }, [tab]);
    function setStatus(ref, status) { API.post('/api/admin/submissions/' + ref + '/status', { status: status }).then(function (res) { if (res && res.ok) {
        onToast && onToast('Status updated');
        load();
    } }); }
    function deleteSubmission(ref) { if (!confirm('Delete this submission?'))
        return; API.del('/api/admin/submissions/' + ref).then(function (res) { if (res && res.ok) {
        onToast && onToast('Submission deleted');
        load();
    } }); }
    function aiReview(sub) { setReviewing(function (prev) { return Object.assign({}, prev, { [sub.blind_id]: true }); }); API.get('/api/submissions/' + sub.blind_id).then(function (full) { const submission = full && full.submission ? full.submission : sub; const text = [submission.title || '', submission.abstract || '', submission.body_text || ''].join('\n\n'); const fd = new FormData(); fd.append('tool', 'desk_review'); fd.append('input', text); API.postForm('/api/tools/run', fd).then(function (res) { setReviewing(function (prev) { const next = Object.assign({}, prev); delete next[sub.blind_id]; return next; }); setReviewResult(function (prev) { return Object.assign({}, prev, { [sub.blind_id]: res }); }); }); }); }
    function setRole(id, role) { API.post('/api/admin/users/' + id + '/role', { role: role }).then(function (res) { if (res && res.ok) {
        onToast && onToast('Role updated');
        load();
    } }); }
    function banToggle(id, banned) { API.post('/api/admin/users/' + id + '/' + (banned ? 'unban' : 'ban'), {}).then(function (res) { if (res && res.ok) {
        onToast && onToast('User updated');
        load();
    } }); }
    function deleteUser(id) { if (!confirm('Delete this user?'))
        return; API.del('/api/admin/users/' + id).then(function (res) { if (res && res.ok) {
        onToast && onToast('User deleted');
        load();
    } }); }
    function resetDb() { if (!confirm('Delete all non-admin users and submissions?'))
        return; API.post('/api/admin/reset-db', {}).then(function (res) { if (res && res.ok) {
        onToast && onToast('Database reset');
        load();
    } }); }
    return React.createElement("div", { style: { width: '100%', maxWidth: 900, margin: '0 auto' } },
        React.createElement("button", { onClick: onBack, style: { display: 'flex', alignItems: 'center', gap: 5, background: 'none', border: 'none', color: '#5ea8ff', cursor: 'pointer', fontSize: 13, fontWeight: 600, marginBottom: 12 } }, "\u2190 Back"),
        React.createElement("h2", { style: { fontSize: 20, fontWeight: 800, margin: '0 0 4px' } }, "Admin Dashboard"),
        React.createElement("p", { style: { fontSize: 13, color: '#6b7db3', margin: '0 0 14px' } }, "Private queue, public promotion, and user controls."),
        React.createElement("div", { style: { display: 'flex', gap: 3, padding: 3, borderRadius: 10, background: 'rgba(255,255,255,0.03)', marginBottom: 12 } },
            React.createElement("button", { onClick: () => setTab('submissions'), style: { flex: 1, padding: '8px', borderRadius: 8, border: 'none', cursor: 'pointer', fontSize: 12, fontWeight: 700, background: tab === 'submissions' ? 'rgba(94,168,255,0.12)' : 'transparent', color: tab === 'submissions' ? '#5ea8ff' : '#4a5a7e' } }, "Queue"),
            React.createElement("button", { onClick: () => setTab('users'), style: { flex: 1, padding: '8px', borderRadius: 8, border: 'none', cursor: 'pointer', fontSize: 12, fontWeight: 700, background: tab === 'users' ? 'rgba(94,168,255,0.12)' : 'transparent', color: tab === 'users' ? '#5ea8ff' : '#4a5a7e' } }, "Users")),
        loading && React.createElement("div", { style: { padding: '20px 0', color: '#5a6a94', fontSize: 13 } }, "Loading..."),
        !loading && tab === 'submissions' && React.createElement("div", { style: { display: 'flex', flexDirection: 'column', gap: 10 } },
            subs.length === 0 && React.createElement("div", { style: { padding: '18px 16px', borderRadius: 12, background: 'rgba(255,255,255,0.02)', border: '1px solid rgba(255,255,255,0.05)', color: '#6b7db3', fontSize: 13 } }, "No papers in the admin queue."),
            subs.map(function (s) { return React.createElement("div", { key: s.blind_id, style: { padding: '14px 16px', borderRadius: 12, background: 'rgba(255,255,255,0.03)', border: '1px solid rgba(255,255,255,0.07)' } },
                React.createElement("div", { style: { display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 10, marginBottom: 8 } },
                    React.createElement("div", { style: { flex: 1 } },
                        React.createElement("div", { style: { fontWeight: 700, fontSize: 15 } }, s.title),
                        React.createElement("div", { style: { fontSize: 11, color: '#6b7db3' } },
                            s.author && s.author.display_name,
                            " \u00B7 ",
                            timeAgo(s.created_at))),
                    React.createElement(Badge, { label: s.status_label || s.status, color: s.status_color || '#6b7db3' })),
                React.createElement("div", { style: { fontSize: 12, color: '#8a9ac4', lineHeight: 1.5, marginBottom: 10 } }, s.abstract || ''),
                React.createElement("div", { style: { display: 'flex', gap: 6, flexWrap: 'wrap', marginBottom: 8 } },
                    React.createElement("button", { onClick: () => aiReview(s), disabled: !!reviewing[s.blind_id], style: { padding: '6px 12px', borderRadius: 8, border: '1px solid rgba(94,168,255,0.3)', background: 'rgba(94,168,255,0.08)', color: '#5ea8ff', fontWeight: 700, fontSize: 11, cursor: 'pointer' } }, reviewing[s.blind_id] ? 'Analyzing...' : '✦ AI Review'),
                    React.createElement("button", { onClick: () => setStatus(s.blind_id, 'in_discovery'), style: { padding: '6px 10px', borderRadius: 8, border: 'none', background: 'rgba(94,168,255,0.14)', color: '#5ea8ff', fontWeight: 700, fontSize: 11, cursor: 'pointer' } }, "Discovery"),
                    React.createElement("button", { onClick: () => setStatus(s.blind_id, 'under_review'), style: { padding: '6px 10px', borderRadius: 8, border: 'none', background: 'rgba(240,160,48,0.14)', color: '#f0a030', fontWeight: 700, fontSize: 11, cursor: 'pointer' } }, "Review"),
                    React.createElement("button", { onClick: () => setStatus(s.blind_id, 'published'), style: { padding: '6px 10px', borderRadius: 8, border: 'none', background: 'rgba(74,222,128,0.14)', color: '#4ade80', fontWeight: 700, fontSize: 11, cursor: 'pointer' } }, "Publish"),
                    React.createElement("button", { onClick: () => setStatus(s.blind_id, 'desk_returned'), style: { padding: '6px 10px', borderRadius: 8, border: 'none', background: 'rgba(240,160,48,0.14)', color: '#f0a030', fontWeight: 700, fontSize: 11, cursor: 'pointer' } }, "Return"),
                    React.createElement("button", { onClick: () => setStatus(s.blind_id, 'declined'), style: { padding: '6px 10px', borderRadius: 8, border: 'none', background: 'rgba(239,68,68,0.14)', color: '#ef4444', fontWeight: 700, fontSize: 11, cursor: 'pointer' } }, "Decline"),
                    React.createElement("button", { onClick: () => deleteSubmission(s.blind_id), style: { padding: '6px 10px', borderRadius: 8, border: 'none', background: 'rgba(255,255,255,0.06)', color: '#c7d2f0', fontWeight: 700, fontSize: 11, cursor: 'pointer' } }, "Delete")),
                reviewResult[s.blind_id] && React.createElement("div", { style: { padding: '12px 14px', borderRadius: 10, background: 'rgba(255,255,255,0.02)', border: '1px solid rgba(255,255,255,0.06)' } },
                    React.createElement(ToolResultView, { result: reviewResult[s.blind_id] }))); })),
        !loading && tab === 'users' && React.createElement("div", { style: { display: 'flex', flexDirection: 'column', gap: 10 } },
            users.length === 0 && React.createElement("div", { style: { padding: '18px 16px', borderRadius: 12, background: 'rgba(255,255,255,0.02)', border: '1px solid rgba(255,255,255,0.05)', color: '#6b7db3', fontSize: 13 } }, "No users found."),
            users.map(function (u) { return React.createElement("div", { key: u.id, style: { padding: '14px 16px', borderRadius: 12, background: 'rgba(255,255,255,0.03)', border: '1px solid rgba(255,255,255,0.07)' } },
                React.createElement("div", { style: { display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 10, marginBottom: 8 } },
                    React.createElement("div", { style: { display: 'flex', alignItems: 'center', gap: 10 } },
                        React.createElement(Av, { user: u, size: 34 }),
                        React.createElement("div", null,
                            React.createElement("div", { style: { fontWeight: 700, fontSize: 14 } }, u.display_name),
                            React.createElement("div", { style: { fontSize: 11, color: '#6b7db3' } },
                                u.email,
                                " \u00B7 ",
                                u.role,
                                u.is_banned ? ' · banned' : ''))),
                    React.createElement("div", { style: { fontSize: 11, color: '#5a6a94' } },
                        u.paper_count || 0,
                        " papers")),
                React.createElement("div", { style: { display: 'flex', gap: 6, flexWrap: 'wrap' } },
                    React.createElement("button", { onClick: () => setRole(u.id, u.role === 'admin' ? 'member' : 'admin'), style: { padding: '6px 10px', borderRadius: 8, border: 'none', background: 'rgba(94,168,255,0.14)', color: '#5ea8ff', fontWeight: 700, fontSize: 11, cursor: 'pointer' } }, u.role === 'admin' ? 'Demote' : 'Promote'),
                    React.createElement("button", { onClick: () => banToggle(u.id, u.is_banned), style: { padding: '6px 10px', borderRadius: 8, border: 'none', background: 'rgba(240,160,48,0.14)', color: '#f0a030', fontWeight: 700, fontSize: 11, cursor: 'pointer' } }, u.is_banned ? 'Unban' : 'Ban'),
                    React.createElement("button", { onClick: () => deleteUser(u.id), style: { padding: '6px 10px', borderRadius: 8, border: 'none', background: 'rgba(255,255,255,0.06)', color: '#c7d2f0', fontWeight: 700, fontSize: 11, cursor: 'pointer' } }, "Delete"))); }),
            React.createElement("button", { onClick: resetDb, style: { marginTop: 4, padding: '10px 14px', borderRadius: 10, border: '1px solid rgba(239,68,68,0.18)', background: 'rgba(239,68,68,0.08)', color: '#ef4444', fontWeight: 700, fontSize: 12, cursor: 'pointer' } }, "Reset Database")));
}
function App() {
    const [authed, setAuthed] = useState(false);
    const [useApi, setUseApi] = useState(false);
    const [user, setUser] = useState(DEFAULT_USER);
    const [categories, setCategories] = useState(FALLBACK_CATS);
    const [view, setView] = useState('discover');
    const [selectedBlindId, setSelectedBlindId] = useState(null);
    const [selectedProfile, setSelectedProfile] = useState(null);
    const [toolState, setToolState] = useState({ toolId: null, seedInput: '' });
    const [publicPool, setPublicPool] = useState([]);
    const [lastFeed, setLastFeed] = useState('discover');
    const [notifs, setNotifs] = useState([]);
    const [notifOpen, setNotifOpen] = useState(false);
    const [toast, setToast] = useState('');
    const headerRef = useRef(null);
    function showToast(msg) { setToast(msg); setTimeout(function () { setToast(''); }, 1800); }
    function pushProfile(profile) { setSelectedProfile(profile); setView('profile'); }
    function openTool(toolId, seedInput) { setToolState({ toolId: toolId, seedInput: seedInput || '' }); setView('tool'); }
    function onAuth(u, isReal) { const next = u && u.id !== 0 ? u : DEFAULT_USER; setUser(next); setAuthed(true); setUseApi(!!isReal); setView('discover'); }
    function onLogout() { API.post('/api/auth/logout', {}).then(function () { setAuthed(false); setUseApi(false); setUser(DEFAULT_USER); setView('discover'); setSelectedBlindId(null); setSelectedProfile(null); setToolState({ toolId: null, seedInput: '' }); setNotifs([]); }); }
    useEffect(function () { API.get('/api/categories').then(function (res) { if (res && res.categories && res.categories.length)
        setCategories(res.categories); }); }, []);
    useEffect(function () { API.get('/api/auth/me').then(function (res) { if (res && res.user) {
        setUser(res.user);
        setAuthed(true);
        setUseApi(true);
    } }); }, []);
    useEffect(function () { if (!useApi)
        return; function poll() { API.get('/api/notifications').then(function (res) { if (res && res.notifications)
        setNotifs(res.notifications); }); } poll(); const id = setInterval(poll, 30000); return function () { clearInterval(id); }; }, [useApi]);
    useEffect(function () { function closeOnOutside(e) { if (headerRef.current && !headerRef.current.contains(e.target))
        setNotifOpen(false); } document.addEventListener('click', closeOnOutside); return function () { document.removeEventListener('click', closeOnOutside); }; }, []);
    if (!authed)
        return React.createElement(AuthScreen, { onAuth: onAuth });
    const unread = notifs.filter(function (n) { return !n.is_read; }).length;
    return React.createElement("div", { style: { minHeight: '100vh', minHeight: '100dvh', background: 'linear-gradient(180deg,#080c18,#0d1225 30%,#0a0f1e)', color: '#edf1ff', fontFamily: '-apple-system,BlinkMacSystemFont,\'Helvetica Neue\',sans-serif' } },
        toast && React.createElement("div", { style: { position: 'fixed', top: 'calc(env(safe-area-inset-top) + 14px)', left: '50%', transform: 'translateX(-50%)', zIndex: 300, padding: '8px 12px', borderRadius: 10, background: 'rgba(12,17,34,0.94)', border: '1px solid rgba(94,168,255,0.18)', color: '#c7d2f0', fontSize: 12, fontWeight: 700, backdropFilter: 'blur(14px)' } }, toast),
        React.createElement("header", { ref: headerRef, style: { position: 'sticky', top: 0, zIndex: 100, background: 'rgba(8,12,24,0.92)', backdropFilter: 'blur(16px)', borderBottom: '1px solid rgba(255,255,255,0.06)', paddingTop: 'env(safe-area-inset-top)' } },
            React.createElement("div", { style: { width: '100%', maxWidth: 980, margin: '0 auto', padding: '10px 16px', display: 'flex', alignItems: 'center', justifyContent: 'space-between' } },
                React.createElement("div", { onClick: () => { setView('discover'); setSelectedBlindId(null); }, style: { cursor: 'pointer', display: 'flex', alignItems: 'center', gap: 8 } },
                    React.createElement("div", { style: { width: 28, height: 28, borderRadius: 7, background: 'linear-gradient(135deg,#5ea8ff,#3d70b8)', display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: 14, fontWeight: 800, color: '#fff' } }, "\u29C7"),
                    React.createElement("span", { style: { fontWeight: 800, fontSize: 15 } }, "The Journal")),
                React.createElement("div", { style: { display: 'flex', alignItems: 'center', gap: 6, position: 'relative' } },
                    React.createElement("button", { onClick: () => { setView('builder'); setSelectedBlindId(null); }, style: { display: 'flex', alignItems: 'center', gap: 4, padding: '6px 14px', borderRadius: 8, background: 'linear-gradient(135deg,#5ea8ff,#3d8be0)', border: 'none', color: '#071120', fontWeight: 700, fontSize: 12, cursor: 'pointer' } }, "+ Submit"),
                    React.createElement("button", { onClick: (e) => { e.stopPropagation(); setNotifOpen(!notifOpen); }, style: { width: 32, height: 32, borderRadius: 8, background: 'rgba(255,255,255,0.04)', border: '1px solid rgba(255,255,255,0.08)', color: '#5a6a94', display: 'flex', alignItems: 'center', justifyContent: 'center', cursor: 'pointer', position: 'relative' } },
                        React.createElement(HIcon, { d: 'M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9M13.73 21a2 2 0 0 1-3.46 0', s: 15 }),
                        unread > 0 && React.createElement("span", { style: { position: 'absolute', top: 5, right: 5, width: 6, height: 6, borderRadius: '50%', background: '#ff4d6a' } })),
                    React.createElement("div", { onClick: () => { setSelectedProfile(user); setView('profile'); }, style: { cursor: 'pointer' } },
                        React.createElement(Av, { user: user, size: 30 })),
                    notifOpen && React.createElement(NotificationDropdown, { items: notifs, onClose: () => setNotifOpen(false), onMarkRead: function (id) { API.post('/api/notifications/read', id ? { id: id } : {}).then(function () { setNotifs(function (prev) { return prev.map(function (n) { return !id || n.id === id ? Object.assign({}, n, { is_read: true }) : n; }); }); }); } })))),
        React.createElement("main", { style: { width: '100%', maxWidth: 980, margin: '0 auto', padding: '16px 16px 88px' } },
            view === 'discover' && React.createElement(FeedScreen, { tab: 'discover', push: function (v, d) { if (v === 'paper') {
                    setLastFeed('discover');
                    setSelectedBlindId(d);
                    setView('paper');
                } }, useApi: useApi, categories: categories, onPool: setPublicPool, onLike: function (p, liked, likeCount, done) { if (!useApi) {
                    done(false);
                    return;
                } API.post('/api/submissions/' + p.blind_id + '/like', {}).then(function (res) { if (res && typeof res.liked === 'boolean') {
                    done(true, res.liked, res.like_count);
                } }); }, onOpenProfile: pushProfile }),
            view === 'published' && React.createElement(FeedScreen, { tab: 'published', push: function (v, d) { if (v === 'paper') {
                    setLastFeed('published');
                    setSelectedBlindId(d);
                    setView('paper');
                } }, useApi: useApi, categories: categories, onPool: setPublicPool, onLike: function (p, liked, likeCount, done) { if (!useApi) {
                    done(false);
                    return;
                } API.post('/api/submissions/' + p.blind_id + '/like', {}).then(function (res) { if (res && typeof res.liked === 'boolean') {
                    done(true, res.liked, res.like_count);
                } }); }, onOpenProfile: pushProfile }),
            view === 'paper' && selectedBlindId && React.createElement(PaperDetail, { blindId: selectedBlindId, onBack: () => setView(lastFeed), onProfile: pushProfile, useApi: useApi, onToast: showToast, relatedPool: publicPool }),
            view === 'builder' && React.createElement(BuilderScreen, { categories: categories, useApi: useApi, onToast: showToast, onOpenTool: openTool, pushProfile: pushProfile }),
            view === 'tools' && React.createElement(ToolHub, { onOpen: openTool }),
            view === 'tool' && toolState.toolId && React.createElement(ToolRunner, { toolId: toolState.toolId, seedInput: toolState.seedInput, onBack: () => setView('tools'), useApi: useApi }),
            view === 'profile' && React.createElement(ProfileScreen, { user: selectedProfile || user, isSelf: (!selectedProfile) || selectedProfile.id === user.id, useApi: useApi, onLogout: onLogout, onToast: showToast, onProfileSaved: function (updated) { setUser(updated); setSelectedProfile(updated); } }),
            view === 'admin' && user.role === 'admin' && React.createElement(AdminDash, { onBack: () => setView('discover'), onToast: showToast })),
        React.createElement("nav", { style: { position: 'fixed', bottom: 0, left: 0, right: 0, zIndex: 50, background: 'rgba(8,12,24,0.94)', backdropFilter: 'blur(16px)', borderTop: '1px solid rgba(255,255,255,0.06)', display: 'flex', justifyContent: 'space-around', padding: '6px 0 calc(6px + env(safe-area-inset-bottom))' } },
            React.createElement("button", { onClick: () => setView('discover'), style: { display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 1, background: 'none', border: 'none', cursor: 'pointer', padding: '3px 10px', color: view === 'discover' ? '#5ea8ff' : '#3a4a6e', fontSize: 9, fontWeight: 600 } },
                React.createElement(HIcon, { d: 'M3 9l9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z', s: 19 }),
                React.createElement("span", null, "Discover")),
            React.createElement("button", { onClick: () => setView('published'), style: { display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 1, background: 'none', border: 'none', cursor: 'pointer', padding: '3px 10px', color: view === 'published' ? '#5ea8ff' : '#3a4a6e', fontSize: 9, fontWeight: 600 } },
                React.createElement(HIcon, { d: 'M6 9H4.5a2.5 2.5 0 0 1 0-5H6M18 9h1.5a2.5 2.5 0 0 0 0-5H18M4 22h16M18 2H6v7a6 6 0 0 0 12 0V2Z', s: 19 }),
                React.createElement("span", null, "Published")),
            React.createElement("button", { onClick: () => setView('builder'), style: { display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 1, background: 'none', border: 'none', cursor: 'pointer', padding: '3px 10px', color: view === 'builder' ? '#5ea8ff' : '#3a4a6e', fontSize: 9, fontWeight: 600 } },
                React.createElement(HIcon, { d: 'M12 5v14M5 12h14', s: 19 }),
                React.createElement("span", null, "Builder")),
            React.createElement("button", { onClick: () => setView('tools'), style: { display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 1, background: 'none', border: 'none', cursor: 'pointer', padding: '3px 10px', color: (view === 'tools' || view === 'tool') ? '#5ea8ff' : '#3a4a6e', fontSize: 9, fontWeight: 600 } },
                React.createElement(HIcon, { d: 'M4.5 3h15M6 3v16a2 2 0 0 0 2 2h8a2 2 0 0 0 2-2V3M6 14h12', s: 19 }),
                React.createElement("span", null, "Tools")),
            user.role === 'admin' && React.createElement("button", { onClick: () => setView('admin'), style: { display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 1, background: 'none', border: 'none', cursor: 'pointer', padding: '3px 10px', color: view === 'admin' ? '#5ea8ff' : '#3a4a6e', fontSize: 9, fontWeight: 600 } },
                React.createElement(HIcon, { d: 'M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z', s: 19 }),
                React.createElement("span", null, "Admin")),
            React.createElement("button", { onClick: () => { setSelectedProfile(user); setView('profile'); }, style: { display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 1, background: 'none', border: 'none', cursor: 'pointer', padding: '3px 10px', color: view === 'profile' ? '#5ea8ff' : '#3a4a6e', fontSize: 9, fontWeight: 600 } },
                React.createElement(HIcon, { d: 'M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2M12 3a4 4 0 1 0 0 8 4 4 0 0 0 0-8z', s: 19 }),
                React.createElement("span", null, "Profile"))));
}

try {
  const root = ReactDOM.createRoot(document.getElementById('root'));
  root.render(React.createElement(App, null));
  requestAnimationFrame(function(){
    requestAnimationFrame(function(){
      if (window.__journalBoot && typeof window.__journalBoot.markMounted === 'function') window.__journalBoot.markMounted();
    });
  });
} catch (err) {
  console.error(err);
  if (window.__journalBoot && typeof window.__journalBoot.showBootError === 'function') {
    window.__journalBoot.showBootError('The app hit a loading error. Refresh and try again.');
  }
}
})();
