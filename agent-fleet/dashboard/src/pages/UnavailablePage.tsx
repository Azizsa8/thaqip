import { Link } from 'react-router-dom';
import { Layout } from '../components/Header';
export function UnavailablePage() {
  return <Layout><section className="p-8"><h1>Feature unavailable / الميزة غير متاحة</h1>
    <p>The API does not provide a catalogue, client records, operational dashboard, document lookup, PDF export, or version history endpoint.</p>
    <p>Search returns article text and available metadata. Ask returns citations and trust signals. No demo records are presented as backend data.</p>
    <Link to="/">Return to search / العودة للبحث</Link>
  </section></Layout>;
}
