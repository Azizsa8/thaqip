import { useState } from 'react';
import { Link, useLocation } from 'react-router-dom';
import { LanguageToggle } from './LanguageToggle';
import { hasApiKey, setApiKey } from '../api';
export function Header() {
  const { search } = useLocation();
  const [key, setKey] = useState('');
  const [configured, setConfigured] = useState(hasApiKey);
  return <header className="bg-white p-4 border-b space-y-3" dir="rtl">
    <nav className="flex flex-wrap gap-5" aria-label="Main navigation">
      <strong>سَنَد — Research</strong><Link to={`/${search}`}>البحث / Search</Link><Link to={`/ask${search}`}>اسأل / Ask</Link>
      <Link to="/browse">Catalogue (unavailable)</Link><Link to="/dashboard">Dashboard (unavailable)</Link><LanguageToggle />
    </nav>
    <p className="text-sm">API results; coverage and currency require verification. لا ضمان لاكتمال التغطية أو السريان.</p>
    <details><summary>API access / مفتاح الوصول</summary>
      <form onSubmit={e => { e.preventDefault(); setApiKey(key); setConfigured(Boolean(key.trim())); setKey(''); }} className="flex flex-wrap gap-3 p-2">
        <label>Session API key <input type="password" autoComplete="off" value={key} onChange={e => setKey(e.target.value)} /></label>
        <button type="submit">Apply key</button><button type="button" onClick={() => { setApiKey(''); setKey(''); setConfigured(false); }}>Clear key</button>
        <span role="status">{configured ? 'Key set in memory' : 'No key — local authentication disabled by default'}</span>
        <p>Cleared on reload. Never saved to browser storage.</p>
      </form>
    </details>
  </header>;
}
export function Layout({ children }: { children: React.ReactNode }) {
  return <div className="min-h-screen bg-[var(--color-background)]"><Header /><main>{children}</main></div>;
}
export function Breadcrumb({ items }: { items: Array<{ label: string; href?: string }> }) {
  return <nav aria-label="Breadcrumb">{items.map((item, i) => item.href ? <Link key={i} to={item.href}>{item.label} / </Link> : <span key={i}>{item.label}</span>)}</nav>;
}
